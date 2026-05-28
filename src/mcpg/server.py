"""MCP server bootstrap for MCPg.

``create_server`` builds a configured :class:`FastMCP` instance. All shared
state (settings, the database connection) is owned by the server's lifespan
and exposed to tools via :class:`AppContext` — there is no module-level
mutable global state.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock

from mcpg import audit
from mcpg.config import Settings, Transport
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.database import Database
from mcpg.listen import ListenManager
from mcpg.middleware.rate_limit import RateLimiter
from mcpg.observability import get_metrics
from mcpg.tools import register_tools

SERVER_NAME = "mcpg"
SERVER_INSTRUCTIONS = (
    "MCPg: a PostgreSQL MCP server for inspecting, querying, operating, and tuning a Postgres database."
)

__all__ = ["SERVER_NAME", "AppContext", "AuditedFastMCP", "create_server", "make_lifespan", "run"]


class AuditedFastMCP(FastMCP[AppContext]):
    """A FastMCP server that records an audit event for every tool call."""

    rate_limiter: RateLimiter

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        # Enforce rate limiting if configured
        if hasattr(self, "rate_limiter"):
            allowed = await self.rate_limiter.consume(name)
            if not allowed:
                raise RuntimeError(f"Rate limit exceeded for tool {name!r}. Please try again later.")

        metrics = get_metrics()
        start = time.monotonic()
        try:
            result = await super().call_tool(name, arguments)
        except Exception as exc:
            duration = time.monotonic() - start
            audit.record(audit.AuditEvent(tool=name, arguments=arguments, status="error", error=str(exc)))
            metrics.record_call(name, "error", duration)
            raise
        duration = time.monotonic() - start
        audit.record(audit.AuditEvent(tool=name, arguments=arguments, status="ok"))
        metrics.record_call(name, "ok", duration)
        return result


def make_lifespan(
    settings: Settings,
    database: Database,
    listen_manager: ListenManager,
    cursor_manager: CursorManager,
) -> Callable[[FastMCP[AppContext]], AbstractAsyncContextManager[AppContext]]:
    """Build the server lifespan: open the database on start, close on stop.

    The listen manager is created eagerly (cheap — it doesn't open the
    listener connection until the first ``subscribe_channel`` call) and
    torn down on lifespan exit so subscriptions can't outlive the
    server. The cursor manager holds dedicated connections per open
    server-side cursor and is closed-out symmetrically.
    """

    @asynccontextmanager
    async def lifespan(_server: FastMCP[AppContext]) -> AsyncIterator[AppContext]:
        async with database, listen_manager, cursor_manager:
            yield AppContext(
                settings=settings,
                database=database,
                listen_manager=listen_manager,
                cursor_manager=cursor_manager,
            )

    return lifespan


def create_server(
    settings: Settings,
    *,
    database: Database | None = None,
    listen_manager: ListenManager | None = None,
    cursor_manager: CursorManager | None = None,
) -> FastMCP[AppContext]:
    """Construct a configured FastMCP server.

    Args:
        settings: Validated server configuration.
        database: Optional pre-built database (used by tests); otherwise one
            is created from ``settings``.
        listen_manager: Optional pre-built listen manager (used by tests
            to inject a fake connection factory); otherwise a default
            one is created from ``settings``.
    """
    db = database if database is not None else Database(settings)
    lm = (
        listen_manager
        if listen_manager is not None
        else ListenManager(database_url=settings.database_url, queue_max=settings.listen_queue_max)
    )
    cm = cursor_manager if cursor_manager is not None else CursorManager(database_url=settings.database_url)
    server: AuditedFastMCP = AuditedFastMCP(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=make_lifespan(settings, db, lm, cm),
        host=settings.http_host,
        port=settings.http_port,
    )
    # Instantiate and register the RateLimiter
    server.rate_limiter = RateLimiter(
        enabled=settings.rate_limit_enabled,
        global_max=settings.rate_limit_max_requests,
        global_window=settings.rate_limit_window_seconds,
        heavy_max=settings.rate_limit_heavy_max,
        heavy_window=settings.rate_limit_heavy_window,
    )
    register_tools(server, settings)
    return server


def run(settings: Settings) -> None:
    """Create and run the server using the transport from ``settings``.

    HTTP transports (``streamable-http`` and ``sse``) go through
    :mod:`mcpg.http_runtime` so the ``/metrics`` endpoint and optional
    bearer-token auth attach to the served app.
    """
    server = create_server(settings)
    match settings.transport:
        case Transport.STDIO:
            server.run(transport="stdio")
        case Transport.STREAMABLE_HTTP:
            from mcpg.http_runtime import run_http

            run_http(server, settings, kind="streamable-http")
        case Transport.SSE:
            from mcpg.http_runtime import run_http

            run_http(server, settings, kind="sse")
