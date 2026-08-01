"""Tests for the MCP server bootstrap."""

from typing import Any

import pytest
from _fakes import FakePool
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.mcpserver import MCPServer
from mcp.types import ClientCapabilities, ElicitationCapability, ToolAnnotations

import mcpg.server as server_mod
from mcpg.config import Settings, Transport, load_settings
from mcpg.cursors import CursorManager
from mcpg.database import Database
from mcpg.listen import ListenManager
from mcpg.observability import get_metrics
from mcpg.server import SERVER_NAME, AppContext, _ConfirmMutation, create_server, make_lifespan, run

_DB_URL = "postgresql://u:p@localhost/db"
_SETTINGS = load_settings({"MCPG_DATABASE_URL": _DB_URL})


def _settings_with(transport: Transport) -> Settings:
    return load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_TRANSPORT": transport.value})


def test_create_server_returns_named_mcpserver() -> None:
    server = create_server(_SETTINGS)

    assert isinstance(server, MCPServer)
    assert server.name == SERVER_NAME


def test_create_server_registers_tenant_role_context_middleware() -> None:
    # Guards against silently regressing multi-tenancy to static-default-only
    # if the `middleware=[TenantRoleContextMiddleware()]` kwarg is ever
    # dropped from `AuditedMCPServer(...)` in `create_server` — that would
    # otherwise fail with zero test breakage. `_lowlevel_server.middleware`
    # is a real, populated attribute on the installed SDK's low-level
    # server (alongside SDK-internal middleware like
    # `OpenTelemetryMiddleware`/`RequestStateBoundary`) — verified directly
    # against the installed `mcp` package before writing this assertion.
    from mcpg.tenancy import TenantRoleContextMiddleware

    server = create_server(_SETTINGS)

    assert any(isinstance(m, TenantRoleContextMiddleware) for m in server._lowlevel_server.middleware)


def test_create_server_reports_mcpg_version_in_serverinfo() -> None:
    # MCPServer forwards ``version`` straight through to the low-level server,
    # so mcpg's version is what gets advertised in the initialize handshake.
    from mcpg import __version__

    server = create_server(_SETTINGS)

    init_options = server._lowlevel_server.create_initialization_options()
    assert init_options.server_version == __version__


async def test_lifespan_connects_database_and_yields_app_context() -> None:
    pool = FakePool()
    db = Database(_SETTINGS, pool=pool)  # type: ignore[arg-type]
    lm = ListenManager(database_url=_SETTINGS.database_url)
    cm = CursorManager(database_url=_SETTINGS.database_url)
    lifespan = make_lifespan(_SETTINGS, db, lm, cm)

    async with lifespan(create_server(_SETTINGS)) as ctx:
        assert isinstance(ctx, AppContext)
        assert ctx.settings is _SETTINGS
        assert ctx.database is db
        assert ctx.listen_manager is lm
        assert ctx.cursor_manager is cm
        assert pool.connect_calls == 1
        assert db.is_connected is True

    assert pool.close_calls == 1
    assert db.is_connected is False


def test_run_dispatches_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(MCPServer, "run", lambda self, transport: seen.append(transport))

    run(_settings_with(Transport.STDIO))

    assert seen == ["stdio"]


def test_run_dispatches_streamable_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    # HTTP transports route through mcpg.http_runtime.run_http (which
    # owns the /metrics endpoint + optional bearer auth + uvicorn loop).
    seen: list[str] = []
    import mcpg.http_runtime as http_runtime

    monkeypatch.setattr(
        http_runtime,
        "run_http",
        lambda _server, _settings, *, kind: seen.append(kind),
    )

    run(_settings_with(Transport.STREAMABLE_HTTP))

    assert seen == ["streamable-http"]


def test_run_dispatches_sse_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    import mcpg.http_runtime as http_runtime

    monkeypatch.setattr(
        http_runtime,
        "run_http",
        lambda _server, _settings, *, kind: seen.append(kind),
    )

    run(_settings_with(Transport.SSE))

    assert seen == ["sse"]


