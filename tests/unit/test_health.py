"""Tests for database health checks and the check_database_health tool."""

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.health import (
    check_cache_hit_ratio,
    check_connections,
    check_database_health,
    check_dead_tuples,
    check_invalid_indexes,
    check_replication_lag,
    check_table_bloat,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- individual checks -----------------------------------------------------


async def test_check_connections_warns_above_the_threshold() -> None:
    ok = await check_connections(FakeDriver([{"used": 10, "max_connections": 100}]))
    high = await check_connections(FakeDriver([{"used": 95, "max_connections": 100}]))

    assert ok.status == "ok"
    assert high.status == "warning"


async def test_check_cache_hit_ratio_warns_on_a_low_ratio() -> None:
    good = await check_cache_hit_ratio(FakeDriver([{"hits": 999, "reads": 1}]))
    poor = await check_cache_hit_ratio(FakeDriver([{"hits": 50, "reads": 50}]))

    assert good.status == "ok"
    assert poor.status == "warning"


async def test_check_cache_hit_ratio_handles_an_idle_database() -> None:
    # A database with no block activity yet: sums are NULL / zero.
    result = await check_cache_hit_ratio(FakeDriver([{"hits": None, "reads": None}]))

    assert result.status == "ok"


async def test_check_dead_tuples_warns_when_tables_need_vacuuming() -> None:
    clean = await check_dead_tuples(FakeDriver([{"bloated": 0}]))
    bloated = await check_dead_tuples(FakeDriver([{"bloated": 4}]))

    assert clean.status == "ok"
    assert bloated.status == "warning"


async def test_check_invalid_indexes_warns_when_any_are_invalid() -> None:
    clean = await check_invalid_indexes(FakeDriver([{"invalid": 0}]))
    broken = await check_invalid_indexes(FakeDriver([{"invalid": 2}]))

    assert clean.status == "ok"
    assert broken.status == "warning"


async def test_check_replication_lag_is_ok_with_no_standbys() -> None:
    result = await check_replication_lag(FakeDriver([{"standbys": 0, "max_lag_bytes": 0}]))

    assert result.status == "ok"
    assert "no replication standbys" in result.detail


async def test_check_replication_lag_warns_on_a_lagging_standby() -> None:
    healthy = await check_replication_lag(FakeDriver([{"standbys": 2, "max_lag_bytes": 4096}]))
    lagging = await check_replication_lag(FakeDriver([{"standbys": 1, "max_lag_bytes": 256 * 1024 * 1024}]))

    assert healthy.status == "ok"
    assert lagging.status == "warning"


async def test_check_table_bloat_warns_when_tables_are_bloated() -> None:
    clean = await check_table_bloat(FakeDriver([{"bloated": 0}]))
    bloated = await check_table_bloat(FakeDriver([{"bloated": 5}]))

    assert clean.status == "ok"
    assert bloated.status == "warning"


# --- aggregate report ------------------------------------------------------

_HEALTHY_ROUTES: dict[str, list[dict[str, object]]] = {
    "pg_stat_activity": [{"used": 5, "max_connections": 100}],
    "pg_stat_database": [{"hits": 999, "reads": 1}],
    "pg_stat_user_tables": [{"bloated": 0}],
    "pg_index": [{"invalid": 0}],
    "pg_stat_replication": [{"standbys": 0, "max_lag_bytes": 0}],
    "table_stats": [{"bloated": 0}],
}


async def test_check_database_health_reports_ok_when_all_checks_pass() -> None:
    report = await check_database_health(FakeRoutingDriver(_HEALTHY_ROUTES))  # type: ignore[arg-type]

    assert report.status == "ok"
    assert {check.name for check in report.checks} == {
        "connections",
        "cache_hit_ratio",
        "dead_tuples",
        "invalid_indexes",
        "replication_lag",
        "table_bloat",
    }


async def test_check_database_health_reports_warning_when_a_check_fails() -> None:
    routes = {**_HEALTHY_ROUTES, "pg_index": [{"invalid": 3}]}

    report = await check_database_health(FakeRoutingDriver(routes))  # type: ignore[arg-type]

    assert report.status == "warning"


async def test_check_database_health_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver(_HEALTHY_ROUTES))  # type: ignore[arg-type]
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("check_database_health", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "ok"
