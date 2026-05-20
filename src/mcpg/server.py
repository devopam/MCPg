"""MCP server bootstrap for MCPg.

``create_server`` builds a configured :class:`FastMCP` instance. All shared
state (settings, the database connection) is owned by the server's lifespan
and exposed to tools via :class:`AppContext` — there is no module-level
mutable global state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

from mcpg.config import Settings, Transport
from mcpg.database import Database

SERVER_NAME = "mcpg"
SERVER_INSTRUCTIONS = (
    "MCPg: a PostgreSQL MCP server for inspecting, querying, operating, and tuning a Postgres database."
)


@dataclass(frozen=True, slots=True)
class AppContext:
    """State shared with every tool invocation for the server's lifetime."""

    settings: Settings
    database: Database


def make_lifespan(
    settings: Settings, database: Database
) -> Callable[[FastMCP[AppContext]], AbstractAsyncContextManager[AppContext]]:
    """Build the server lifespan: open the database on start, close on stop."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP[AppContext]) -> AsyncIterator[AppContext]:
        async with database:
            yield AppContext(settings=settings, database=database)

    return lifespan


def create_server(settings: Settings, *, database: Database | None = None) -> FastMCP[AppContext]:
    """Construct a configured FastMCP server.

    Args:
        settings: Validated server configuration.
        database: Optional pre-built database (used by tests); otherwise one
            is created from ``settings``.
    """
    db = database if database is not None else Database(settings)
    server: FastMCP[AppContext] = FastMCP(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=make_lifespan(settings, db),
        host=settings.http_host,
        port=settings.http_port,
    )
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