async def test_lifespan_waits_for_in_flight_calls_to_drain() -> None:
    import asyncio
    import dataclasses
    import time

    pool = FakePool()
    db = Database(_SETTINGS, pool=pool)  # type: ignore[arg-type]
    lm = ListenManager(database_url=_SETTINGS.database_url)
    cm = CursorManager(database_url=_SETTINGS.database_url)

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
        }
    )
    settings = dataclasses.replace(settings, shutdown_drain_seconds=0.5)  # type: ignore[arg-type]

    lifespan = make_lifespan(settings, db, lm, cm)
    server = create_server(settings)
    server.in_flight_calls = 1

    async def decrement_later() -> None:
        await asyncio.sleep(0.2)
        server.in_flight_calls = 0

    tasks = []
    start_time = time.monotonic()
    async with lifespan(server):
        tasks.append(asyncio.create_task(decrement_later()))

    duration = time.monotonic() - start_time
    assert duration >= 0.2
    assert server.in_flight_calls == 0


async def test_lifespan_drain_timeout() -> None:
    import dataclasses
    import time

    pool = FakePool()
    db = Database(_SETTINGS, pool=pool)  # type: ignore[arg-type]
    lm = ListenManager(database_url=_SETTINGS.database_url)
    cm = CursorManager(database_url=_SETTINGS.database_url)

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
        }
    )
    settings = dataclasses.replace(settings, shutdown_drain_seconds=0.2)  # type: ignore[arg-type]

    lifespan = make_lifespan(settings, db, lm, cm)
    server = create_server(settings)
    server.in_flight_calls = 1

    start_time = time.monotonic()
    async with lifespan(server):
        pass

    duration = time.monotonic() - start_time
    assert duration >= 0.2
    assert server.in_flight_calls == 1


# --- elicitation confirmation gate (MCPG_ELICIT_CONFIRM_WRITES) ------------


class _FakeElicitContext:
    """Minimal stand-in for ``Context`` exposing only the two members the
    gate in ``AuditedMCPServer.call_tool`` touches: ``client_capabilities``
    and ``elicit()``. Records whether/how ``elicit`` was called so tests
    can assert on it without standing up a real client session."""

    def __init__(self, client_capabilities: ClientCapabilities | None, elicit_result: Any = None) -> None:
        self.client_capabilities = client_capabilities
        self._elicit_result = elicit_result
        self.elicit_calls: list[tuple[str, type]] = []

    async def elicit(self, message: str, schema: type) -> Any:
        self.elicit_calls.append((message, schema))
        return self._elicit_result


async def _fake_write_tool() -> str:
    return "wrote"


async def _fake_read_tool() -> str:
    return "read"


def _elicit_settings(*, elicit_confirm_writes: bool) -> Settings:
    return load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_ELICIT_CONFIRM_WRITES": "true" if elicit_confirm_writes else "false",
        }
    )


def _server_with_fake_tools(settings: Settings) -> Any:
    server = create_server(settings)
    server.add_tool(_fake_write_tool, name="fake_write_tool", annotations=ToolAnnotations(read_only_hint=False))
    server.add_tool(_fake_read_tool, name="fake_read_tool", annotations=ToolAnnotations(read_only_hint=True))
    return server


_ELICITING_CAPS = ClientCapabilities(elicitation=ElicitationCapability())
_NON_ELICITING_CAPS = ClientCapabilities()


async def test_elicit_gate_off_never_calls_elicit_even_for_write_tool() -> None:
    # (a) setting off -> tool runs without any elicit call regardless of
    # read-only status. Byte-for-byte the pre-Task-6 behavior.
    server = _server_with_fake_tools(_elicit_settings(elicit_confirm_writes=False))
    ctx = _FakeElicitContext(client_capabilities=_ELICITING_CAPS)

    result = await server.call_tool("fake_write_tool", {}, context=ctx)

    assert ctx.elicit_calls == []
    assert result.is_error is False


async def test_elicit_gate_accepted_confirmation_runs_tool() -> None:
    # (b) setting on, client declares elicitation, write-tier tool, client
    # accepts -> tool runs.
    server = _server_with_fake_tools(_elicit_settings(elicit_confirm_writes=True))
    ctx = _FakeElicitContext(
        client_capabilities=_ELICITING_CAPS,
        elicit_result=AcceptedElicitation(data=_ConfirmMutation(confirm=True)),
    )

    result = await server.call_tool("fake_write_tool", {}, context=ctx)

    assert len(ctx.elicit_calls) == 1
    assert result.is_error is False


