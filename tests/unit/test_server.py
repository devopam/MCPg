"""Tests for the MCP server bootstrap."""

import pytest
from _fakes import FakePool
from mcp.server.fastmcp import FastMCP

from mcpg.config import Settings, Transport, load_settings
from mcpg.cursors import CursorManager
from mcpg.database import Database
from mcpg.listen import ListenManager
from mcpg.server import SERVER_NAME, AppContext, create_server, make_lifespan, run

_DB_URL = "postgresql://u:p@localhost/db"
_SETTINGS = load_settings({"MCPG_DATABASE_URL": _DB_URL})


def _settings_with(transport: Transport) -> Settings:
    return load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_TRANSPORT": transport.value})


def test_create_server_returns_named_fastmcp() -> None:
    server = create_server(_SETTINGS)

    assert isinstance(server, FastMCP)
    assert server.name == SERVER_NAME


def test_create_server_reports_mcpg_version_in_serverinfo() -> None:
    # FastMCP doesn't forward a version, so without the pin the initialize
    # handshake would advertise the MCP SDK's version instead of mcpg's.
    from mcpg import __version__

    server = create_server(_SETTINGS)

    init_options = server._mcp_server.create_initialization_options()
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
    monkeypatch.setattr(FastMCP, "run", lambda self, transport: seen.append(transport))

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
