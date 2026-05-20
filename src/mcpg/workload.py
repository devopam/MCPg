"""Workload analysis via the ``pg_stat_statements`` extension.

``analyze_workload`` surfaces the slowest queries by mean execution time. The
extension is optional; when it is not installed the report degrades
gracefully with ``available=False`` rather than failing.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# Default number of slow queries to return.
DEFAULT_LIMIT = 10


@dataclass(frozen=True, slots=True)
class SlowQuery:
    """One entry from ``pg_stat_statements``."""

    query: str
    calls: int
    mean_exec_ms: float
    total_exec_ms: float
    rows: int


@dataclass(frozen=True, slots=True)
class WorkloadReport:
    """The slowest queries, or a note that the extension is unavailable."""

    available: bool
    slow_queries: list[SlowQuery]


async def _extension_installed(driver: SqlDriver, name: str) -> bool:
    rows = await driver.execute_query(
        "SELECT 1 AS present FROM pg_extension WHERE extname = %s",
        params=[name],
        force_readonly=True,
    )
    return bool(rows)


async def analyze_workload(driver: SqlDriver, *, limit: int = DEFAULT_LIMIT) -> WorkloadReport:
    """Return the slowest queries by mean execution time.

    Requires the ``pg_stat_statements`` extension. When it is not installed
    the report is returned with ``available=False`` and no queries.

    Args:
        driver: The SQL driver to query through.
        limit: Maximum number of slow queries to return.
    """
    if not await _extension_installed(driver, "pg_stat_statements"):
        return WorkloadReport(available=False, slow_queries=[])

    rows = await driver.execute_query(
        "SELECT query, calls, mean_exec_time, total_exec_time, rows "
        "FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT %s",
        params=[limit],
        force_readonly=True,
    )
    slow_queries = [
        SlowQuery(
            query=row.cells["query"],
            calls=row.cells["calls"],
            mean_exec_ms=row.cells["mean_exec_time"],
            total_exec_ms=row.cells["total_exec_time"],
            rows=row.cells["rows"],
        )
        for row in rows or []
    ]
    return WorkloadReport(available=True, slow_queries=slow_queries)
