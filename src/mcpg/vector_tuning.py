"""pgvector index-tuning advisors.

Two read-only tools that help agents make sensible pgvector choices:

* ``tune_vector_index`` recommends ``ivfflat`` or ``hnsw`` parameters
  from the live row count and column dimension, and emits a ready-to-run
  ``CREATE INDEX`` snippet.
* ``vector_recall_at_k`` probes an existing pgvector index by comparing
  its top-k results against a brute-force ground truth for the same
  query vectors, reporting mean recall@k.

Both require the ``vector`` extension; both raise :class:`VectorTuningError`
when it is absent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed
from mcpg.introspection import describe_table

# pgvector supports these index access methods today; the allowlist
# guards against arbitrary identifier injection in CREATE INDEX text.
_INDEX_TYPES = frozenset({"ivfflat", "hnsw"})

# Per pgvector docs: distance functions bypass the index in the planner,
# giving us a reliable brute-force baseline without SET LOCAL gymnastics.
_DISTANCE_FUNCTIONS = {"l2": "l2_distance", "cosine": "cosine_distance", "inner_product": "inner_product"}
# Their operator counterparts trigger the ANN index when one exists.
_DISTANCE_OPERATORS = {"l2": "<->", "cosine": "<=>", "inner_product": "<#>"}


class VectorTuningError(Exception):
    """Raised when a pgvector tuning operation cannot complete."""


@dataclass(frozen=True, slots=True)
class TuningRecommendation:
    """A recommended pgvector index configuration."""

    index_type: str
    parameters: dict[str, int]
    rationale: str
    create_index_sql: str
    row_count: int
    dimension: int


@dataclass(frozen=True, slots=True)
class RecallReport:
    """Recall@k probed against an existing pgvector index."""

    metric: str
    k: int
    sample_size: int
    mean_recall: float


async def _ensure_installed(driver: SqlDriver) -> None:
    if not await extension_installed(driver, "vector"):
        raise VectorTuningError("vector extension is not installed in this database")


async def _row_count(driver: SqlDriver, schema: str, table: str) -> int:
    rows = await driver.execute_query(
        # Catalog estimate from pg_class.reltuples — accurate enough for
        # tuning heuristics and orders of magnitude faster than COUNT(*).
        "SELECT GREATEST(c.reltuples, 0)::bigint AS estimate "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[schema, table],
        force_readonly=True,
    )
    if not rows:
        raise VectorTuningError(f"table {schema}.{table} not found")
    return int(rows[0].cells["estimate"])


async def _column_dimension(driver: SqlDriver, schema: str, table: str, column: str) -> int:
    columns = await describe_table(driver, schema, table)
    for info in columns:
        if info.name == column:
            if info.vector_dimension is None:
                raise VectorTuningError(f"column {schema}.{table}.{column} is not a pgvector vector(N)")
            return info.vector_dimension
    raise VectorTuningError(f"column {schema}.{table}.{column} not found")


def _recommend_ivfflat(row_count: int) -> tuple[dict[str, int], str]:
    # pgvector docs: start with rows/1000 up to 1M rows, sqrt(rows)
    # above 1M. Clamp at 100 — fewer lists than that gives little
    # benefit over a seqscan.
    if row_count > 1_000_000:
        lists = max(100, int(math.sqrt(row_count)))
        rationale = f"row_count={row_count:,} > 1M → lists = sqrt(rows) ≈ {lists}"
    else:
        lists = max(100, row_count // 1000)
        rationale = f"row_count={row_count:,} → lists = rows/1000, floored at 100 = {lists}"
    return {"lists": lists}, rationale


def _recommend_hnsw(row_count: int) -> tuple[dict[str, int], str]:
    # m controls graph degree (memory + recall trade-off); ef_construction
    # controls build-time quality. Both ramp with size.
    m = 16 if row_count <= 1_000_000 else 24
    ef_construction = 64 if row_count <= 100_000 else 128
    m_note = "baseline" if m == 16 else "denser graph for >1M rows"
    ef_note = "default" if ef_construction == 64 else "wider candidate pool for >100k rows"
    rationale = f"row_count={row_count:,} → m={m} ({m_note}), ef_construction={ef_construction} ({ef_note})"
    return {"m": m, "ef_construction": ef_construction}, rationale


def _format_create_index(
    index_type: str, schema: str, table: str, column: str, ops: str, parameters: dict[str, int]
) -> str:
    params_text = ", ".join(f"{name} = {value}" for name, value in parameters.items())
    return f"CREATE INDEX ON {schema}.{table} USING {index_type} ({column} {ops}) WITH ({params_text});"


_DEFAULT_OPS_FOR_METRIC = {"l2": "vector_l2_ops", "cosine": "vector_cosine_ops", "inner_product": "vector_ip_ops"}


async def tune_vector_index(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    *,
    index_type: str = "hnsw",
    metric: str = "l2",
) -> TuningRecommendation:
    """Recommend an ``ivfflat`` or ``hnsw`` configuration for a vector column.

    Reads the live row count (from ``pg_class.reltuples``) and column
    dimension, applies the standard pgvector heuristics, and returns the
    parameters plus a ready-to-run ``CREATE INDEX`` statement.

    Raises:
        VectorTuningError: pgvector is not installed, ``index_type`` or
            ``metric`` is not in the allowlist, or the column is not a
            ``vector(N)`` column.
    """
    await _ensure_installed(driver)
    if index_type not in _INDEX_TYPES:
        raise VectorTuningError(f"unsupported index_type {index_type!r}; expected one of {sorted(_INDEX_TYPES)}")
    if metric not in _DEFAULT_OPS_FOR_METRIC:
        raise VectorTuningError(f"unknown metric {metric!r}; expected l2, cosine, or inner_product")

    row_count = await _row_count(driver, schema, table)
    dimension = await _column_dimension(driver, schema, table, column)

    if index_type == "ivfflat":
        parameters, rationale = _recommend_ivfflat(row_count)
    else:
        parameters, rationale = _recommend_hnsw(row_count)

    ops = _DEFAULT_OPS_FOR_METRIC[metric]
    sql = _format_create_index(index_type, schema, table, column, ops, parameters)
    return TuningRecommendation(
        index_type=index_type,
        parameters=parameters,
        rationale=rationale,
        create_index_sql=sql,
        row_count=row_count,
        dimension=dimension,
    )


async def vector_recall_at_k(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    id_column: str,
    *,
    k: int = 10,
    sample_size: int = 20,
    metric: str = "l2",
) -> RecallReport:
    """Measure recall@k of an existing pgvector index against brute-force truth.

    Picks ``sample_size`` rows in id order, treats each row's vector as
    a query, runs the top-k search through the index (operator form,
    which the planner routes to the ANN index) and via the brute-force
    function form (``l2_distance`` / ``cosine_distance`` / ``inner_product``
    — pgvector documents these as non-indexed alternatives), and reports
    the mean overlap.

    Raises:
        VectorTuningError: pgvector is not installed, ``metric`` is
            unknown, or the table / id column does not exist.
    """
    await _ensure_installed(driver)
    if metric not in _DISTANCE_OPERATORS:
        raise VectorTuningError(f"unknown metric {metric!r}; expected l2, cosine, or inner_product")
    if k <= 0 or sample_size <= 0:
        raise VectorTuningError("k and sample_size must be positive")

    operator = _DISTANCE_OPERATORS[metric]
    function = _DISTANCE_FUNCTIONS[metric]

    sample_rows = await driver.execute_query(
        f"SELECT {id_column} AS id, {column}::text AS vec FROM {schema}.{table} "
        f"WHERE {column} IS NOT NULL ORDER BY {id_column} LIMIT %s",
        params=[sample_size],
        force_readonly=True,
    )
    samples = sample_rows or []
    if not samples:
        return RecallReport(metric=metric, k=k, sample_size=0, mean_recall=0.0)

    recalls: list[float] = []
    for sample in samples:
        query_vec = sample.cells["vec"]
        ann_rows = await driver.execute_query(
            f"SELECT {id_column} AS id FROM {schema}.{table} ORDER BY {column} {operator} %s::vector LIMIT %s",
            params=[query_vec, k],
            force_readonly=True,
        )
        truth_rows = await driver.execute_query(
            f"SELECT {id_column} AS id FROM {schema}.{table} ORDER BY {function}({column}, %s::vector) LIMIT %s",
            params=[query_vec, k],
            force_readonly=True,
        )
        ann_ids = {row.cells["id"] for row in ann_rows or []}
        truth_ids = {row.cells["id"] for row in truth_rows or []}
        if truth_ids:
            recalls.append(len(ann_ids & truth_ids) / len(truth_ids))

    mean = sum(recalls) / len(recalls) if recalls else 0.0
    return RecallReport(metric=metric, k=k, sample_size=len(samples), mean_recall=mean)
