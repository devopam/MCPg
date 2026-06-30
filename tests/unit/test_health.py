"""Tests for database health checks and the check_database_health tool."""

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.health import (
    IndexBloat,
    TableBloat,
    TableBloatReport,
    analyze_table_bloat,
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


# --- analyze_table_bloat (roadmap 2.7) -------------------------------------


def _bloat_routes(
    *,
    tables: list[dict[str, object]],
    indexes: list[dict[str, object]],
    pgstattuple: bool = False,
    precise_tables: list[dict[str, object]] | None = None,
    precise_indexes: list[dict[str, object]] | None = None,
) -> FakeRoutingDriver:
    # Substrings are unique per query so the first-match router is
    # deterministic. Precise queries are matched ahead of the estimate ones
    # by their pgstattuple/pgstatindex call signatures.
    routes: dict[str, list[dict[str, object]]] = {
        "pg_extension": [{"present": 1}] if pgstattuple else [],
        "pgstattuple(t.relid)": precise_tables or [],
        "pgstatindex(idx.relid)": precise_indexes or [],
        "pg_total_relation_size(t.relid)": tables,
        "pg_index i ON i.indexrelid": indexes,
    }
    return FakeRoutingDriver(routes)


async def test_analyze_table_bloat_ranks_tables_and_indexes_worst_first() -> None:
    driver = _bloat_routes(
        tables=[
            {
                "schema": "public",
                "table": "low",
                "est_bloat_pct": 5.0,
                "dead_tuple_pct": 1.0,
                "n_dead_tup": 1,
                "n_live_tup": 100,
                "table_bytes": 1000,
            },
            {
                "schema": "public",
                "table": "high",
                "est_bloat_pct": 60.0,
                "dead_tuple_pct": 30.0,
                "n_dead_tup": 30,
                "n_live_tup": 100,
                "table_bytes": 9000,
            },
        ],
        indexes=[
            {"schema": "public", "table": "high", "index": "idx_a", "est_bloat_pct": 10.0, "index_bytes": 500},
            {"schema": "public", "table": "high", "index": "idx_b", "est_bloat_pct": 40.0, "index_bytes": 800},
        ],
    )

    report = await analyze_table_bloat(driver, "public")  # type: ignore[arg-type]

    assert report.available is True
    assert report.method == "estimate"
    assert [t.table for t in report.tables] == ["high", "low"]
    assert [i.index for i in report.indexes] == ["idx_b", "idx_a"]
    assert isinstance(report.tables[0], TableBloat)
    assert isinstance(report.indexes[0], IndexBloat)


async def test_analyze_table_bloat_caps_each_list_at_limit() -> None:
    driver = _bloat_routes(
        tables=[
            {
                "schema": "public",
                "table": f"t{n}",
                "est_bloat_pct": float(n),
                "dead_tuple_pct": 0.0,
                "n_dead_tup": 0,
                "n_live_tup": 1,
                "table_bytes": 1,
            }
            for n in range(5)
        ],
        indexes=[],
    )

    report = await analyze_table_bloat(driver, "public", limit=2)  # type: ignore[arg-type]

    assert len(report.tables) == 2
    # Worst-first: t4 (4.0) then t3 (3.0).
    assert [t.table for t in report.tables] == ["t4", "t3"]


async def test_analyze_table_bloat_empty_schema_is_available_with_empty_lists() -> None:
    driver = _bloat_routes(tables=[], indexes=[])

    report = await analyze_table_bloat(driver, "empty")  # type: ignore[arg-type]

    assert report == TableBloatReport(
        available=True,
        schema="empty",
        method="estimate",
        tables=[],
        indexes=[],
        detail="0 tables, 0 indexes analysed (estimate)",
    )


async def test_analyze_table_bloat_uses_pgstattuple_when_present_and_precise() -> None:
    driver = _bloat_routes(
        tables=[],
        indexes=[],
        pgstattuple=True,
        precise_tables=[
            {
                "schema": "public",
                "table": "t",
                "est_bloat_pct": 22.5,
                "dead_tuple_pct": 12.5,
                "n_dead_tup": 5,
                "n_live_tup": 40,
                "table_bytes": 4096,
            }
        ],
        precise_indexes=[
            {"schema": "public", "table": "t", "index": "t_pkey", "est_bloat_pct": 15.0, "index_bytes": 2048}
        ],
    )

    report = await analyze_table_bloat(driver, "public", precise=True)  # type: ignore[arg-type]

    assert report.method == "pgstattuple"
    assert report.tables[0].est_bloat_pct == 22.5
    assert report.indexes[0].index == "t_pkey"


async def test_analyze_table_bloat_falls_back_to_estimate_without_extension() -> None:
    driver = _bloat_routes(tables=[], indexes=[], pgstattuple=False)

    report = await analyze_table_bloat(driver, "public", precise=True)  # type: ignore[arg-type]

    assert report.method == "estimate"


async def test_analyze_table_bloat_reports_unavailable_on_driver_failure() -> None:
    report = await analyze_table_bloat(FakeDriver([], fail=True), "public")

    assert report.available is False
    assert "failed" in report.detail


async def test_analyze_table_bloat_rejects_non_positive_limit() -> None:
    import pytest

    with pytest.raises(ValueError, match="limit must be at least 1"):
        await analyze_table_bloat(FakeDriver([]), "public", limit=0)


async def test_analyze_table_bloat_tool_is_callable_from_a_client() -> None:
    driver = _bloat_routes(tables=[], indexes=[])
    database = FakeDatabase(driver)  # type: ignore[arg-type]
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("analyze_table_bloat", {"schema": "public"})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is True
    assert result.structuredContent["method"] == "estimate"
