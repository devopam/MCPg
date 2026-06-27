"""Advanced pgvector tuning diagnostics.

Provides HNSW recall and latency sweeping analysis against exact brute-force
ground truth (exact k-NN) for a given query vector.

Two tools:

* ``analyze_hnsw_recall(... query_vector ...)`` — sweeps ``ef_search`` for
  a SINGLE caller-supplied query vector and returns the raw
  recall / latency curve as a list of dicts. Cheap, exploratory.
* ``recommend_hnsw_ef_search(...)`` — the actionable advisor (roadmap
  9.1). Samples MANY query vectors from the table, averages recall@k
  per ``ef_search`` with p50 / p95 latency, verifies an HNSW index
  actually exists on the column (the single-query tool can't tell a
  real index from a sequential scan), and recommends the smallest
  ``ef_search`` that clears a target recall. Returns a typed dataclass.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed
from mcpg.vector_tuning import VectorTuningError, _indexes_on_column, _quoted

_DISTANCE_OPERATORS = {"l2": "<->", "cosine": "<=>", "inner_product": "<#>"}
_DISTANCE_FUNCTIONS = {"l2": "l2_distance", "cosine": "cosine_distance", "inner_product": "inner_product"}

# Default ef_search sweep — the geometric ladder pgvector operators
# reach for first. Callers can override.
_DEFAULT_EF_VALUES = (16, 32, 64, 128, 256)

# Sampling 2N+1 queries per ef value; cap N so a runaway argument
# can't DoS the database.
_MAX_SAMPLE_QUERIES = 50


async def _ensure_installed(driver: SqlDriver) -> None:
    if not await extension_installed(driver, "vector"):
        raise VectorTuningError("vector extension is not installed in this database")


async def _detect_primary_key(driver: SqlDriver, schema: str, table: str) -> str:
    """Find the primary key column name of the table using the catalog."""
    rows = await driver.execute_query(
        "SELECT a.attname AS pk_column "
        "FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary = true",
        params=[schema, table],
        force_readonly=True,
    )
    if rows:
        return str(rows[0].cells["pk_column"])
    return "id"  # fallback


async def analyze_hnsw_recall(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    query_vector: list[float] | str,
    *,
    k: int = 10,
    metric: str = "l2",
) -> list[dict[str, Any]]:
    """Sweeps ef_search values to measure the latency and recall trade-off curve.

    Computes exact k-NN ground truth by disabling index scans locally, then
    probes typical `hnsw.ef_search` values to output a speed/recall curve.
    """
    await _ensure_installed(driver)

    if metric not in _DISTANCE_OPERATORS:
        raise VectorTuningError(f"unknown metric {metric!r}; expected l2, cosine, or inner_product")
    if k <= 0:
        raise VectorTuningError("k must be positive")

    # Format vector representation
    if isinstance(query_vector, list):
        query_vector_str = "[" + ",".join(str(x) for x in query_vector) + "]"
    else:
        query_vector_str = str(query_vector)

    id_column = await _detect_primary_key(driver, schema, table)

    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    id_col = _quoted(id_column, "id_column")
    operator = _DISTANCE_OPERATORS[metric]

    # 1. Compute ground truth (exact k-NN) inside a transaction with index scans disabled
    truth_rows = await driver.execute_query(
        f"SET LOCAL enable_indexscan = off; "
        f"SELECT {id_col} AS id FROM {relation} ORDER BY {col} {operator} %s::vector LIMIT %s",
        params=[query_vector_str, k],
        force_readonly=True,
    )
    truth_ids = {row.cells["id"] for row in truth_rows or []}
    if not truth_ids:
        # No vectors or empty table, return empty sweep curve
        return []

    # 2. Sweep typical ef_search values
    ef_values = [16, 32, 64, 128, 256]
    curve = []

    for ef in ef_values:
        start_time = time.monotonic()
        approx_rows = await driver.execute_query(
            f"SET LOCAL enable_indexscan = on; "
            f"SET LOCAL hnsw.ef_search = {ef}; "
            f"SELECT {id_col} AS id FROM {relation} ORDER BY {col} {operator} %s::vector LIMIT %s",
            params=[query_vector_str, k],
            force_readonly=True,
        )
        latency_ms = (time.monotonic() - start_time) * 1000.0

        approx_ids = {row.cells["id"] for row in approx_rows or []}
        recall = len(truth_ids & approx_ids) / len(truth_ids) if truth_ids else 0.0

        curve.append(
            {
                "ef_search": ef,
                "recall": recall,
                "latency_ms": round(latency_ms, 3),
            }
        )

    return curve


# ---------------------------------------------------------------------------
# recommend_hnsw_ef_search — roadmap 9.1 (multi-query advisor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EfSearchSweepPoint:
    """One ``ef_search`` value's measured behaviour.

    ``mean_recall_at_k`` is averaged across every sampled query vector.
    ``p50_latency_ms`` / ``p95_latency_ms`` are the per-query latency
    percentiles at this knob value. ``meets_target`` is whether
    ``mean_recall_at_k`` cleared the requested ``target_recall``.
    """

    ef_search: int
    mean_recall_at_k: float
    p50_latency_ms: float
    p95_latency_ms: float
    meets_target: bool


@dataclass(frozen=True, slots=True)
class HnswRecallRecommendation:
    """Roll-up of :func:`recommend_hnsw_ef_search`.

    ``available`` is ``False`` only when pgvector itself is absent.
    ``has_hnsw_index`` distinguishes "swept a real HNSW index" from
    "no HNSW index on this column, so the sweep would just be measuring
    sequential scans" — the single-query :func:`analyze_hnsw_recall`
    can't tell these apart and silently reports recall 1.0.
    ``recommended_ef_search`` is the smallest swept value whose mean
    recall met ``target_recall``, or ``None`` when none did (widen the
    sweep or rebuild the index with a larger ``ef_construction`` /
    ``m``).
    """

    available: bool
    has_hnsw_index: bool
    index_name: str | None
    metric: str
    k: int
    target_recall: float
    sample_queries: int
    recommended_ef_search: int | None
    sweep: list[EfSearchSweepPoint] = field(default_factory=list)
    detail: str = ""


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of ``values`` (0 < pct <= 100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    # nearest-rank: ceil(pct/100 * N) - 1, clamped into range.
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[rank]


async def recommend_hnsw_ef_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    *,
    k: int = 10,
    target_recall: float = 0.95,
    sample_queries: int = 10,
    metric: str = "l2",
    ef_values: tuple[int, ...] = _DEFAULT_EF_VALUES,
) -> HnswRecallRecommendation:
    """Recommend an ``hnsw.ef_search`` for a target recall@k.

    Samples ``sample_queries`` rows (in id order) as query vectors,
    builds an exact brute-force top-k ground truth per query (via the
    pgvector distance *function*, which the planner does not route to
    the ANN index), then sweeps ``ef_values`` measuring mean recall@k
    and p50 / p95 latency at each. Recommends the smallest swept value
    whose mean recall clears ``target_recall``.

    The query row is excluded from both ground truth and approximate
    results (a row's own vector is its nearest neighbour at distance 0,
    which would inflate recall).

    Raises:
        VectorTuningError: pgvector absent, unknown ``metric``, or an
            out-of-range numeric argument.
    """
    if not await extension_installed(driver, "vector"):
        return HnswRecallRecommendation(
            available=False,
            has_hnsw_index=False,
            index_name=None,
            metric=metric,
            k=k,
            target_recall=target_recall,
            sample_queries=0,
            recommended_ef_search=None,
            sweep=[],
            detail="vector extension is not installed in this database.",
        )
    if metric not in _DISTANCE_OPERATORS:
        raise VectorTuningError(f"unknown metric {metric!r}; expected l2, cosine, or inner_product")
    if k <= 0:
        raise VectorTuningError("k must be positive")
    if not (0.0 < target_recall <= 1.0):
        raise VectorTuningError(f"target_recall must be in (0, 1]; got {target_recall}")
    if sample_queries <= 0:
        raise VectorTuningError("sample_queries must be positive")
    if sample_queries > _MAX_SAMPLE_QUERIES:
        raise VectorTuningError(f"sample_queries cannot exceed {_MAX_SAMPLE_QUERIES}")
    if not ef_values:
        raise VectorTuningError("ef_values must contain at least one value")
    if any(ef <= 0 for ef in ef_values):
        raise VectorTuningError("every ef_search value must be positive")

    operator = _DISTANCE_OPERATORS[metric]
    function = _DISTANCE_FUNCTIONS[metric]
    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    id_column = await _detect_primary_key(driver, schema, table)
    id_col = _quoted(id_column, "id_column")

    # Detect whether an HNSW index actually backs this column. Without
    # one the sweep measures sequential scans and every recall is 1.0 —
    # we surface that rather than hand back a misleading curve.
    hnsw_indexes = [
        ix for ix in await _indexes_on_column(driver, schema, table, column) if ix["index_method"] == "hnsw"
    ]
    index_name = hnsw_indexes[0]["index_name"] if hnsw_indexes else None

    sample_rows = await driver.execute_query(
        f"SELECT {id_col} AS id, {col}::text AS vec FROM {relation} WHERE {col} IS NOT NULL ORDER BY {id_col} LIMIT %s",
        params=[sample_queries],
        force_readonly=True,
    )
    samples = sample_rows or []

    if not index_name:
        return HnswRecallRecommendation(
            available=True,
            has_hnsw_index=False,
            index_name=None,
            metric=metric,
            k=k,
            target_recall=target_recall,
            sample_queries=len(samples),
            recommended_ef_search=None,
            sweep=[],
            detail=(
                f"No HNSW index on {schema}.{table}.{column}. ef_search only "
                "affects HNSW scans — create an HNSW index first "
                "(see tune_vector_index), then re-run."
            ),
        )

    if not samples:
        return HnswRecallRecommendation(
            available=True,
            has_hnsw_index=True,
            index_name=index_name,
            metric=metric,
            k=k,
            target_recall=target_recall,
            sample_queries=0,
            recommended_ef_search=None,
            sweep=[],
            detail=f"No non-null vectors found in {schema}.{table}.{column} to sample.",
        )

    # Pre-compute the exact ground truth per query once (it doesn't
    # depend on ef_search). Fetch k+1 and drop the query row itself.
    truths: list[tuple[Any, str, set[Any]]] = []
    for sample in samples:
        qid = sample.cells["id"]
        qvec = sample.cells["vec"]
        truth_rows = await driver.execute_query(
            f"SELECT {id_col} AS id FROM {relation} WHERE {id_col} <> %s "
            f"ORDER BY {function}({col}, %s::vector) LIMIT %s",
            params=[qid, qvec, k],
            force_readonly=True,
        )
        truth_ids = {row.cells["id"] for row in truth_rows or []}
        if truth_ids:
            truths.append((qid, qvec, truth_ids))

    sweep: list[EfSearchSweepPoint] = []
    recommended: int | None = None
    for ef in sorted(set(ef_values)):
        recalls: list[float] = []
        latencies: list[float] = []
        for qid, qvec, truth_ids in truths:
            start = time.monotonic()
            approx_rows = await driver.execute_query(
                f"SET LOCAL hnsw.ef_search = {int(ef)}; "
                f"SELECT {id_col} AS id FROM {relation} WHERE {id_col} <> %s "
                f"ORDER BY {col} {operator} %s::vector LIMIT %s",
                params=[qid, qvec, k],
                force_readonly=True,
            )
            latencies.append((time.monotonic() - start) * 1000.0)
            approx_ids = {row.cells["id"] for row in approx_rows or []}
            recalls.append(len(truth_ids & approx_ids) / len(truth_ids))

        mean_recall = sum(recalls) / len(recalls) if recalls else 0.0
        meets = mean_recall >= target_recall
        sweep.append(
            EfSearchSweepPoint(
                ef_search=int(ef),
                mean_recall_at_k=round(mean_recall, 4),
                p50_latency_ms=round(_percentile(latencies, 50), 3),
                p95_latency_ms=round(_percentile(latencies, 95), 3),
                meets_target=meets,
            )
        )
        if meets and recommended is None:
            recommended = int(ef)

    if recommended is not None:
        detail = (
            f"ef_search={recommended} is the smallest swept value clearing "
            f"recall@{k} >= {target_recall} (averaged over {len(truths)} "
            "query samples). Lower ef_search = faster but less accurate."
        )
    else:
        best = max(sweep, key=lambda p: p.mean_recall_at_k) if sweep else None
        best_txt = f" (best swept: ef_search={best.ef_search} at recall {best.mean_recall_at_k})" if best else ""
        detail = (
            f"No swept ef_search reached recall@{k} >= {target_recall}{best_txt}. "
            "Widen ef_values, or rebuild the HNSW index with a larger m / "
            "ef_construction for better attainable recall."
        )

    return HnswRecallRecommendation(
        available=True,
        has_hnsw_index=True,
        index_name=index_name,
        metric=metric,
        k=k,
        target_recall=target_recall,
        sample_queries=len(samples),
        recommended_ef_search=recommended,
        sweep=sweep,
        detail=detail,
    )
