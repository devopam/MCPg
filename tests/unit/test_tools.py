"""Tests for MCP tools."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakePool
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg import __version__
from mcpg.config import AccessMode, load_settings
from mcpg.context import AppContext
from mcpg.database import Database
from mcpg.server import create_server
from mcpg.tools import ServerInfo, build_server_info

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})

_READ_TOOLS = {
    "get_server_info",
    "list_schemas",
    "list_tables",
    "describe_table",
    "list_indexes",
    "list_extensions",
    "run_select",
    "explain_query",
}


def _database() -> Database:
    return Database(_SETTINGS, pool=FakePool())  # type: ignore[arg-type]


def test_build_server_info_reports_static_facts() -> None:
    info = build_server_info(AppContext(settings=_SETTINGS, database=_database()))

    assert info == ServerInfo(
        mcpg_version=__version__,
        access_mode="read-only",
        transport="stdio",
        database_connected=False,
    )


async def test_build_server_info_reflects_database_connection() -> None:
    db = _database()
    await db.connect()

    info = build_server_info(AppContext(settings=_SETTINGS, database=db))

    assert info.database_connected is True


async def test_get_server_info_is_listed_by_the_server() -> None:
    server = create_server(_SETTINGS, database=_database())

    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_tools()

    assert "get_server_info" in {tool.name for tool in listed.tools}


async def test_get_server_info_is_callable_from_an_mcp_client() -> None:
    server = create_server(_SETTINGS, database=_database())

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("get_server_info", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["mcpg_version"] == __version__
    assert result.structuredContent["access_mode"] == "read-only"
    # The lifespan connected the (fake) database before the tool ran.
    assert result.structuredContent["database_connected"] is True


@pytest.mark.parametrize("access_mode", list(AccessMode))
async def test_read_tools_are_exposed_in_every_access_mode(access_mode: AccessMode) -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_ACCESS_MODE": access_mode.value,
        }
    )
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    # Every current tool is a read tool, so all modes expose the same set.
    # Phase 4 adds write tools, gated to unrestricted mode.
    assert names == _READ_TOOLS
