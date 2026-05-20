"""Integration tests for database health checks against a live PostgreSQL."""

from mcpg.database import Database
from mcpg.health import check_database_health


async def test_check_database_health_against_real_postgres(connected_database: Database) -> None:
    report = await check_database_health(connected_database.driver())

    assert report.status in {"ok", "warning"}
    assert {check.name for check in report.checks} == {
        "connections",
        "cache_hit_ratio",
        "dead_tuples",
        "invalid_indexes",
    }
    # A freshly created test database should have no invalid indexes.
    invalid = next(check for check in report.checks if check.name == "invalid_indexes")
    assert invalid.status == "ok"
