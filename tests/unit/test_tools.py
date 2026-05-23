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
    "list_constraints",
    "list_views",
    "list_functions",
    "list_triggers",
    "list_partitions",
    "list_policies",
    "list_roles",
    "list_grants",
    "list_sequences",
    "list_extensions",
    "list_available_extensions",
    "run_select",
    "explain_query",
    "analyze_query_plan",
    "check_database_health",
    "analyze_workload",
    "recommend_indexes",
    "list_active_queries",
    "fuzzy_search",
    "full_text_search",
    "vector_search",
    "geo_search",
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


def _server_for(access_mode: AccessMode) -> object:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_ACCESS_MODE": access_mode.value,
        }
    )
    return create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]


@pytest.mark.parametrize("access_mode", list(AccessMode))
async def test_read_tools_are_exposed_in_every_access_mode(access_mode: AccessMode) -> None:
    async with create_connected_server_and_client_session(_server_for(access_mode)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert _READ_TOOLS <= names


@pytest.mark.parametrize("access_mode", list(AccessMode))
async def test_write_tools_are_exposed_only_in_unrestricted_mode(access_mode: AccessMode) -> None:
    async with create_connected_server_and_client_session(_server_for(access_mode)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert ("run_write" in names) is (access_mode is AccessMode.UNRESTRICTED)
    assert ("run_maintenance" in names) is (access_mode is AccessMode.UNRESTRICTED)
    assert ("cancel_query" in names) is (access_mode is AccessMode.UNRESTRICTED)
    assert ("terminate_backend" in names) is (access_mode is AccessMode.UNRESTRICTED)


@pytest.mark.parametrize(
    ("access_mode", "allow_ddl", "expected"),
    [
        ("read-only", True, False),
        ("restricted", True, False),
        ("unrestricted", False, False),
        ("unrestricted", True, True),
    ],
)
async def test_run_ddl_requires_unrestricted_mode_and_the_allow_ddl_opt_in(
    access_mode: str, allow_ddl: bool, expected: bool
) -> None:
    env = {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": access_mode}
    if allow_ddl:
        env["MCPG_ALLOW_DDL"] = "true"
    server = create_server(load_settings(env), database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert ("run_ddl" in names) is expected
