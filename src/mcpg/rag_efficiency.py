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


# --- audit_database adapter (Phase B) --------------------------------------


# Walk pg_index for every ANN index across all user schemas. Returned
# in (schema, table, column, index_name, backend) tuples so the audit
# walker can call analyze_vector_search_efficiency on each in turn.
# Single-column indexes only (``ix.indkey[0]`` with ``indnatts = 1``);
# composite ANN indexes are rare and would need a different sweep.
_LIST_ALL_ANN_INDEXES_SQL = """
SELECT n.nspname AS schema,
       t.relname AS table,
       a.attname AS column,
       i.relname AS index,
       am.amname AS backend
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = i.relnamespace
JOIN pg_am am ON am.oid = i.relam
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
WHERE am.amname IN ('hnsw', 'ivfflat', 'turboquant')
  AND ix.indnatts = 1
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY n.nspname, t.relname, i.relname
"""


# Single-column PRIMARY KEY lookup. Returns the pk column name when
# the table has exactly one primary-key column, ``None`` otherwise.
# Composite PKs and PK-less tables are both unauditable here — the
# audit walker skips them silently rather than guessing.
_DETECT_SINGLE_COL_PK_SQL = """
SELECT a.attname AS pk_column
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary = true
  AND i.indnatts = 1
"""


# Audit-time sample budget. Each per-index sweep runs
# 1 brute-force + len(multipliers) approx per sample.
# With 10 x (1 + 2) = 30 queries per index, the audit stays bounded
# even on databases with several ANN indexes.
_AUDIT_SAMPLE_SIZE = 10
_AUDIT_MULTIPLIERS: tuple[int, ...] = (1, 4)

# Severity → score deduction. Mirrors mcpg.turboquant's adapter so the
# two categories produce comparable scorecards.
_SEVERITY_DEDUCTION = {"CRITICAL": 30, "WARNING": 15, "GOOD": 0}


async def _detect_single_column_pk(driver: SqlDriver, schema: str, table: str) -> str | None:
    """Return the table's single-column primary key, or ``None``.

    Composite PKs and PK-less tables both return ``None`` — the audit
    walker treats both as unauditable rather than guessing an id
    column.
    """
    rows = await driver.execute_query(_DETECT_SINGLE_COL_PK_SQL, params=[schema, table], force_readonly=True)
    if not rows:
        return None
    return str(rows[0].cells["pk_column"])


