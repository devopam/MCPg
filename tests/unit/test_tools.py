"""Tests for MCP tools."""

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver, FakePool
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg import __version__
from mcpg.config import AccessMode, load_settings
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.database import Database
from mcpg.listen import ListenManager
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


def _listen_manager() -> ListenManager:
    # Constructed with the real ListenManager class; stays idle (no PG
    # connection opened) until a subscribe_channel call lands.
    return ListenManager(database_url=_SETTINGS.database_url)


def _cursor_manager() -> CursorManager:
    # Real CursorManager — stays idle until open() is called, no
    # connection opens at construction time.
    return CursorManager(database_url=_SETTINGS.database_url)


async def test_build_server_info_reports_static_facts() -> None:
    info = await build_server_info(
        AppContext(
            settings=_SETTINGS,
            database=_database(),
            listen_manager=_listen_manager(),
            cursor_manager=_cursor_manager(),
        )
    )

    assert info == ServerInfo(
        mcpg_version=__version__,
        access_mode="read-only",
        transport="stdio",
        database_connected=False,
        nl2sql_default_provider=None,
        nl2sql_available_providers=[],
        wal_level=None,
        effective_wal_level=None,
    )


async def test_build_server_info_reflects_database_connection() -> None:
    db = _database()
    await db.connect()

    info = await build_server_info(
        AppContext(
            settings=_SETTINGS,
            database=db,
            listen_manager=_listen_manager(),
            cursor_manager=_cursor_manager(),
        )
    )

    assert info.database_connected is True


async def test_build_server_info_skips_wal_level_probe_without_a_driver() -> None:
    """No driver → no probe; both WAL fields stay None even when the
    DB is connected. Lets `build_server_info` stay sync-callable in
    contexts where the caller doesn't need the live GUCs."""
    db = _database()
    await db.connect()
    info = await build_server_info(
        AppContext(
            settings=_SETTINGS,
            database=db,
            listen_manager=_listen_manager(),
            cursor_manager=_cursor_manager(),
        )
    )
    assert info.wal_level is None
    assert info.effective_wal_level is None


async def test_build_server_info_populates_wal_levels_from_the_driver() -> None:
    """When a driver IS supplied and the DB is connected, both
    `wal_level` and `effective_wal_level` come from
    `current_setting(..., true)` calls — the second `true` flag means
    the GUC name being missing returns NULL instead of erroring,
    which is how we differentiate PG ≤ 18 (no `effective_wal_level`)
    from PG 19+ (where both are populated)."""
    db = _database()
    await db.connect()
    # FakeDriver returns the same rows for every query, so seed both
    # GUC values into a single row keyed by the field name
    # `_string_setting` expects (`val`).
    driver = FakeDriver([{"val": "logical"}])
    info = await build_server_info(
        AppContext(
            settings=_SETTINGS,
            database=db,
            listen_manager=_listen_manager(),
            cursor_manager=_cursor_manager(),
        ),
        driver=driver,  # type: ignore[arg-type]
    )
    assert info.wal_level == "logical"
    assert info.effective_wal_level == "logical"


async def test_build_server_info_swallows_driver_errors() -> None:
    """A driver failure during the WAL-level probe must not abort the
    rest of the response — `wal_level` / `effective_wal_level` stay
    None and the static fields still populate. This is the "GUC probe
    is best-effort" contract."""
    db = _database()
    await db.connect()
    driver = FakeDriver([], fail=True)
    info = await build_server_info(
        AppContext(
            settings=_SETTINGS,
            database=db,
            listen_manager=_listen_manager(),
            cursor_manager=_cursor_manager(),
        ),
        driver=driver,  # type: ignore[arg-type]
    )
    assert info.wal_level is None
    assert info.effective_wal_level is None
    # Static facts still landed.
    assert info.mcpg_version == __version__
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
    assert ("seed_table_with_sample_data" in names) is (access_mode is AccessMode.UNRESTRICTED)


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


async def test_heavy_diagnostics_gating() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_ENABLE_HEAVY_DIAGNOSTICS": "false",
        }
    )
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        # Introspection should still work or not be blocked by heavy diagnostics (it should be registered)
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "run_advisors" in names

        # A gated tool like run_advisors should fail with the friendly error message
        result = await client.call_tool("run_advisors", {"schema": "public"})
        assert result.isError is True
        assert "disabled by the server administrator" in result.content[0].text


async def test_heavy_diagnostics_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    When heavy diagnostics are enabled, multiple invocations of a heavy tool like
    run_advisors in the same session should hit the cache after the first call.
    """
    # Import here to avoid circular imports at module import time if any
    from mcpg.tools import advisors

    original_run_advisors = advisors.run_advisors
    calls: dict[str, int] = {"count": 0}

    async def wrapped_run_advisors(*args: Any, **kwargs: Any) -> Any:
        import inspect

        calls["count"] += 1
        result = original_run_advisors(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    monkeypatch.setattr(advisors, "run_advisors", wrapped_run_advisors)

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            # Heavy diagnostics must be enabled to exercise caching, not gating
            "MCPG_ENABLE_HEAVY_DIAGNOSTICS": "true",
        }
    )
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        # First call should invoke the underlying implementation
        first = await client.call_tool("run_advisors", {"schema": "public"})
        assert first.isError is False

        # Second call with the same arguments should be served from cache
        second = await client.call_tool("run_advisors", {"schema": "public"})
        assert second.isError is False

        # The cached response should be identical and the underlying function
        # should only have been invoked once.
        assert second.content == first.content
        assert calls["count"] == 1
