"""Tests for safe read-only query execution and the run_select tool."""

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.query import (
    ExplainResult,
    QueryError,
    QueryPlanAnalysis,
    QueryResult,
    analyze_query_plan,
    explain_query,
    run_select,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_run_select_returns_rows_columns_and_count() -> None:
    driver = FakeDriver([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    result = await run_select(driver, "SELECT id, name FROM widget")

    assert result == QueryResult(
        columns=["id", "name"],
        rows=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        row_count=2,
        truncated=False,
    )


async def test_run_select_on_empty_result_has_no_columns() -> None:
    result = await run_select(FakeDriver([]), "SELECT id FROM widget")

    assert result == QueryResult(columns=[], rows=[], row_count=0, truncated=False)


async def test_run_select_caps_rows_and_flags_truncation() -> None:
    driver = FakeDriver([{"id": n} for n in range(5)])

    result = await run_select(driver, "SELECT id FROM widget", max_rows=3)

    assert result.row_count == 3
    assert result.rows == [{"id": 0}, {"id": 1}, {"id": 2}]
    assert result.truncated is True


async def test_run_select_not_truncated_when_under_the_cap() -> None:
    driver = FakeDriver([{"id": n} for n in range(3)])

    result = await run_select(driver, "SELECT id FROM widget", max_rows=3)

    assert result.truncated is False


async def test_run_select_rejects_non_positive_max_rows() -> None:
    with pytest.raises(QueryError, match="max_rows"):
        await run_select(FakeDriver(), "SELECT 1", max_rows=0)


@pytest.mark.parametrize(
    "unsafe_sql",
    [
        "DROP TABLE widget",
        "DELETE FROM widget",
        "INSERT INTO widget (id) VALUES (1)",
        "UPDATE widget SET id = 1",
    ],
)
async def test_run_select_rejects_non_read_statements(unsafe_sql: str) -> None:
    with pytest.raises(QueryError):
        await run_select(FakeDriver(), unsafe_sql)


async def test_run_select_rejects_unparseable_sql() -> None:
    with pytest.raises(QueryError):
        await run_select(FakeDriver(), "this is not sql ;;;")


async def test_run_select_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([{"one": 1}]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("run_select", {"sql": "SELECT 1 AS one", "max_rows": 1})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["row_count"] == 1
    assert result.structuredContent["truncated"] is False


async def test_explain_query_returns_the_plan() -> None:
    plan = [{"Plan": {"Node Type": "Result"}}]
    driver = FakeDriver([{"QUERY PLAN": plan}])

    result = await explain_query(driver, "SELECT 1")

    assert result == ExplainResult(plan=plan)


async def test_explain_query_parses_a_json_string_plan() -> None:
    driver = FakeDriver([{"QUERY PLAN": '[{"Plan": {"Node Type": "Result"}}]'}])

    result = await explain_query(driver, "SELECT 1")

    assert result == ExplainResult(plan=[{"Plan": {"Node Type": "Result"}}])


async def test_explain_query_rejects_a_write() -> None:
    with pytest.raises(QueryError):
        await explain_query(FakeDriver(), "DROP TABLE widget")


async def test_explain_query_raises_when_no_plan_is_returned() -> None:
    with pytest.raises(QueryError, match="no plan"):
        await explain_query(FakeDriver([]), "SELECT 1")


async def test_explain_query_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([{"QUERY PLAN": [{"Plan": {"Node Type": "Result"}}]}]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("explain_query", {"sql": "SELECT 1"})

    assert result.isError is False


_PLAN_TREE = [
    {
        "Plan": {
            "Node Type": "Hash Join",
            "Total Cost": 250.0,
            "Plan Rows": 1000,
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "orders", "Total Cost": 100.0, "Plan Rows": 5000},
                {"Node Type": "Index Scan", "Relation Name": "users", "Total Cost": 50.0, "Plan Rows": 1000},
            ],
        }
    }
]


async def test_analyze_query_plan_summarises_the_plan_tree() -> None:
    result = await analyze_query_plan(FakeDriver([{"QUERY PLAN": _PLAN_TREE}]), "SELECT 1")

    assert result == QueryPlanAnalysis(
        total_cost=250.0,
        estimated_rows=1000,
        node_types=["Hash Join", "Index Scan", "Seq Scan"],
        sequential_scans=["orders"],
    )


async def test_analyze_query_plan_reports_no_sequential_scans_for_an_index_plan() -> None:
    plan = [{"Plan": {"Node Type": "Index Scan", "Relation Name": "users", "Total Cost": 8.0, "Plan Rows": 1}}]

    result = await analyze_query_plan(FakeDriver([{"QUERY PLAN": plan}]), "SELECT 1")

    assert result.sequential_scans == []


async def test_analyze_query_plan_rejects_unexpected_explain_output() -> None:
    with pytest.raises(QueryError, match="unexpected EXPLAIN output"):
        await analyze_query_plan(FakeDriver([{"QUERY PLAN": {"not": "a list"}}]), "SELECT 1")


async def test_analyze_query_plan_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([{"QUERY PLAN": _PLAN_TREE}]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("analyze_query_plan", {"sql": "SELECT 1"})

    assert result.isError is False
