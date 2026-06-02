"""Advanced pgvector tuning diagnostics.

Provides HNSW recall and latency sweeping analysis against exact brute-force
ground truth (exact k-NN) for a given query vector.
"""

from __future__ import annotations

import time
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed
from mcpg.vector_tuning import VectorTuningError, _quoted

_DISTANCE_OPERATORS = {"l2": "<->", "cosine": "<=>", "inner_product": "<#>"}


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
