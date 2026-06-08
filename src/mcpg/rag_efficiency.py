"""RAG efficiency suite — Phase A.

Cross-backend retrieval-quality reporting for pgvector ANN indexes
(HNSW, IVFFlat) and pg_turboquant. One report shape, three backends:
an agent can ask "is this index pulling its weight?" without knowing
which access method backs it.

The headline function :func:`analyze_vector_search_efficiency` does
the work end-to-end:

1. Detects the index's access method (``hnsw`` / ``ivfflat`` /
   ``turboquant``).
2. Picks the right *knob axis* for that backend (``ef_search`` for
   HNSW, ``probes`` for IVFFlat, ``candidate_limit`` for turboquant).
3. Samples a small set of query vectors from the table itself,
   computes a brute-force exact baseline using the pgvector *function-
   form* distance (documented as non-indexed), and sweeps the
   approximate retrieval across a multiplier curve.
4. Reports recall@k, Spearman rho, Kendall tau, per-query latency, and
   (for turboquant) the page-pruning ratio from ``tq_last_scan_stats``.
5. Emits actionable findings against a small rule table.

**Cost note.** Per call costs
``sample_size x (1 brute-force + len(multipliers) x approximate)``
queries. The brute-force query is sequential on the underlying
column. Run ad-hoc, not on a cron.

**Statistical helpers** (Spearman rho, Kendall tau, recall@k) are
implemented in pure Python — no SciPy / NumPy at runtime, consistent
with the rest of MCPg.

**Sample-size cap** of 100 is reused from
:data:`mcpg.vector_tuning._MAX_SAMPLE_SIZE` (re-declared here to keep
the cross-module dependency narrow). Future tightening / loosening
of that cap should happen in both places.

**Adaptive thresholds (Phase E, future).** Rule firing currently uses
hardcoded thresholds (``baseline_recall_low`` at 0.80, etc.). A
follow-up phase will optionally replace them with corpus-percentile
thresholds learned from accumulated observations of this same
function — see ``docs/plans/rag-efficiency-suite.md``.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

# Reused from vector_tuning's identifier convention — refuse anything
# that would require delimited quoting.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Reused cap from vector_tuning. Each sample run triggers
# 1 + len(multipliers) queries, so even at the cap a run with the
# default 4-multiplier sweep is 500 queries — significant but
# bounded.
_MAX_SAMPLE_SIZE = 100

# Public-facing metric → (operator, distance function). pgvector
# documents the function form as non-indexed (used for the brute-
# force baseline) and the operator form as the planner's index path.
# inner_product is intentionally absent in Phase A — its operator
# form (``<#>``) returns the negative inner product (so ``ORDER BY
# col <#> q ASC`` puts most-similar first), while the function form
# (``inner_product(col, q)``) returns the raw value (most-similar is
# largest, needs DESC). Mixing the two ordering directions in one
# report risks subtle off-by-direction bugs; deferred to a follow-up
# once the operator/function convention is tested end-to-end.
_DISTANCE_OPERATORS: dict[str, str] = {"l2": "<->", "cosine": "<=>"}
_DISTANCE_FUNCTIONS: dict[str, str] = {"l2": "l2_distance", "cosine": "cosine_distance"}

# Backend → knob GUC name (pgvector) or pseudo-knob (turboquant).
# Single source of truth for the abstraction; the value-per-
# multiplier mapping is :func:`_knob_value_for_multiplier`.
_KNOB_NAMES: dict[str, str] = {
    "hnsw": "ef_search",
    "ivfflat": "probes",
    "turboquant": "candidate_limit",
}

# Backend → GUC namespace for ``SET LOCAL``. Turboquant's knob is
# a function argument, not a GUC, so it has no entry here.
_GUC_NAMESPACE: dict[str, str] = {
    "hnsw": "hnsw.ef_search",
    "ivfflat": "ivfflat.probes",
}

# Rule thresholds. Documented at the module level so the adaptive-
# threshold framework (Phase E) has one place to override them.
_THRESHOLD_RECALL_LOW = 0.80
_THRESHOLD_RERANK_FLAT_DELTA = 0.02
_THRESHOLD_RERANK_STEEP_LOW = 0.70
_THRESHOLD_RERANK_STEEP_HIGH = 0.95
_THRESHOLD_RANKING_DEGRADED_RECALL = 0.90
_THRESHOLD_RANKING_DEGRADED_SPEARMAN = 0.50
_THRESHOLD_PRUNING_INEFFECTIVE = 0.10


class VectorEfficiencyError(Exception):
    """Raised when a vector-efficiency analysis cannot complete."""


def _validate_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise VectorEfficiencyError(f"invalid {kind} name: {name!r}")


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --- statistical helpers (pure Python) -------------------------------------


def _rank(values: list[float]) -> list[float]:
    """Average-rank values, ties get the mean of their position range.

    Uses the standard tie-handling convention so Spearman rho is well-
    defined when scores cluster (which quantized indexes produce in
    spades).
    """
    sorted_with_idx = sorted(enumerate(values), key=lambda p: p[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(sorted_with_idx):
        j = i
        while j + 1 < len(sorted_with_idx) and sorted_with_idx[j + 1][1] == sorted_with_idx[i][1]:
            j += 1
        # Positions are 1-indexed; the tied group's rank is the mean
        # of (i+1, i+2, …, j+1).
        avg = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[sorted_with_idx[k][0]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation. Returns 0.0 when either series is constant."""
    n = len(xs)
    if n != len(ys) or n == 0:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation. Range [-1, 1]; 0 when uncorrelated."""
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    return _pearson(_rank(xs), _rank(ys))


def _kendall_tau(xs: list[float], ys: list[float]) -> float:
    """Kendall tau-b, accepting ties.

    ``(concordant - discordant) / sqrt((n_pairs - tx)(n_pairs - ty))``
    Robust on ties; degrades more gracefully than Spearman when
    scores cluster.
    """
    n = len(xs)
    if n != len(ys) or n < 2:
        return 0.0
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            if dx == 0 and dy == 0:
                ties_x += 1
                ties_y += 1
            elif dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            else:
                if dx * dy > 0:
                    concordant += 1
                else:
                    discordant += 1
    total_pairs = n * (n - 1) / 2
    denom = math.sqrt((total_pairs - ties_x) * (total_pairs - ties_y))
    if denom == 0:
        return 0.0
    return (concordant - discordant) / denom


def _recall_at_k(approx_ids: list[Any], exact_ids: list[Any]) -> float:
    """Set-overlap recall: |approx ∩ exact| / |exact|. Returns 0 when exact is empty."""
    if not exact_ids:
        return 0.0
    return len(set(approx_ids) & set(exact_ids)) / len(exact_ids)


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile. ``q`` ∈ [0, 1]."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


# --- knob axis -------------------------------------------------------------


def _knob_value_for_multiplier(backend: str, k: int, multiplier: int) -> int:
    """Map ``candidate_multiplier`` to the right per-backend knob value.

    HNSW's ``ef_search`` controls the candidate pool size and must be
    >= k. IVFFlat's ``probes`` is a count of lists to scan, so the
    multiplier maps to ``probes`` directly. Turboquant's
    ``candidate_limit`` is the approximate retrieval pool (in rows),
    so ``k * multiplier`` is the natural axis.
    """
    if multiplier < 1:
        raise VectorEfficiencyError(f"candidate_multiplier must be >= 1; got {multiplier!r}")
    if backend == "hnsw":
        return max(k * multiplier, k + 10)
    if backend == "ivfflat":
        return max(multiplier, 1)
    if backend == "turboquant":
        return k * multiplier
    raise VectorEfficiencyError(f"unsupported backend {backend!r}")


# --- dataclasses -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RerankLiftPoint:
    """One point on the rerank-lift curve."""

    candidate_multiplier: int
    knob_name: str
    knob_value: int
    recall_at_k: float
    p50_latency_ms: float
    p95_latency_ms: float


@dataclass(frozen=True, slots=True)
class VectorEfficiencyFinding:
    """An advisor finding produced from the computed metrics."""

    code: str
    severity: str  # GOOD / WARNING / CRITICAL
    evidence: str
    suggested_action: str


@dataclass(frozen=True, slots=True)
class VectorEfficiencyReport:
    """End-to-end retrieval-quality report for one ANN index."""

    schema: str
    table: str
    column: str
    index_name: str
    backend: str  # "hnsw" | "ivfflat" | "turboquant"
    metric: str
    sample_size: int
    k: int

    recall_at_k_baseline: float
    rerank_lift_curve: list[RerankLiftPoint]
    score_rank_correlation_spearman: float
    score_rank_correlation_kendall: float
    pages_pruned_ratio_p50: float | None  # turboquant only

    findings: list[VectorEfficiencyFinding] = field(default_factory=list)


# --- rule table ------------------------------------------------------------
#
# Rules consume the computed metrics — they have no I/O — so they're
# trivial to unit-test in isolation.


def _finding_baseline_recall_low(report_partial: dict[str, Any]) -> VectorEfficiencyFinding | None:
    if report_partial["recall_at_k_baseline"] >= _THRESHOLD_RECALL_LOW:
        return None
    return VectorEfficiencyFinding(
        code="baseline_recall_low",
        severity="CRITICAL",
        evidence=(
            f"recall@{report_partial['k']} at the default knob value is "
            f"{report_partial['recall_at_k_baseline']:.3f} — below the "
            f"{_THRESHOLD_RECALL_LOW} threshold. The index is returning the wrong neighbours too often."
        ),
        suggested_action=(
            f"Raise the per-query knob ({report_partial['knob_name']}); inspect the lift curve "
            "for the smallest value that recovers acceptable recall."
        ),
    )


def _finding_rerank_lift_flat(report_partial: dict[str, Any]) -> VectorEfficiencyFinding | None:
    curve: list[RerankLiftPoint] = report_partial["rerank_lift_curve"]
    if len(curve) < 2:
        return None
    baseline = curve[0].recall_at_k
    top = curve[-1].recall_at_k
    if top - baseline > _THRESHOLD_RERANK_FLAT_DELTA:
        return None
    if baseline < _THRESHOLD_RECALL_LOW:
        # Lift is flat but baseline is already bad — surface
        # baseline_recall_low instead, not lift_flat (which would
        # confusingly suggest "knob already saturated").
        return None
    return VectorEfficiencyFinding(
        code="rerank_lift_flat",
        severity="WARNING",
        evidence=(
            f"recall@{report_partial['k']} barely moves across the multiplier sweep "
            f"(baseline {baseline:.3f} → top {top:.3f}, delta {top - baseline:.3f} ≤ "
            f"{_THRESHOLD_RERANK_FLAT_DELTA}). The {report_partial['knob_name']} knob is over-provisioned."
        ),
        suggested_action=(
            f"Lower {report_partial['knob_name']} towards the smallest value that keeps recall "
            f"≥ {_THRESHOLD_RECALL_LOW + 0.1:.2f}; the latency savings come for free."
        ),
    )


def _finding_rerank_lift_steep(report_partial: dict[str, Any]) -> VectorEfficiencyFinding | None:
    curve: list[RerankLiftPoint] = report_partial["rerank_lift_curve"]
    if len(curve) < 2:
        return None
    baseline = curve[0].recall_at_k
    # Find the first point at >= 4x multiplier (or fall through if none).
    high_point = next((p for p in curve if p.candidate_multiplier >= 4), curve[-1])
    if baseline >= _THRESHOLD_RERANK_STEEP_LOW or high_point.recall_at_k < _THRESHOLD_RERANK_STEEP_HIGH:
        return None
    return VectorEfficiencyFinding(
        code="rerank_lift_steep",
        severity="WARNING",
        evidence=(
            f"baseline recall@{report_partial['k']} is {baseline:.3f} but lifts to "
            f"{high_point.recall_at_k:.3f} at multiplier {high_point.candidate_multiplier}x "
            f"({report_partial['knob_name']}={high_point.knob_value}). The knob is set too tight."
        ),
        suggested_action=(
            f"Raise the default {report_partial['knob_name']} to {high_point.knob_value} — "
            "the recall gain is worth the latency cost."
        ),
    )


def _finding_ranking_degraded(report_partial: dict[str, Any]) -> VectorEfficiencyFinding | None:
    if (
        report_partial["recall_at_k_baseline"] < _THRESHOLD_RANKING_DEGRADED_RECALL
        or report_partial["score_rank_correlation_spearman"] >= _THRESHOLD_RANKING_DEGRADED_SPEARMAN
    ):
        return None
    return VectorEfficiencyFinding(
        code="ranking_degraded",
        severity="WARNING",
        evidence=(
            f"recall@{report_partial['k']} is {report_partial['recall_at_k_baseline']:.3f} (good set overlap) "
            f"but Spearman rho between approximate and exact ranks is "
            f"{report_partial['score_rank_correlation_spearman']:.3f} — the right rows are returned "
            "but in the wrong order."
        ),
        suggested_action=(
            "Increase the quantization fidelity (more bits for turboquant; consider switching "
            "halfvec → vector for pgvector) or pick a metric better suited to the embeddings' "
            "magnitude distribution."
        ),
    )


def _finding_pruning_ineffective(report_partial: dict[str, Any]) -> VectorEfficiencyFinding | None:
    if report_partial["backend"] != "turboquant":
        return None
    pruned = report_partial["pages_pruned_ratio_p50"]
    if pruned is None or pruned >= _THRESHOLD_PRUNING_INEFFECTIVE:
        return None
    return VectorEfficiencyFinding(
        code="pruning_ineffective",
        severity="WARNING",
        evidence=(
            f"turboquant pages_pruned ratio (median {pruned:.3f}) is below "
            f"{_THRESHOLD_PRUNING_INEFFECTIVE} — the index is scanning most pages anyway, "
            "negating turboquant's main optimisation."
        ),
        suggested_action=(
            "Consider raising the index's ``lists`` option (IVF clustering) or increasing the "
            "default ``candidate_limit`` so the planner has more candidates to prune from."
        ),
    )


_RULES = (
    _finding_baseline_recall_low,
    _finding_rerank_lift_flat,
    _finding_rerank_lift_steep,
    _finding_ranking_degraded,
    _finding_pruning_ineffective,
)


def _evaluate_rules(report_partial: dict[str, Any]) -> list[VectorEfficiencyFinding]:
    findings: list[VectorEfficiencyFinding] = []
    for rule in _RULES:
        if (f := rule(report_partial)) is not None:
            findings.append(f)
    return findings


# --- backend detection -----------------------------------------------------


_DETECT_INDEX_SQL = """
SELECT n.nspname AS schema, i.relname AS index, t.relname AS table,
       am.amname AS backend, a.attname AS column
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = i.relnamespace
JOIN pg_am am ON am.oid = i.relam
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
WHERE n.nspname = %s AND i.relname = %s AND am.amname IN ('hnsw', 'ivfflat', 'turboquant')
"""

_FIND_INDEX_FOR_COLUMN_SQL = """
SELECT i.relname AS index, am.amname AS backend
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = i.relnamespace
JOIN pg_am am ON am.oid = i.relam
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
WHERE n.nspname = %s AND t.relname = %s AND a.attname = %s
  AND am.amname IN ('hnsw', 'ivfflat', 'turboquant')
