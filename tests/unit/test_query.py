"""Tests for safe read-only query execution and the run_select tool."""

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.query import QueryError, QueryResult, run_select
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_run_select_returns_rows_columns_and_count() -> None:
    driver = FakeDriver([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    result = await run_select(driver, "SELECT id, name FROM widget")

    assert result == QueryResult(
        columns=["id", "name"],
        rows=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        row_count=2,
    )


async def test_run_select_on_empty_result_has_no_columns() -> None:
    result = await run_select(FakeDriver([]), "SELECT id FROM widget")

    assert result == QueryResult(columns=[], rows=[], row_count=0)


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
        result = await client.call_tool("run_select", {"sql": "SELECT 1 AS one"})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["row_count"] == 1
