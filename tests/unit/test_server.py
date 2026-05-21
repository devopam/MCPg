"""Tests for the MCP server bootstrap."""

import pytest
from _fakes import FakePool
from mcp.server.fastmcp import FastMCP

from mcpg.config import Settings, Transport, load_settings
from mcpg.database import Database
from mcpg.server import SERVER_NAME, AppContext, create_server, make_lifespan, run

_DB_URL = "postgresql://u:p@localhost/db"
_SETTINGS = load_settings({"MCPG_DATABASE_URL": _DB_URL})


def _settings_with(transport: Transport) -> Settings:
    return load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_TRANSPORT": transport.value})


def test_create_server_returns_named_fastmcp() -> None:
    server = create_server(_SETTINGS)

    assert isinstance(server, FastMCP)
    assert server.name == SERVER_NAME


async def test_lifespan_connects_database_and_yields_app_context() -> None:
    pool = FakePool()
    db = Database(_SETTINGS, pool=pool)  # type: ignore[arg-type]
    lifespan = make_lifespan(_SETTINGS, db)

    async with lifespan(create_server(_SETTINGS)) as ctx:
        assert isinstance(ctx, AppContext)
        assert ctx.settings is _SETTINGS
        assert ctx.database is db
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
    seen: list[str] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, transport: seen.append(transport))

    run(_settings_with(Transport.STREAMABLE_HTTP))

    assert seen == ["streamable-http"]


def test_run_dispatches_sse_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, transport: seen.append(transport))

    run(_settings_with(Transport.SSE))

    assert seen == ["sse"]
