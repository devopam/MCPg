"""Integration tests for workload analysis against a live PostgreSQL."""

from mcpg.database import Database
from mcpg.workload import analyze_workload


async def test_analyze_workload_against_real_postgres(connected_database: Database) -> None:
    report = await analyze_workload(connected_database.driver())

    # pg_stat_statements may or may not be installed in the test database;
    # either way the call must succeed and return a coherent report.
    if report.available:
        assert isinstance(report.slow_queries, list)
    else:
        assert report.slow_queries == []
