"""Tests for workload analysis and the analyze_workload tool."""

from _fakes import FakeDatabase, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.workload import SlowQuery, analyze_workload

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