async def audit_vector_indexes(driver: SqlDriver) -> Any:
    """Scorecard adapter — returns a CategoryResult or None.

    Returns ``None`` when there are no ANN indexes (HNSW / IVFFlat /
    turboquant) in user schemas, so :func:`audit.audit_database`
    cleanly omits the category from deployments that don't use them.

    Otherwise walks every ANN index, runs a small
    :func:`analyze_vector_search_efficiency` sweep per index
    (sample_size=10, multipliers=(1, 4)), and emits each finding as a
    MetricResult. Tables without a single-column primary key are
    skipped silently — the audit reports what it can, not what it
    can't.
    """
    # Late import — audit imports rag_efficiency at function-call
    # time, not at module load, so we keep the dependency direction
    # one-way at module-load time.
    from mcpg.audit import CategoryResult, MetricResult

    if not await extension_installed(driver, "vector"):
        # No pgvector → no ANN indexes possible. Omit the category.
        return None

    rows = await driver.execute_query(_LIST_ALL_ANN_INDEXES_SQL, force_readonly=True)
    if not rows:
        return None

    metrics: list[MetricResult] = []
    score = 100
    indexes_audited = 0
    indexes_skipped: list[tuple[str, str, str]] = []  # (schema.index, reason, ...)

    for row in rows:
        cells = row.cells
        schema = cells["schema"]
        table = cells["table"]
        column = cells["column"]
        index_name = cells["index"]
        # Skip tables we can't audit (no single-column PK).
        pk = await _detect_single_column_pk(driver, schema, table)
        if pk is None:
            indexes_skipped.append((f"{schema}.{index_name}", "no single-column primary key", table))
            continue
        try:
            report = await analyze_vector_search_efficiency(
                driver,
                schema,
                table,
                column,
                pk,
                index_name=index_name,
                k=10,
                sample_size=_AUDIT_SAMPLE_SIZE,
                candidate_multipliers=_AUDIT_MULTIPLIERS,
            )
        except VectorEfficiencyError as exc:
            # An unexpected per-index failure (e.g. metric mismatch
            # for a custom opclass) shouldn't sink the whole audit.
            indexes_skipped.append((f"{schema}.{index_name}", str(exc), table))
            continue

        indexes_audited += 1
        if not report.findings:
            metrics.append(
                MetricResult(
                    name=f"vector_efficiency:no_findings on {schema}.{index_name}",
                    value="ok",
                    unit="finding",
                    target="no findings",
                    status="GOOD",
                    severity=0,
                    evidence=(
                        f"recall@10 baseline {report.recall_at_k_baseline:.3f}, "
                        f"Spearman {report.score_rank_correlation_spearman:.3f} — within thresholds."
                    ),
                    suggestion="",
                )
            )
            continue
        for finding in report.findings:
            score -= _SEVERITY_DEDUCTION.get(finding.severity, 0)
            metrics.append(
                MetricResult(
                    name=f"vector_efficiency:{finding.code} on {schema}.{index_name}",
                    value=finding.code,
                    unit="finding",
                    target="no findings",
                    status=finding.severity,
                    severity=3 if finding.severity == "CRITICAL" else 2 if finding.severity == "WARNING" else 0,
                    evidence=finding.evidence,
                    suggestion=finding.suggested_action,
                )
            )

    if indexes_audited == 0 and not indexes_skipped:
        # No ANN indexes after filtering by schema — same as the
        # empty-result short-circuit above.
        return None

    # Surface skipped indexes as an INFO baseline metric (status
    # GOOD so they don't deduct from the score, but the operator
    # sees them).
    for label, reason, _table in indexes_skipped:
        metrics.append(
            MetricResult(
                name=f"vector_efficiency:skipped on {label}",
                value="skipped",
                unit="finding",
                target="auditable",
                status="GOOD",
                severity=0,
                evidence=f"skipped: {reason}",
                suggestion="",
            )
        )

    if not metrics:
        metrics.append(
            MetricResult(
                name="vector_efficiency:no_findings",
                value="ok",
                unit="finding",
                target="no findings",
                status="GOOD",
                severity=0,
                evidence="All ANN indexes pass the advisor rules.",
                suggestion="",
            )
        )

    score = max(0, score)
    status_label = "GOOD" if score >= 90 else ("WARNING" if score >= 70 else "CRITICAL")
    return CategoryResult(
        category="ANN Index Efficiency",
        status=status_label,
        score=score,
        metrics=metrics,
    )


# --- RAG-D: reranker analytics --------------------------------------------
#
# Reads from mcpg_rag.rerank_events (the schema shipped in RAG-C) and
# produces five analytics + a roll-up advisor + an audit category. All
# the heavy stats run in pure Python on rows pulled with a single
# query per analytic — no pushed-down aggregations beyond filtering.

# Thresholds — gathered here so the future Phase E adaptive-thresholds
# framework can override them in one place.
_THRESHOLD_RERANKER_IDLE_KENDALL = 0.85
_THRESHOLD_TOPK_STABLE_JACCARD = 0.90
_THRESHOLD_SCORE_CLUSTERING_TOP_DECILE = 0.50
_THRESHOLD_NDCG_HURTS_DELTA = -0.02
_THRESHOLD_NDCG_LIFTS_DELTA = 0.05

# Default analysis window — matches the plan's "last 7 days" framing.
_DEFAULT_WINDOW_DAYS = 7


