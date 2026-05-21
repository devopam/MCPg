"""Index recommendations from table scan statistics.

This is a deliberately simple table-level heuristic: it flags large tables
that are read mostly by sequential scans. Choosing *which columns* to index
needs query analysis (see ``analyze_workload`` and ``explain_query``).
Index-type-aware recommendations (GIN, trigram, BRIN, ...) arrive in Phase 8.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# Tables smaller than this are ignored — sequential scans of them are cheap.
DEFAULT_MIN_LIVE_TUPLES = 10_000


@dataclass(frozen=True, slots=True)
class IndexRecommendation:
    """A table that may benefit from an index."""

    schema: str
    table: str
    seq_scans: int
    live_tuples: int
    reason: str


async def recommend_indexes(
    driver: SqlDriver, *, min_live_tuples: int = DEFAULT_MIN_LIVE_TUPLES
) -> list[IndexRecommendation]:
    """Recommend tables that may benefit from indexing.

    Heuristic: large tables (at least ``min_live_tuples`` rows) read more
    often by sequential scan than by index scan.

    Args:
        driver: The SQL driver to query through.
        min_live_tuples: Smallest table (row estimate) worth flagging.
    """
    rows = await driver.execute_query(
        "SELECT schemaname, relname, seq_scan, n_live_tup FROM pg_stat_user_tables "
        "WHERE n_live_tup >= %s AND seq_scan > COALESCE(idx_scan, 0) "
        "ORDER BY seq_scan DESC",
        params=[min_live_tuples],
        force_readonly=True,
    )
    return [
        IndexRecommendation(
            schema=row.cells["schemaname"],
            table=row.cells["relname"],
            seq_scans=row.cells["seq_scan"],
            live_tuples=row.cells["n_live_tup"],
            reason="large table read mostly by sequential scan",
        )
        for row in rows or []
    ]
