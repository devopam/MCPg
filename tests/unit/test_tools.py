"""Tests for MCP tools."""

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver, FakePool
from _mcp_test_helpers import create_connected_server_and_client_session
from mcp.server.mcpserver import MCPServer

from mcpg import __version__
from mcpg.config import AccessMode, load_settings
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.database import Database
from mcpg.listen import ListenManager
from mcpg.server import create_server
from mcpg.tools import ServerInfo, build_server_info, register_tools

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})
_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"

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

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["mcpg_version"] == __version__
    assert result.structured_content["access_mode"] == "read-only"
    # The lifespan connected the (fake) database before the tool ran.
    assert result.structured_content["database_connected"] is True


async def test_list_databases_primary_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no secondaries, list_databases reports just the primary."""
    from mcpg.sql import SqlDriver

    db = Database(_SETTINGS, pool=FakePool())  # type: ignore[arg-type]

    class _OkDriver:
        async def execute_query(self, *a: Any, **k: Any) -> list[SqlDriver.RowResult]:
            return [SqlDriver.RowResult(cells={"?column?": 1})]

    monkeypatch.setattr(db, "driver", lambda database_id=None: _OkDriver())
    server = create_server(_SETTINGS, database=db)

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("list_databases", {})

    assert result.is_error is False
    content = result.structured_content
    assert content is not None
    # The primary is advertised under its real DB name ("db"), not "primary".
    assert content["primary_id"] == "db"
    assert content["database_ids"] == ["db"]
    assert len(content["databases"]) == 1
    assert content["databases"][0]["is_primary"] is True
    assert content["databases"][0]["read_only"] is False


async def test_list_databases_lists_secondaries(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpg.sql import SqlDriver

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SECONDARY_DATABASE_URLS": "analytics=postgresql://u:p@localhost/an",
        }
    )
    db = Database(
        settings,
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()},  # type: ignore[arg-type]
    )

    class _OkDriver:
        async def execute_query(self, *a: Any, **k: Any) -> list[SqlDriver.RowResult]:
            return [SqlDriver.RowResult(cells={"?column?": 1})]

    monkeypatch.setattr(db, "driver", lambda database_id=None: _OkDriver())
    server = create_server(settings, database=db)

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("list_databases", {})

    content = result.structured_content
    assert content is not None
    assert content["database_ids"] == ["db", "analytics"]
    secondary = next(d for d in content["databases"] if d["id"] == "analytics")
    assert secondary["read_only"] is True
    assert secondary["is_primary"] is False


async def test_list_databases_surfaces_unreachable_secondary(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpg.sql import SqlDriver

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SECONDARY_DATABASE_URLS": "analytics=postgresql://u:p@localhost/an",
        }
    )
    db = Database(
        settings,
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()},  # type: ignore[arg-type]
    )

    class _OkDriver:
        async def execute_query(self, *a: Any, **k: Any) -> list[SqlDriver.RowResult]:
            return [SqlDriver.RowResult(cells={"?column?": 1})]

    class _FailDriver:
        async def execute_query(self, *a: Any, **k: Any) -> list[SqlDriver.RowResult]:
            raise RuntimeError("connection refused")

    def _driver(database_id: str | None = None) -> Any:
        return _FailDriver() if database_id == "analytics" else _OkDriver()

    monkeypatch.setattr(db, "driver", _driver)
    server = create_server(settings, database=db)

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("list_databases", {})

    content = result.structured_content
    assert content is not None
    secondary = next(d for d in content["databases"] if d["id"] == "analytics")
    assert secondary["reachable"] is False
    assert secondary["detail"] is not None


async def test_read_tool_threads_database_param_to_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read tool's ``database`` arg must select the secondary's driver."""
    from mcpg.sql import SqlDriver

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SECONDARY_DATABASE_URLS": "analytics=postgresql://u:p@localhost/an",
        }
    )
    db = Database(
        settings,
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()},  # type: ignore[arg-type]
    )
    seen: list[str | None] = []

    class _OkDriver:
        async def execute_query(self, *a: Any, **k: Any) -> list[SqlDriver.RowResult]:
            return []

    def _driver(database_id: str | None = None) -> Any:
        seen.append(database_id)
        return _OkDriver()

    monkeypatch.setattr(db, "driver", _driver)
    server = create_server(settings, database=db)

    async with create_connected_server_and_client_session(server) as client:
        await client.call_tool("list_schemas", {"database": "analytics"})

    assert "analytics" in seen


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
async def test_write_tools_are_exposed_in_restricted_and_unrestricted_modes(access_mode: AccessMode) -> None:
    # WRITE-capability tools appear in the read-write tiers (restricted +
    # unrestricted) and are hidden only in read-only.
    async with create_connected_server_and_client_session(_server_for(access_mode)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    has_write = access_mode is not AccessMode.READ_ONLY
    assert ("run_write" in names) is has_write
    assert ("run_maintenance" in names) is has_write
    assert ("cancel_query" in names) is has_write
    assert ("terminate_backend" in names) is has_write
    assert ("seed_table_with_sample_data" in names) is has_write


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
        assert result.is_error is True
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
        assert first.is_error is False

        # Second call with the same arguments should be served from cache
        second = await client.call_tool("run_advisors", {"schema": "public"})
        assert second.is_error is False

        # The cached response should be identical and the underlying function
        # should only have been invoked once.
        assert second.content == first.content
        assert calls["count"] == 1


async def test_session_intent_core_actually_narrows_the_surface() -> None:
    """Regression test for the call-site switch: MCPG_SESSION_INTENT=core
    must narrow the registered surface to ~12 tools, not to 2 (the bug
    this task fixes) and not leave it at 254 (the bug of not switching
    the call site at all)."""
    from mcpg.session_intent import _TOOL_NAME_PRESETS, ALWAYS_KEEP

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SESSION_INTENT": "core",
        }
    )
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        tools = await client.list_tools()

    names = {t.name for t in tools.tools}

    # Core preset includes 12 headline tools, but run_ddl and run_write are
    # never registered in read-only mode (gated by capability checks before
    # session_intent filtering). Expected: 10 core tools + 2 always-keep tools.
    # Note: ALWAYS_KEEP includes 4 tools, but this fixture doesn't set
    # MCPG_DYNAMIC_SESSION_INTENT, so list_session_intents and
    # enable_session_intent are never registered here in the first place
    # (see _register_dynamic_session_intent's conditional registration) --
    # we only expect describe_self and describe_tool to survive the filter.
    expected_core = _TOOL_NAME_PRESETS["core"] - {"run_ddl", "run_write"}
    expected_always_keep = ALWAYS_KEEP - {"list_session_intents", "enable_session_intent"}
    expected_all = expected_core | expected_always_keep

    assert names == expected_all, f"Expected {sorted(expected_all)}, got {sorted(names)}"


