"""MCP server bootstrap for MCPg.

``create_server`` builds a configured :class:`FastMCP` instance. All shared
state (settings, the database connection) is owned by the server's lifespan
and exposed to tools via :class:`AppContext` — there is no module-level
mutable global state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock

from mcpg import audit
from mcpg.config import Settings, Transport
from mcpg.context import AppContext
from mcpg.database import Database
from mcpg.listen import ListenManager
from mcpg.tools import register_tools

SERVER_NAME = "mcpg"
SERVER_INSTRUCTIONS = (
    "MCPg: a PostgreSQL MCP server for inspecting, querying, operating, and tuning a Postgres database."
)

__all__ = ["SERVER_NAME", "AppContext", "AuditedFastMCP", "create_server", "make_lifespan", "run"]


class AuditedFastMCP(FastMCP[AppContext]):
    """A FastMCP server that records an audit event for every tool call."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            result = await super().call_tool(name, arguments)
        except Exception as exc:
            audit.record(audit.AuditEvent(tool=name, arguments=arguments, status="error", error=str(exc)))
            raise
        audit.record(audit.AuditEvent(tool=name, arguments=arguments, status="ok"))
        return result


def make_lifespan(
    settings: Settings, database: Database, listen_manager: ListenManager
) -> Callable[[FastMCP[AppContext]], AbstractAsyncContextManager[AppContext]]:
    """Build the server lifespan: open the database on start, close on stop.

    The listen manager is created eagerly (cheap — it doesn't open the
    listener connection until the first ``subscribe_channel`` call) and
    torn down on lifespan exit so subscriptions can't outlive the
    server.
    """

    @asynccontextmanager
    async def lifespan(_server: FastMCP[AppContext]) -> AsyncIterator[AppContext]:
        async with database, listen_manager:
            yield AppContext(settings=settings, database=database, listen_manager=listen_manager)

    return lifespan


def create_server(
    settings: Settings,
    *,
    database: Database | None = None,
    listen_manager: ListenManager | None = None,
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
    server: FastMCP[AppContext] = AuditedFastMCP(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=make_lifespan(settings, db, lm),
        host=settings.http_host,
        port=settings.http_port,
    )
    register_tools(server, settings)
    return server


def run(settings: Settings) -> None:
    """Create and run the server using the transport from ``settings``."""
    server = create_server(settings)
    match settings.transport:
        case Transport.STDIO:
            server.run(transport="stdio")
        case Transport.STREAMABLE_HTTP:
            server.run(transport="streamable-http")
        case Transport.SSE:
            server.run(transport="sse")
