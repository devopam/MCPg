"""Tests for write execution and the run_write tool."""

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.write import WriteError, WriteResult, run_ddl, run_write

_UNRESTRICTED = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)
_UNRESTRICTED_DDL = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_DDL": "true",
    }
)


async def test_run_write_executes_a_dml_statement() -> None:
    driver = FakeDriver()

    result = await run_write(driver, "INSERT INTO widget (id) VALUES (1)")

    assert result == WriteResult(rows=[], row_count=0)
    # Writes must run read-write, not on the read-only path.
    assert driver.calls[0][2] is False


async def test_run_write_returns_rows_from_a_returning_clause() -> None:
    driver = FakeDriver([{"id": 7}])

    result = await run_write(driver, "INSERT INTO widget (id) VALUES (7) RETURNING id")

    assert result == WriteResult(rows=[{"id": 7}], row_count=1)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "CREATE TABLE widget (id int)",
        "DROP TABLE widget",
        "TRUNCATE widget",
        "ALTER TABLE widget ADD COLUMN x text",
    ],
)
async def test_run_write_rejects_non_dml_statements(sql: str) -> None:
    driver = FakeDriver()

    with pytest.raises(WriteError):
        await run_write(driver, sql)
    assert driver.calls == []


async def test_run_write_rejects_statement_stacking() -> None:
    driver = FakeDriver()

    with pytest.raises(WriteError, match="exactly one"):
        await run_write(driver, "INSERT INTO widget (id) VALUES (1); DROP TABLE widget")
    assert driver.calls == []


async def test_run_write_rejects_unparseable_sql() -> None:
    with pytest.raises(WriteError, match="could not parse"):
        await run_write(FakeDriver(), "this is not valid sql ;;;")


async def test_run_write_wraps_execution_failures() -> None:
    with pytest.raises(WriteError, match="execution failed"):
        await run_write(FakeDriver(fail=True), "INSERT INTO widget (id) VALUES (1)")


async def test_run_write_tool_is_callable_in_unrestricted_mode() -> None:
    database = FakeDatabase(FakeDriver([{"id": 1}]))
    server = create_server(_UNRESTRICTED, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("run_write", {"sql": "INSERT INTO widget (id) VALUES (1) RETURNING id"})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["row_count"] == 1


async def test_run_ddl_executes_a_ddl_statement() -> None:
    driver = FakeDriver()

    result = await run_ddl(driver, "CREATE TABLE widget (id int)")

    assert result == WriteResult(rows=[], row_count=0)
    assert driver.calls[0][2] is False


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1", "INSERT INTO widget (id) VALUES (1)", "DELETE FROM widget"],
)
async def test_run_ddl_rejects_non_ddl_statements(sql: str) -> None:
    driver = FakeDriver()

    with pytest.raises(WriteError):
        await run_ddl(driver, sql)
    assert driver.calls == []


async def test_run_ddl_rejects_statement_stacking() -> None:
    with pytest.raises(WriteError, match="exactly one"):
        await run_ddl(FakeDriver(), "CREATE TABLE a (id int); DROP TABLE b")


async def test_run_ddl_tool_is_callable_when_ddl_is_allowed() -> None:
    server = create_server(_UNRESTRICTED_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("run_ddl", {"sql": "CREATE TABLE widget (id int)"})

    assert result.isError is False
