"""Tests for extension management and the enable_extension tool."""

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.extensions import EnableExtensionResult, ExtensionError, enable_extension
from mcpg.server import create_server

_UNRESTRICTED_DDL = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_DDL": "true",
    }
)
_READ_ONLY = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_enable_extension_runs_create_extension_for_an_allowlisted_name() -> None:
    driver = FakeDriver()

    result = await enable_extension(driver, "pg_trgm")

    assert result == EnableExtensionResult(name="pg_trgm", enabled=True)
    query, _params, force_readonly = driver.calls[0]
    assert query == 'CREATE EXTENSION IF NOT EXISTS "pg_trgm"'
    assert force_readonly is False


async def test_enable_extension_rejects_a_name_not_on_the_allowlist() -> None:
    driver = FakeDriver()

    with pytest.raises(ExtensionError, match="allowlist"):
        await enable_extension(driver, "evil; DROP DATABASE postgres")
    # Rejection happens before any SQL is built.
    assert driver.calls == []


async def test_enable_extension_wraps_execution_failures() -> None:
    with pytest.raises(ExtensionError, match="execution failed"):
        await enable_extension(FakeDriver(fail=True), "pg_trgm")


async def test_enable_extension_tool_is_callable_when_ddl_is_allowed() -> None:
    server = create_server(_UNRESTRICTED_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("enable_extension", {"name": "pg_trgm"})

    assert result.isError is False


async def test_enable_extension_tool_is_absent_without_ddl_opt_in() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert "enable_extension" not in names