async def test_dynamic_session_intent_tools_not_registered_by_default() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _FIXTURE_DB_URL})
    server: MCPServer = MCPServer("mcpg-dynamic-off-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" not in names
    assert "enable_session_intent" not in names


async def test_dynamic_session_intent_tools_registered_when_enabled() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _FIXTURE_DB_URL, "MCPG_DYNAMIC_SESSION_INTENT": "true"})
    server: MCPServer = MCPServer("mcpg-dynamic-on-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" in names
    assert "enable_session_intent" in names


async def test_dynamic_session_intent_tools_survive_a_narrow_static_intent() -> None:
    """The ordering/ALWAYS_KEEP guarantee: under MCPG_SESSION_INTENT=core,
    the two meta-tools must still survive -- otherwise a session under a
    narrow static intent would have no way to discover or grow its own
    surface.

    ``core`` (not a bucket preset like ``lookup``) is the case that
    actually exercises ALWAYS_KEEP: it's a tool-name preset resolved
    with an *empty* bucket set (see ``session_intent.resolve_intent``),
    so ``resolved_tool_names`` only keeps ALWAYS_KEEP names plus the 12
    explicit core tool names -- the observability bucket these two
    meta-tools classify into is never consulted. Every bucket preset
    (``lookup``/``migration``/``vector_rag``/``monitor``) includes the
    ``observability`` bucket already, so using one of those here would
    pass regardless of whether ALWAYS_KEEP or ordering worked at all --
    verified empirically, not assumed."""
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_SESSION_INTENT": "core",
            "MCPG_DYNAMIC_SESSION_INTENT": "true",
        }
    )
    server: MCPServer = MCPServer("mcpg-dynamic-and-static-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" in names
    assert "enable_session_intent" in names


async def test_default_settings_tool_surface_is_unchanged() -> None:
    """Parity test: with every new flag unset, the registered surface must
    be byte-for-byte the same tools as before this feature existed --
    neither of the two new meta-tools, and no change to any existing
    tool's registration.

    ``MCPG_DATABASE_URL`` alone leaves ``MCPG_ACCESS_MODE`` at its default
    (``read-only``), which has always exposed a subset of the full
    catalogue -- 186 tools, not the 254-tool *maximal* surface (every gate
    flag on; see ``tests/contract/test_tool_surface_snapshot.py``). Verified
    empirically against the pre-plan baseline (commit 0ed672b, before this
    feature's first commit): default settings there also registered 186
    tools. 186 is therefore the correct default-parity number, not 254 --
    the plan's example test text conflated the two.
    """
    settings = load_settings({"MCPG_DATABASE_URL": _FIXTURE_DB_URL})
    server: MCPServer = MCPServer("mcpg-default-parity-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" not in names
    assert "enable_session_intent" not in names
    assert len(names) == 186


async def test_maximal_settings_tool_surface_is_unchanged_with_dynamic_flag_off() -> None:
    """Parity test at the *other* ceiling: every gate flag on (unrestricted
    + allow_ddl/shell/listen) except MCPG_DYNAMIC_SESSION_INTENT, which
    stays unset. This is the 254-tool number the plan's example text
    actually meant -- it's the maximal surface
    ``tests/contract/tool_surface.snapshot.json`` was generated from before
    Task 9 added the two meta-tools behind the dynamic flag. Confirms the
    new feature is fully opt-in: even at the maximal ceiling, the meta-tools
    stay absent until the flag is explicitly enabled.
    """
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
        }
    )
    server: MCPServer = MCPServer("mcpg-maximal-parity-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" not in names
    assert "enable_session_intent" not in names
    assert len(names) == 254


async def test_static_session_intent_only_is_unchanged() -> None:
    """Layer 2 disabled, Layer 1 alone: must match session_intent.py's
    pre-existing, already-tested behavior exactly -- every surviving
    tool classifies into one of `lookup`'s buckets, or is one of the
    two always-kept introspection tools, with no other tool escaping
    the filter."""
    from mcpg.about import classify_tool
    from mcpg.session_intent import ALWAYS_KEEP, INTENT_PRESETS

    settings = load_settings({"MCPG_DATABASE_URL": _FIXTURE_DB_URL, "MCPG_SESSION_INTENT": "lookup"})
    server: MCPServer = MCPServer("mcpg-static-only-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" not in names  # Layer 2 never enabled
    expected_always_keep = ALWAYS_KEEP - {"list_session_intents", "enable_session_intent"}
    for name in names:
        assert classify_tool(name) in INTENT_PRESETS["lookup"] or name in expected_always_keep


async def test_layered_composition_dynamic_cannot_exceed_static_ceiling() -> None:
    """The test that proves the Layer 1/Layer 2 ceiling relationship
    (spec section 3 and section 6) is real, not just described.

    MCPG_SESSION_INTENT=lookup registers only the lookup-bucket tools
    (plus describe_self/describe_tool/the two meta-tools). Enabling a
    dynamic intent whose tools were never registered under that ceiling
    must reveal nothing new for them via visible_tool_names -- this
    tests the *resolver*, since exercising it through the live
    MCPServer's tools/list dispatch requires a running session; the
    resolver-level test is what Task 5/6 already establish, and this
    test wires it to a real, fully-registered server's tool set to
    confirm the intersection basis is correct end-to-end."""
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_SESSION_INTENT": "lookup",
            "MCPG_DYNAMIC_SESSION_INTENT": "true",
        }
    )
    server: MCPServer = MCPServer("mcpg-layered-fixture")
    register_tools(server, settings)
    registered = frozenset(t.name for t in await server.list_tools())

    from mcpg.dynamic_session_intent import enable_intent, visible_tool_names

    session_key = "layered-test-session"
    await enable_intent(session_key, "vector_rag")  # vector_rag was never registered under lookup
    visible = visible_tool_names(session_key, default_intent=("core",), registered=registered)
    assert visible <= registered  # never exceeds what Layer 1 left
    # vector_rag's own tools (e.g. vector_search) were never in `registered`
    # in the first place under MCPG_SESSION_INTENT=lookup, so they can't
    # appear in `visible` either -- the intersection makes this automatic.
    assert "vector_search" not in visible
