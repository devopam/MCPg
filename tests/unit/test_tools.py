"""Tests for MCP tools."""

from _fakes import FakePool
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg import __version__
from mcpg.config import load_settings
from mcpg.context import AppContext
from mcpg.database import Database
from mcpg.server import create_server
from mcpg.tools import ServerInfo, build_server_info

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


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