def _jaccard(a: list[Any], b: list[Any]) -> float:
    """Set-overlap Jaccard: |A inter B| / |A union B|.

    Returns 0.0 when both sides are empty (rather than a NaN).
    """
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def _ndcg_at_k(grades: list[float], k: int) -> float:
    """NDCG@k from a list of relevance grades in *display order*.

    ``grades[0]`` is the grade of the item ranked 1, ``grades[1]`` of
    item ranked 2, and so on. Returns 0.0 when the ideal DCG is zero
    (all grades zero) so the metric stays in ``[0, 1]`` for callers.
    """
    if k <= 0 or not grades:
        return 0.0
    cut = grades[:k]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(cut))
    ideal = sorted(grades, reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg <= 0:
        return 0.0
    return dcg / idcg


def _histogram(values: list[float], n_buckets: int = 20) -> list[int]:
    """Equal-width histogram. Returns a list of ``n_buckets`` counts.

    The last bucket includes the max value (inclusive on both edges).
    """
    if n_buckets <= 0 or not values:
        return [0] * max(n_buckets, 0)
    lo, hi = min(values), max(values)
    if hi == lo:
        # All values identical → everything in the last bucket so the
        # caller's "top-decile share" computation still works.
        out = [0] * n_buckets
        out[-1] = len(values)
        return out
    width = (hi - lo) / n_buckets
    counts = [0] * n_buckets
    for v in values:
        idx = int((v - lo) / width)
        if idx >= n_buckets:
            idx = n_buckets - 1
        counts[idx] += 1
    return counts


# --- dataclasses ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RerankerLiftReport:
    window_days: int
    model: str | None
    retrieval_index: str | None
    query_count: int
    mean_spearman: float
    mean_kendall: float
    p25_spearman: float
    p75_spearman: float
    interpretation: str  # "reranker actively reorders" / "reranker mostly confirms"
    findings: list[VectorEfficiencyFinding] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TopKStabilityReport:
    window_days: int
    k: int
    model: str | None
    retrieval_index: str | None
    query_count: int
    mean_jaccard: float
    p25_jaccard: float
    p75_jaccard: float
    findings: list[VectorEfficiencyFinding] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RerankScoreDistributionReport:
    window_days: int
    model: str | None
    event_count: int
    histogram: list[int]
    bucket_edges: list[float]
    top_decile_share: float
    findings: list[VectorEfficiencyFinding] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NDCGReport:
    window_days: int
    k: int
    model: str | None
    retrieval_index: str | None
    labeled_query_count: int
    ndcg_at_k_under_bi_order: float
    ndcg_at_k_under_cross_order: float
    delta: float  # cross - bi
    findings: list[VectorEfficiencyFinding] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RerankRecommendation:
    """Roll-up advisor — pulls in the four analytics and picks the
    most actionable finding for the window."""

    window_days: int
    retrieval_index: str | None
    summary: str
    findings: list[VectorEfficiencyFinding]


# --- shared SQL fragment + helpers ----------------------------------------


def _validate_optional_text(name: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise VectorEfficiencyError(f"{name} must be a non-empty string or None; got {value!r}")


def _validate_window(days: int) -> None:
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0 or days > 365:
        raise VectorEfficiencyError(f"window days must be an int in [1..365]; got {days!r}")


def _validate_k(k: int) -> None:
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0 or k > 1000:
        raise VectorEfficiencyError(f"k must be an int in [1..1000]; got {k!r}")


def _build_where_and_params(
    days: int, model: str | None, retrieval_index: str | None, *, require_labeled: bool = False
) -> tuple[str, list[Any]]:
    """Compose the WHERE clause + params for an analytics query.

    Window goes first (always present); model + retrieval_index are
    optional. ``require_labeled`` adds the
    ``ground_truth_relevance IS NOT NULL`` predicate the NDCG analytic
    needs.
    """
    parts = ["occurred_at >= now() - make_interval(days => %s)"]
    params: list[Any] = [days]
    if model is not None:
        parts.append("reranker_model = %s")
        params.append(model)
    if retrieval_index is not None:
        parts.append("retrieval_index = %s")
        params.append(retrieval_index)
    if require_labeled:
        parts.append("ground_truth_relevance IS NOT NULL")
    return " AND ".join(parts), params


async def _events_table_exists(driver: SqlDriver) -> bool:
    rows = await driver.execute_query(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'mcpg_rag' AND c.relname = 'rerank_events' AND c.relkind = 'r'",
        force_readonly=True,
    )
    return bool(rows)


# --- analyze_reranker_lift ------------------------------------------------


async def analyze_reranker_lift(
    driver: SqlDriver,
    *,
    days: int = _DEFAULT_WINDOW_DAYS,
    model: str | None = None,
    retrieval_index: str | None = None,
) -> RerankerLiftReport:
    """Per-query Spearman + Kendall correlation between bi and cross ranks.

    Aggregated across queries in the window. Low correlation = the
    reranker is actively reordering (doing real work). High
    correlation = the reranker mostly confirms the bi-encoder order
    (its compute is being spent).
    """
    _validate_window(days)
    _validate_optional_text("model", model)
    _validate_optional_text("retrieval_index", retrieval_index)
    if not await _events_table_exists(driver):
        return RerankerLiftReport(
            window_days=days,
            model=model,
            retrieval_index=retrieval_index,
            query_count=0,
            mean_spearman=0.0,
            mean_kendall=0.0,
            p25_spearman=0.0,
            p75_spearman=0.0,
            interpretation="no events table",
        )
    where_sql, params = _build_where_and_params(days, model, retrieval_index)
    sql = (
        "SELECT query_hash, bi_encoder_rank, cross_encoder_rank "
        f"FROM mcpg_rag.rerank_events WHERE {where_sql} "
        "ORDER BY query_hash, bi_encoder_rank"
    )
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    per_query: dict[bytes, list[tuple[int, int]]] = {}
    for row in rows or []:
        per_query.setdefault(bytes(row.cells["query_hash"]), []).append(
            (int(row.cells["bi_encoder_rank"]), int(row.cells["cross_encoder_rank"]))
        )
    spearmans: list[float] = []
    kendalls: list[float] = []
    for pairs in per_query.values():
        if len(pairs) < 2:
            continue
        xs = [float(p[0]) for p in pairs]
        ys = [float(p[1]) for p in pairs]
        spearmans.append(_spearman(xs, ys))
        kendalls.append(_kendall_tau(xs, ys))
    if not spearmans:
        return RerankerLiftReport(
            window_days=days,
            model=model,
            retrieval_index=retrieval_index,
            query_count=0,
            mean_spearman=0.0,
            mean_kendall=0.0,
            p25_spearman=0.0,
            p75_spearman=0.0,
            interpretation="insufficient data",
        )
    mean_spearman = sum(spearmans) / len(spearmans)
    mean_kendall = sum(kendalls) / len(kendalls)
    p25 = _percentile(spearmans, 0.25)
    p75 = _percentile(spearmans, 0.75)
    findings: list[VectorEfficiencyFinding] = []
    if mean_kendall > _THRESHOLD_RERANKER_IDLE_KENDALL:
        findings.append(
            VectorEfficiencyFinding(
                code="reranker_idle",
                severity="WARNING",
                evidence=(
                    f"mean Kendall tau={mean_kendall:.3f} > {_THRESHOLD_RERANKER_IDLE_KENDALL} — the reranker "
                    f"rarely changes ordering across {len(spearmans)} queries."
                ),
                suggested_action=(
                    "Consider skipping the reranker at this K and saving the latency. Likely candidates: "
                    "shorter inputs, simple lookups, queries where bi-encoder confidence is already high."
                ),
            )
        )
    interpretation = (
        "reranker mostly confirms" if mean_kendall > _THRESHOLD_RERANKER_IDLE_KENDALL else "reranker actively reorders"
    )
    return RerankerLiftReport(
        window_days=days,
        model=model,
        retrieval_index=retrieval_index,
        query_count=len(spearmans),
        mean_spearman=mean_spearman,
        mean_kendall=mean_kendall,
        p25_spearman=p25,
        p75_spearman=p75,
        interpretation=interpretation,
        findings=findings,
    )


# --- analyze_topk_stability -----------------------------------------------


async def analyze_topk_stability(
    driver: SqlDriver,
    *,
    days: int = _DEFAULT_WINDOW_DAYS,
    k: int = 10,
    model: str | None = None,
    retrieval_index: str | None = None,
) -> TopKStabilityReport:
    """Jaccard overlap between top-K-by-bi-rank and top-K-by-cross-rank.

    High mean Jaccard means the reranker isn't actually changing the
    top-K membership — the same items are returned, just in a
    different order.
    """
    _validate_window(days)
    _validate_k(k)
    _validate_optional_text("model", model)
    _validate_optional_text("retrieval_index", retrieval_index)
    if not await _events_table_exists(driver):
        return TopKStabilityReport(
            window_days=days,
            k=k,
            model=model,
            retrieval_index=retrieval_index,
            query_count=0,
            mean_jaccard=0.0,
            p25_jaccard=0.0,
            p75_jaccard=0.0,
        )
    where_sql, params = _build_where_and_params(days, model, retrieval_index)
    sql = (
        "SELECT query_hash, candidate_id, bi_encoder_rank, cross_encoder_rank "
        f"FROM mcpg_rag.rerank_events WHERE {where_sql}"
    )
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    per_query: dict[bytes, list[tuple[int, int, int]]] = {}
    for row in rows or []:
        per_query.setdefault(bytes(row.cells["query_hash"]), []).append(
            (
                int(row.cells["candidate_id"]),
                int(row.cells["bi_encoder_rank"]),
                int(row.cells["cross_encoder_rank"]),
            )
        )
    jaccards: list[float] = []
    for items in per_query.values():
        top_bi = [c for c, b, _ in sorted(items, key=lambda t: t[1])[:k]]
        top_cross = [c for c, _, x in sorted(items, key=lambda t: t[2])[:k]]
        jaccards.append(_jaccard(top_bi, top_cross))
    if not jaccards:
        return TopKStabilityReport(
            window_days=days,
            k=k,
            model=model,
            retrieval_index=retrieval_index,
            query_count=0,
            mean_jaccard=0.0,
            p25_jaccard=0.0,
            p75_jaccard=0.0,
        )
    mean = sum(jaccards) / len(jaccards)
    p25 = _percentile(jaccards, 0.25)
    p75 = _percentile(jaccards, 0.75)
    findings: list[VectorEfficiencyFinding] = []
    if mean > _THRESHOLD_TOPK_STABLE_JACCARD:
        findings.append(
            VectorEfficiencyFinding(
                code="topk_stable",
                severity="WARNING",
                evidence=(
                    f"mean Jaccard={mean:.3f} > {_THRESHOLD_TOPK_STABLE_JACCARD} at k={k}: the same items appear "
                    f"in the top-{k} before and after rerank across {len(jaccards)} queries."
                ),
                suggested_action=(
                    "Rerank is barely earning its place at this K. Skip the reranker for queries you can pre-"
                    "classify as 'easy', or shrink K so the reranker has fewer no-ops."
                ),
            )
        )
    return TopKStabilityReport(
        window_days=days,
        k=k,
        model=model,
        retrieval_index=retrieval_index,
        query_count=len(jaccards),
        mean_jaccard=mean,
        p25_jaccard=p25,
        p75_jaccard=p75,
        findings=findings,
    )


# --- analyze_rerank_score_distribution ------------------------------------


async def analyze_rerank_score_distribution(
    driver: SqlDriver,
    *,
    days: int = _DEFAULT_WINDOW_DAYS,
    model: str | None = None,
    n_buckets: int = 20,
) -> RerankScoreDistributionReport:
    """Histogram of cross_encoder_score values + top-decile share.

    Clustering at the top of the range is a known failure mode for
    some commercial rerankers (e.g. half the scores all fall in
    [0.9, 1.0]) — they're not discriminating.
    """
    _validate_window(days)
    _validate_optional_text("model", model)
    if not isinstance(n_buckets, int) or isinstance(n_buckets, bool) or n_buckets <= 0 or n_buckets > 100:
        raise VectorEfficiencyError(f"n_buckets must be an int in [1..100]; got {n_buckets!r}")
    if not await _events_table_exists(driver):
        return RerankScoreDistributionReport(
            window_days=days,
            model=model,
            event_count=0,
            histogram=[0] * n_buckets,
            bucket_edges=[],
            top_decile_share=0.0,
        )
    where_sql, params = _build_where_and_params(days, model, None)
    sql = f"SELECT cross_encoder_score AS score FROM mcpg_rag.rerank_events WHERE {where_sql} ORDER BY score"
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    values = [float(row.cells["score"]) for row in rows or []]
    if not values:
        return RerankScoreDistributionReport(
            window_days=days,
            model=model,
            event_count=0,
            histogram=[0] * n_buckets,
            bucket_edges=[],
            top_decile_share=0.0,
        )
    histogram = _histogram(values, n_buckets)
    lo, hi = min(values), max(values)
    # Bucket edges: n_buckets+1 numbers from lo to hi.
    width = (hi - lo) / n_buckets if hi != lo else 0.0
    edges = [lo + i * width for i in range(n_buckets + 1)]
    # Top decile (last 10% of buckets) — for n_buckets=20 that's the
    # last 2 buckets.
    top_decile_count = sum(histogram[-max(1, n_buckets // 10) :])
    top_decile_share = top_decile_count / len(values)
    findings: list[VectorEfficiencyFinding] = []
    if top_decile_share > _THRESHOLD_SCORE_CLUSTERING_TOP_DECILE:
        findings.append(
            VectorEfficiencyFinding(
                code="score_clustering",
                severity="WARNING",
                evidence=(
                    f"{top_decile_share:.0%} of rerank scores fall in the top decile of the range — the "
                    "reranker isn't discriminating. Common with some commercial models when inputs are "
                    "too short or too similar."
                ),
                suggested_action=(
                    "Try a different reranker model, calibrate the scores (e.g. Platt scaling on a labeled "
                    "set), or feed longer/more distinctive inputs."
                ),
            )
        )
    return RerankScoreDistributionReport(
        window_days=days,
        model=model,
        event_count=len(values),
        histogram=histogram,
        bucket_edges=edges,
        top_decile_share=top_decile_share,
        findings=findings,
    )


# --- analyze_rerank_ndcg --------------------------------------------------


async def analyze_rerank_ndcg(
    driver: SqlDriver,
    *,
    days: int = _DEFAULT_WINDOW_DAYS,
    k: int = 10,
    model: str | None = None,
    retrieval_index: str | None = None,
) -> NDCGReport:
    """NDCG@k under bi-encoder ordering vs cross-encoder ordering.

    Gated on ``ground_truth_relevance IS NOT NULL`` — only labeled
    rows count. The delta (cross - bi) is the actual lift the
    reranker provides on labeled data.
    """
    _validate_window(days)
    _validate_k(k)
    _validate_optional_text("model", model)
    _validate_optional_text("retrieval_index", retrieval_index)
    if not await _events_table_exists(driver):
        return NDCGReport(
            window_days=days,
            k=k,
            model=model,
            retrieval_index=retrieval_index,
            labeled_query_count=0,
            ndcg_at_k_under_bi_order=0.0,
            ndcg_at_k_under_cross_order=0.0,
            delta=0.0,
        )
    where_sql, params = _build_where_and_params(days, model, retrieval_index, require_labeled=True)
    sql = (
        "SELECT query_hash, bi_encoder_rank, cross_encoder_rank, ground_truth_relevance "
        f"FROM mcpg_rag.rerank_events WHERE {where_sql}"
    )
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    per_query: dict[bytes, list[tuple[int, int, int]]] = {}
    for row in rows or []:
        grade = row.cells.get("ground_truth_relevance")
        if grade is None:
            # WHERE clause filters NULL grades, but defensively skip
            # any that slip through (e.g. drivers that don't apply WHERE).
            continue
        per_query.setdefault(bytes(row.cells["query_hash"]), []).append(
            (
                int(row.cells["bi_encoder_rank"]),
                int(row.cells["cross_encoder_rank"]),
                int(grade),
            )
        )
    bi_scores: list[float] = []
    cross_scores: list[float] = []
    for items in per_query.values():
        # Grades in bi-rank order:
        bi_order = sorted(items, key=lambda t: t[0])
        bi_grades = [float(g) for _, _, g in bi_order]
        cross_order = sorted(items, key=lambda t: t[1])
        cross_grades = [float(g) for _, _, g in cross_order]
        bi_scores.append(_ndcg_at_k(bi_grades, k))
        cross_scores.append(_ndcg_at_k(cross_grades, k))
    if not bi_scores:
        return NDCGReport(
            window_days=days,
            k=k,
            model=model,
            retrieval_index=retrieval_index,
            labeled_query_count=0,
            ndcg_at_k_under_bi_order=0.0,
            ndcg_at_k_under_cross_order=0.0,
            delta=0.0,
        )
    bi_mean = sum(bi_scores) / len(bi_scores)
    cross_mean = sum(cross_scores) / len(cross_scores)
    delta = cross_mean - bi_mean
    findings: list[VectorEfficiencyFinding] = []
    if delta < _THRESHOLD_NDCG_HURTS_DELTA:
        findings.append(
            VectorEfficiencyFinding(
                code="rerank_hurts_ndcg",
                severity="CRITICAL",
                evidence=(
                    f"NDCG@{k} drops by {delta:.3f} (bi={bi_mean:.3f} → cross={cross_mean:.3f}) across "
                    f"{len(bi_scores)} labeled queries. The reranker is making it worse."
                ),
                suggested_action=(
                    "Investigate model/version/prompt regression on labeled traffic. Roll back the most "
                    "recent reranker change or A/B the current model against a known-good baseline."
                ),
            )
        )
    elif delta > _THRESHOLD_NDCG_LIFTS_DELTA:
        findings.append(
            VectorEfficiencyFinding(
                code="rerank_lifts_ndcg",
                severity="GOOD",
                evidence=(
                    f"NDCG@{k} lifts by {delta:.3f} (bi={bi_mean:.3f} → cross={cross_mean:.3f}) across "
                    f"{len(bi_scores)} labeled queries. The reranker is doing real work."
                ),
                suggested_action="No change needed — track this baseline to detect drift.",
            )
        )
    return NDCGReport(
        window_days=days,
        k=k,
        model=model,
        retrieval_index=retrieval_index,
        labeled_query_count=len(bi_scores),
        ndcg_at_k_under_bi_order=bi_mean,
        ndcg_at_k_under_cross_order=cross_mean,
        delta=delta,
        findings=findings,
    )


# --- recommend_rerank_strategy --------------------------------------------


async def recommend_rerank_strategy(
    driver: SqlDriver,
    *,
    days: int = _DEFAULT_WINDOW_DAYS,
    retrieval_index: str | None = None,
) -> RerankRecommendation:
    """Roll-up advisor over the four analytics for one window.

    Returns a single recommendation built from whichever finding has
    the most actionable signal. The plan calls out three end-states:

    * Reranker is theatre (topk_stable + reranker_idle both fire).
    * Reranker is hurting (rerank_hurts_ndcg fires).
    * Reranker is critical / healthy (rerank_lifts_ndcg + no
      stability findings).
    """
    _validate_window(days)
    _validate_optional_text("retrieval_index", retrieval_index)
    lift = await analyze_reranker_lift(driver, days=days, retrieval_index=retrieval_index)
    stability = await analyze_topk_stability(driver, days=days, retrieval_index=retrieval_index)
    distribution = await analyze_rerank_score_distribution(driver, days=days)
    ndcg = await analyze_rerank_ndcg(driver, days=days, retrieval_index=retrieval_index)

    findings: list[VectorEfficiencyFinding] = []
    findings.extend(lift.findings)
    findings.extend(stability.findings)
    findings.extend(distribution.findings)
    findings.extend(ndcg.findings)

    # Pick the headline message based on which combination fired.
    codes = {f.code for f in findings}
    if "rerank_hurts_ndcg" in codes:
        summary = "Reranker is making NDCG worse on labeled data — investigate immediately."
    elif "reranker_idle" in codes and "topk_stable" in codes:
        summary = (
            "Reranker is theatre at this K — top-K is stable and ranks are mostly preserved. "
            "Consider skipping the reranker for queries you can pre-classify as easy."
        )
    elif "rerank_lifts_ndcg" in codes:
        summary = "Reranker is doing real work — NDCG lift is significant. Keep it."
    elif "score_clustering" in codes:
        summary = "Reranker scores cluster at the top of the range — not discriminating. Calibrate or switch model."
    elif not findings:
        summary = "No actionable findings in this window — reranker pipeline looks healthy."
    else:
        summary = "Mixed signals — review the individual analytics."

    return RerankRecommendation(
        window_days=days,
        retrieval_index=retrieval_index,
        summary=summary,
        findings=findings,
    )


# --- audit_rag_pipeline ---------------------------------------------------


async def audit_rag_pipeline(driver: SqlDriver) -> Any:
    """Scorecard adapter for the RAG reranker pipeline.

    Returns ``None`` when ``mcpg_rag.rerank_events`` doesn't exist
    (the common case until the caller opts in by running
    ``setup_rag_telemetry``). When the table is there, runs
    :func:`recommend_rerank_strategy` over the default 7-day window
    and surfaces each finding as a :class:`MetricResult`.
    """
    from mcpg.audit import CategoryResult, MetricResult

    if not await _events_table_exists(driver):
        return None
    recommendation = await recommend_rerank_strategy(driver)
    score = 100
    metrics: list[MetricResult] = []
    for finding in recommendation.findings:
        score -= _SEVERITY_DEDUCTION.get(finding.severity, 0)
        metrics.append(
            MetricResult(
                name=f"rag_pipeline:{finding.code}",
                value=finding.code,
                unit="finding",
                target="no findings",
                status=finding.severity,
                severity=3 if finding.severity == "CRITICAL" else 2 if finding.severity == "WARNING" else 0,
                evidence=finding.evidence,
                suggestion=finding.suggested_action,
            )
        )
    if not metrics:
        metrics.append(
            MetricResult(
                name="rag_pipeline:no_findings",
                value="ok",
                unit="finding",
                target="no findings",
                status="GOOD",
                severity=0,
                evidence=recommendation.summary,
                suggestion="",
            )
        )
    score = max(0, score)
    status_label = "GOOD" if score >= 90 else ("WARNING" if score >= 70 else "CRITICAL")
    return CategoryResult(
        category="RAG Reranker Pipeline",
        status=status_label,
        score=score,
        metrics=metrics,
    )
