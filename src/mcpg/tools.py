"""MCP tool definitions for MCPg.

Each tool has a small, directly testable logic function plus a thin wrapper
registered on the server. ``register_tools`` is called by ``create_server``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from mcpg import __version__
from mcpg.context import AppContext


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """High-level facts about a running MCPg server."""

    mcpg_version: str
    access_mode: str
    transport: str
    database_connected: bool


def build_server_info(app: AppContext) -> ServerInfo:
    """Assemble server info from the application context."""
    return ServerInfo(
        mcpg_version=__version__,
        access_mode=app.settings.access_mode.value,
        transport=app.settings.transport.value,
        database_connected=app.database.is_connected,
    )


def register_tools(server: FastMCP[AppContext]) -> None:
    """Register every MCP tool on the given server."""

    @server.tool(
        name="get_server_info",
        description=("Return the MCPg server version, access mode, transport, and database connection status."),
    )
    async def get_server_info(ctx: Context[ServerSession, AppContext, Any]) -> dict[str, Any]:
        return asdict(build_server_info(ctx.request_context.lifespan_context))