ORDER BY i.relname
LIMIT 1
"""


async def _detect_index(
    driver: SqlDriver, schema: str, table: str, column: str, index_name: str | None
) -> tuple[str, str]:
    """Return ``(index_name, backend)``.

    When ``index_name`` is supplied, asserts it exists, uses one of
    the three supported access methods, and is on the named column.
    When ``None``, picks the first matching index on
    ``schema.table.column``.
    """
    if index_name is not None:
        _validate_identifier(index_name, "index_name")
        rows = await driver.execute_query(_DETECT_INDEX_SQL, params=[schema, index_name], force_readonly=True)
        if not rows:
            raise VectorEfficiencyError(f"no HNSW / IVFFlat / turboquant index named {schema}.{index_name}")
        cells = rows[0].cells
        # The catalog row carries the index's actual table + column;
        # both must match what the caller said they're analyzing.
        # Without the table check, an index named `idx` in this schema
        # on a *different* table that happens to share a column name
        # would pass — and the brute-force baseline below would run
        # against the wrong relation, producing meaningless recall.
        if cells.get("table") != table:
            raise VectorEfficiencyError(
                f"index {schema}.{index_name} is on table {cells.get('table')!r}, not {table!r}"
            )
        if cells.get("column") != column:
            raise VectorEfficiencyError(
                f"index {schema}.{index_name} is not on column {column!r} (it indexes {cells.get('column')!r})"
            )
        return index_name, cells["backend"]
    # Auto-detect.
    rows = await driver.execute_query(_FIND_INDEX_FOR_COLUMN_SQL, params=[schema, table, column], force_readonly=True)
    if not rows:
        raise VectorEfficiencyError(f"no HNSW / IVFFlat / turboquant index found on {schema}.{table}.{column}")
    return rows[0].cells["index"], rows[0].cells["backend"]


# --- sampling + queries ----------------------------------------------------


async def _collect_sample_vectors(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    id_column: str,
    sample_size: int,
) -> list[tuple[Any, str]]:
    """Sample ``sample_size`` (id, vector_text) tuples from the table.

    Picks rows in id order — deterministic, same convention as
    :func:`mcpg.vector_tuning.vector_recall_at_k`.
    """
    sql = (
        f"SELECT {_quoted(id_column)} AS id, {_quoted(column)}::text AS vec "
        f"FROM {_quoted(schema)}.{_quoted(table)} "
        f"WHERE {_quoted(column)} IS NOT NULL "
        f"ORDER BY {_quoted(id_column)} "
        f"LIMIT %s"
    )
    rows = await driver.execute_query(sql, params=[sample_size], force_readonly=True)
    return [(row.cells["id"], row.cells["vec"]) for row in rows or []]


async def _exact_top_k(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    id_column: str,
    query_vec: str,
    metric: str,
    k: int,
    exclude_id: Any,
) -> tuple[list[Any], list[float]]:
    """Brute-force exact top-k via the pgvector distance function.

    Returns parallel lists of ids and distances. Excludes the query
    row itself from the result so a query vector pulled from the
    table doesn't dominate its own top-k.
    """
    func = _DISTANCE_FUNCTIONS[metric]
    sql = (
        f"SELECT {_quoted(id_column)} AS id, "
        f"{func}({_quoted(column)}, %s::vector) AS dist "
        f"FROM {_quoted(schema)}.{_quoted(table)} "
        f"WHERE {_quoted(column)} IS NOT NULL AND {_quoted(id_column)} <> %s "
        f"ORDER BY dist ASC LIMIT %s"
    )
    rows = await driver.execute_query(sql, params=[query_vec, exclude_id, k], force_readonly=True)
    ids: list[Any] = []
    dists: list[float] = []
    for row in rows or []:
        ids.append(row.cells["id"])
        dists.append(float(row.cells["dist"]))
    return ids, dists


async def _approx_top_k_pgvector(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    id_column: str,
    query_vec: str,
    metric: str,
    k: int,
    backend: str,
    knob_value: int,
    exclude_id: Any,
) -> tuple[list[Any], list[float], float]:
    """HNSW / IVFFlat path. Returns (ids, distances, latency_ms).

    ``SET LOCAL`` only persists for the duration of the transaction
    it runs in. Two separate ``execute_query`` calls land in two
    separate (implicit) transactions, so a ``SET LOCAL`` in the first
    is a no-op for the second. We work around this by sending both
    statements as one semicolon-separated string in a single call —
    the same pattern :func:`mcpg.vector_tuner_advanced.analyze_hnsw_recall`
    uses. ``knob_value`` is an int produced by our own
    :func:`_knob_value_for_multiplier`, so interpolating it into the
    SQL text is safe.
    """
    operator = _DISTANCE_OPERATORS[metric]
    guc = _GUC_NAMESPACE[backend]
    sql = (
        f"SET LOCAL {guc} = {knob_value}; "
        f"SELECT {_quoted(id_column)} AS id, "
        f"({_quoted(column)} {operator} %s::vector) AS dist "
        f"FROM {_quoted(schema)}.{_quoted(table)} "
        f"WHERE {_quoted(column)} IS NOT NULL AND {_quoted(id_column)} <> %s "
        f"ORDER BY {_quoted(column)} {operator} %s::vector LIMIT %s"
    )
    started = time.monotonic()
    rows = await driver.execute_query(sql, params=[query_vec, exclude_id, query_vec, k], force_readonly=True)
    latency_ms = (time.monotonic() - started) * 1000.0
    ids: list[Any] = []
    dists: list[float] = []
    for row in rows or []:
        ids.append(row.cells["id"])
        dists.append(float(row.cells["dist"]))
    return ids, dists, latency_ms


async def _approx_top_k_turboquant(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    id_column: str,
    query_vec: str,
    metric: str,
    k: int,
    candidate_limit: int,
) -> tuple[list[Any], list[float], float, float | None]:
    """Turboquant path. Returns (ids, distances, latency_ms, pages_pruned_ratio_or_None).

    Calls :mod:`mcpg.turboquant`'s wrapper so the metric mapping
    (`cosine` / `inner_product` / `l2` → upstream token) and the
    pgvector vs halfvec dispatch stay in one place.
    """
    # Imported here, not at module top, to avoid an unconditional
    # dependency between modules — turboquant has its own deferred
    # imports for the same reason.
    from mcpg.turboquant import (
        get_turboquant_last_scan_stats,
        turboquant_rerank_candidates,
    )

    started = time.monotonic()
    rows = await turboquant_rerank_candidates(
        driver,
        schema,
        table,
        id_column,
        column,
        query_vec,
        metric,
        candidate_limit=candidate_limit,
        final_limit=k,
    )
    latency_ms = (time.monotonic() - started) * 1000.0
    ids = [row.candidate_id for row in rows]
    # Use exact_distance (the SQL-side rerank result) as the "score"
    # for rank-correlation against the brute-force baseline.
    dists = [row.exact_distance for row in rows]
    # tq_last_scan_stats is backend-local — call right after to
    # capture the page counters for this specific query.
    pages_ratio: float | None = None
    stats = await get_turboquant_last_scan_stats(driver)
    if stats is not None and stats.pages_scanned and stats.pages_pruned is not None:
        pages_ratio = stats.pages_pruned / stats.pages_scanned
    return ids, dists, latency_ms, pages_ratio


# --- main entry point ------------------------------------------------------


async def analyze_vector_search_efficiency(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    id_column: str,
    *,
    index_name: str | None = None,
    k: int = 10,
    sample_size: int = 30,
    candidate_multipliers: tuple[int, ...] = (1, 2, 4, 10),
    metric: str = "cosine",
) -> VectorEfficiencyReport:
    """Sweep an ANN index's recall, rank-correlation, and (for turboquant) page-pruning.

    Returns a :class:`VectorEfficiencyReport` with the metrics and a
    rule-table-generated list of findings.

    Each call burns ``sample_size x (1 + len(candidate_multipliers))``
    queries; the brute-force baseline is sequential on the table.
    Run ad-hoc, not on a cron.

    Raises:
        VectorEfficiencyError: any identifier fails validation; the
            named index isn't HNSW / IVFFlat / turboquant; the column
            doesn't match; ``metric`` is not in the supported set; or
            the pgvector extension is not installed.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(column, "column")
    _validate_identifier(id_column, "id_column")
    if metric not in _DISTANCE_OPERATORS:
        expected = ", ".join(sorted(_DISTANCE_OPERATORS))
        raise VectorEfficiencyError(f"unsupported metric {metric!r}; expected one of {expected}")
    if k <= 0:
        raise VectorEfficiencyError(f"k must be positive; got {k!r}")
    if sample_size <= 0 or sample_size > _MAX_SAMPLE_SIZE:
        raise VectorEfficiencyError(f"sample_size must be in 1..{_MAX_SAMPLE_SIZE}; got {sample_size!r}")
    if not candidate_multipliers:
        raise VectorEfficiencyError("candidate_multipliers cannot be empty")
    if any(m < 1 for m in candidate_multipliers):
        raise VectorEfficiencyError(f"candidate_multipliers must all be >= 1; got {candidate_multipliers!r}")

    if not await extension_installed(driver, "vector"):
        raise VectorEfficiencyError("vector extension is not installed in this database")

    detected_name, backend = await _detect_index(driver, schema, table, column, index_name)
    samples = await _collect_sample_vectors(driver, schema, table, column, id_column, sample_size)

    if not samples:
        # Empty table or no non-null vectors — return an honest empty
        # report rather than divide-by-zero further down.
        return VectorEfficiencyReport(
            schema=schema,
            table=table,
            column=column,
            index_name=detected_name,
            backend=backend,
            metric=metric,
            sample_size=0,
            k=k,
            recall_at_k_baseline=0.0,
            rerank_lift_curve=[],
            score_rank_correlation_spearman=0.0,
            score_rank_correlation_kendall=0.0,
            pages_pruned_ratio_p50=None,
            findings=[],
        )

    # Brute-force baselines for every sample, plus a list-of-lists of
    # approx results indexed [multiplier_idx][sample_idx]. Computed in
    # nested loops so the bookkeeping stays readable.
    exact_per_sample: list[tuple[list[Any], list[float]]] = []
    for sample_id, vec in samples:
        ids, dists = await _exact_top_k(driver, schema, table, column, id_column, vec, metric, k, sample_id)
        exact_per_sample.append((ids, dists))

    curve: list[RerankLiftPoint] = []
    all_pruned_ratios: list[float] = []
    # For Spearman / Kendall we compute the correlation *per sample*
    # at the baseline (first multiplier), then average across
    # samples. A global concat of distances would mix query-to-query
    # distance scale differences with within-query ranking quality —
    # the latter is what we actually want to measure.
    per_sample_spearmans: list[float] = []
    per_sample_kendalls: list[float] = []

    for mult_idx, multiplier in enumerate(candidate_multipliers):
        knob_value = _knob_value_for_multiplier(backend, k, multiplier)
        per_sample_recalls: list[float] = []
        per_sample_latencies: list[float] = []
        for sample_idx, (sample_id, vec) in enumerate(samples):
            exact_ids, exact_dists = exact_per_sample[sample_idx]
            if backend == "turboquant":
                approx_ids, approx_dists, latency_ms, pages_ratio = await _approx_top_k_turboquant(
                    driver, schema, table, column, id_column, vec, metric, k, knob_value
                )
                if pages_ratio is not None:
                    all_pruned_ratios.append(pages_ratio)
            else:
                approx_ids, approx_dists, latency_ms = await _approx_top_k_pgvector(
                    driver, schema, table, column, id_column, vec, metric, k, backend, knob_value, sample_id
                )
            per_sample_recalls.append(_recall_at_k(approx_ids, exact_ids))
            per_sample_latencies.append(latency_ms)
            if mult_idx == 0:
                # Per-sample rank correlation on the IDs that appear
                # in both top-k lists. Requires at least 2 overlapping
                # IDs to be defined — otherwise skipped (don't pollute
                # the average with degenerate 0.0s).
                sample_approx: list[float] = []
                sample_exact: list[float] = []
                _accumulate_rank_pairs(approx_ids, approx_dists, exact_ids, exact_dists, sample_approx, sample_exact)
                if len(sample_approx) >= 2:
                    per_sample_spearmans.append(_spearman(sample_approx, sample_exact))
                    per_sample_kendalls.append(_kendall_tau(sample_approx, sample_exact))
        mean_recall = sum(per_sample_recalls) / len(per_sample_recalls)
        curve.append(
            RerankLiftPoint(
                candidate_multiplier=multiplier,
                knob_name=_KNOB_NAMES[backend],
                knob_value=knob_value,
                recall_at_k=mean_recall,
                p50_latency_ms=_percentile(per_sample_latencies, 0.50),
                p95_latency_ms=_percentile(per_sample_latencies, 0.95),
            )
        )

    spearman = sum(per_sample_spearmans) / len(per_sample_spearmans) if per_sample_spearmans else 0.0
    kendall = sum(per_sample_kendalls) / len(per_sample_kendalls) if per_sample_kendalls else 0.0
    pages_pruned_ratio_p50 = (
        _percentile(all_pruned_ratios, 0.50) if backend == "turboquant" and all_pruned_ratios else None
    )

    report_partial = {
        "k": k,
        "backend": backend,
        "knob_name": _KNOB_NAMES[backend],
        "recall_at_k_baseline": curve[0].recall_at_k,
        "rerank_lift_curve": curve,
        "score_rank_correlation_spearman": spearman,
        "pages_pruned_ratio_p50": pages_pruned_ratio_p50,
    }
    findings = _evaluate_rules(report_partial)

    return VectorEfficiencyReport(
        schema=schema,
        table=table,
        column=column,
        index_name=detected_name,
        backend=backend,
        metric=metric,
        sample_size=len(samples),
        k=k,
        recall_at_k_baseline=curve[0].recall_at_k,
        rerank_lift_curve=curve,
        score_rank_correlation_spearman=spearman,
        score_rank_correlation_kendall=kendall,
        pages_pruned_ratio_p50=pages_pruned_ratio_p50,
        findings=findings,
    )


def _accumulate_rank_pairs(
    approx_ids: list[Any],
    approx_dists: list[float],
    exact_ids: list[Any],
    exact_dists: list[float],
    out_approx: list[float],
    out_exact: list[float],
) -> None:
    """Append parallel (approx_distance, exact_distance) pairs for ids in both lists.

    For Spearman / Kendall we want score sequences for the same set
    of items. Items returned by approx but missing from exact (or
    vice versa) are skipped — they don't have a counterpart score.
    """
    approx_score = dict(zip(approx_ids, approx_dists, strict=False))
    exact_score = dict(zip(exact_ids, exact_dists, strict=False))
    for iid in set(approx_score) & set(exact_score):
        out_approx.append(approx_score[iid])
        out_exact.append(exact_score[iid])
