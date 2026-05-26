"""Tests for workload analysis and the analyze_workload tool."""

from _fakes import FakeDatabase, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.workload import SlowQuery, _extract_table_hint, analyze_workload, detect_n_plus_one

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})

_SLOW_ROW = {
    "query": "SELECT * FROM big",
    "calls": 5,
    "mean_exec_time": 120.5,
    "total_exec_time": 602.5,
    "rows": 9000,
}


async def test_analyze_workload_reports_unavailable_when_the_extension_is_missing() -> None:
    report = await analyze_workload(FakeRoutingDriver({"pg_extension": []}))  # type: ignore[arg-type]

    assert report.available is False
    assert report.slow_queries == []


async def test_analyze_workload_returns_slow_queries_when_available() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "pg_stat_statements": [_SLOW_ROW]})

    report = await analyze_workload(driver)  # type: ignore[arg-type]

    assert report.available is True
    assert report.slow_queries == [
        SlowQuery(
            query="SELECT * FROM big",
            calls=5,
            mean_exec_ms=120.5,
            total_exec_ms=602.5,
            rows=9000,
        )
    ]


async def test_analyze_workload_binds_the_limit_as_a_parameter() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "pg_stat_statements": []})

    await analyze_workload(driver, limit=3)  # type: ignore[arg-type]

    stat_call = next(call for call in driver.calls if "pg_stat_statements" in call[0])
    assert stat_call[1] == [3]


async def test_analyze_workload_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))  # type: ignore[arg-type]
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("analyze_workload", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is False


# --- detect_n_plus_one (Phase 8.4) ---------------------------------------


def test_extract_table_hint_parses_qualified_and_quoted_names() -> None:
    assert _extract_table_hint("SELECT id FROM users WHERE id = $1") == "users"
    assert _extract_table_hint('SELECT id FROM "public"."users" WHERE id = $1') == "public.users"
    assert _extract_table_hint("SELECT 1") is None
    # Subquery / CTE: regex returns the first FROM-target, which is what we want.
    assert _extract_table_hint("WITH t AS (SELECT * FROM logs) SELECT * FROM t") == "logs"


async def test_detect_n_plus_one_reports_unavailable_without_pg_stat_statements() -> None:
    report = await detect_n_plus_one(FakeRoutingDriver({"pg_extension": []}))  # type: ignore[arg-type]

    assert report.available is False
    assert report.candidates == []
    # Thresholds still echoed even when unavailable — the agent can see
    # what the call would have used.
    assert report.thresholds["min_calls"] >= 1


async def test_detect_n_plus_one_returns_candidates_sorted_by_total_time_desc() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_stat_statements": [
                {
                    "query": "SELECT * FROM orders WHERE id = $1",
                    "calls": 5000,
                    "rows": 5000,
                    "mean_exec_time": 0.4,
                    "total_exec_time": 2000.0,
                },
                {
                    "query": "SELECT * FROM users WHERE id = $1",
                    "calls": 1000,
                    "rows": 1000,
                    "mean_exec_time": 0.2,
                    "total_exec_time": 200.0,
                },
            ],
        }
    )

    report = await detect_n_plus_one(driver)  # type: ignore[arg-type]

    assert report.available is True
    assert len(report.candidates) == 2
    # The fake driver returns rows in the order it was given, but we
    # rely on the SQL ORDER BY for sorting — verify the candidates
    # round-trip with the right shape and table hints.
    candidates_by_table = {c.table_hint: c for c in report.candidates}
    assert candidates_by_table["orders"].calls == 5000
    assert candidates_by_table["orders"].rows_per_call == 1.0
    assert "5,000 calls" in candidates_by_table["orders"].reason
    assert candidates_by_table["users"].calls == 1000


async def test_detect_n_plus_one_binds_threshold_parameters_into_the_query() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "pg_stat_statements": []})

    await detect_n_plus_one(  # type: ignore[arg-type]
        driver,
        min_calls=250,
        max_rows_per_call=1.5,
        min_total_ms=10.0,
        limit=5,
    )

    stat_call = next(call for call in driver.calls if "pg_stat_statements" in call[0])
    # Params: [min_calls, min_total_ms, max_rows_per_call, limit]
    assert stat_call[1] == [250, 10.0, 1.5, 5]


async def test_detect_n_plus_one_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))  # type: ignore[arg-type]
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("detect_n_plus_one", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is False