async def test_elicit_gate_declined_confirmation_blocks_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # (c) same but client declines -> tool does NOT run, is_error=True.
    recorded: list[Any] = []
    monkeypatch.setattr(server_mod.audit, "record", lambda event: recorded.append(event))

    server = _server_with_fake_tools(_elicit_settings(elicit_confirm_writes=True))
    ctx = _FakeElicitContext(
        client_capabilities=_ELICITING_CAPS,
        elicit_result=DeclinedElicitation(),
    )

    calls_before = get_metrics().snapshot()[0].get(("fake_write_tool", "unknown", "denied"), 0)
    result = await server.call_tool("fake_write_tool", {}, context=ctx)
    calls_after = get_metrics().snapshot()[0].get(("fake_write_tool", "unknown", "denied"), 0)

    assert len(ctx.elicit_calls) == 1
    assert result.is_error is True
    assert any("not confirmed" in block.text for block in result.content)

    # A declined write must still leave an audit trail (the elicitation gate
    # returns early, before the normal audit.record()/metrics.record_call()
    # calls at the bottom of `call_tool` — see server.py's early-return path
    # for the confirmation gate) and be counted, so it isn't invisible to
    # `render_prometheus()`.
    assert len(recorded) == 1
    assert recorded[0].tool == "fake_write_tool"
    assert recorded[0].status == "denied"
    assert calls_after - calls_before == 1


async def test_elicit_gate_cancelled_confirmation_blocks_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cancelling the elicitation prompt must be treated the same as a
    # decline — no partial/ambiguous state where the tool still runs, and
    # (like a decline) it must still produce an audit event + metrics count.
    recorded: list[Any] = []
    monkeypatch.setattr(server_mod.audit, "record", lambda event: recorded.append(event))

    server = _server_with_fake_tools(_elicit_settings(elicit_confirm_writes=True))
    ctx = _FakeElicitContext(
        client_capabilities=_ELICITING_CAPS,
        elicit_result=CancelledElicitation(),
    )

    calls_before = get_metrics().snapshot()[0].get(("fake_write_tool", "unknown", "denied"), 0)
    result = await server.call_tool("fake_write_tool", {}, context=ctx)
    calls_after = get_metrics().snapshot()[0].get(("fake_write_tool", "unknown", "denied"), 0)

    assert len(recorded) == 1
    assert recorded[0].tool == "fake_write_tool"
    assert recorded[0].status == "denied"
    assert calls_after - calls_before == 1

    assert len(ctx.elicit_calls) == 1
    assert result.is_error is True


async def test_elicit_gate_no_client_capability_runs_tool_without_elicit() -> None:
    # (d) setting on but client didn't declare elicitation support ->
    # graceful degradation: tool still runs, no elicit call.
    server = _server_with_fake_tools(_elicit_settings(elicit_confirm_writes=True))
    ctx = _FakeElicitContext(client_capabilities=_NON_ELICITING_CAPS)

    result = await server.call_tool("fake_write_tool", {}, context=ctx)

    assert ctx.elicit_calls == []
    assert result.is_error is False


async def test_elicit_gate_absent_capabilities_object_runs_tool_without_elicit() -> None:
    # Some transports/anonymous requests report client_capabilities=None
    # entirely (no _meta at all) rather than a capabilities object with
    # elicitation unset — must degrade the same way.
    server = _server_with_fake_tools(_elicit_settings(elicit_confirm_writes=True))
    ctx = _FakeElicitContext(client_capabilities=None)

    result = await server.call_tool("fake_write_tool", {}, context=ctx)

    assert ctx.elicit_calls == []
    assert result.is_error is False


async def test_elicit_gate_skips_read_only_tool_even_with_capability() -> None:
    # (e) setting on, tool IS read-only -> no elicit call regardless of
    # client capability.
    server = _server_with_fake_tools(_elicit_settings(elicit_confirm_writes=True))
    ctx = _FakeElicitContext(client_capabilities=_ELICITING_CAPS)

    result = await server.call_tool("fake_read_tool", {}, context=ctx)

    assert ctx.elicit_calls == []
    assert result.is_error is False


async def test_elicit_gate_is_noop_when_context_is_none() -> None:
    # Calls made without a context (already-handled possibility elsewhere
    # in this codebase) must not raise just because the flag is on.
    server = _server_with_fake_tools(_elicit_settings(elicit_confirm_writes=True))

    result = await server.call_tool("fake_write_tool", {}, context=None)

    assert result.is_error is False
