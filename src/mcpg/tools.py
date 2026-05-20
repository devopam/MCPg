"""MCP tool definitions for MCPg.

Tool *logic* lives in dedicated modules (e.g. ``mcpg.introspection``) and is
unit-tested directly. This module holds the thin MCP wrappers and
``register_tools``, which ``create_server`` calls.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from mcpg import __version__, introspection, query
from mcpg._vendor.sql import SqlDriver
from mcpg.context import AppContext

# The MCP request context FastMCP injects into every tool.
_Ctx = Context[ServerSession, AppContext, Any]


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


def _driver(ctx: _Ctx) -> SqlDriver:
    """Return the SQL driver for the current request."""
    return ctx.request_context.lifespan_context.database.driver()


def _register_server_info(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_server_info",
        description=("Return the MCPg server version, access mode, transport, and database connection status."),
    )
    async def get_server_info(ctx: _Ctx) -> dict[str, Any]:
        return asdict(build_server_info(ctx.request_context.lifespan_context))


def _register_introspection(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_schemas",
        description="List database schemas, excluding PostgreSQL's own schemas unless include_system is true.",
    )
    async def list_schemas(ctx: _Ctx, include_system: bool = False) -> list[dict[str, Any]]:
        schemas = await introspection.list_schemas(_driver(ctx), include_system=include_system)
        return [asdict(schema) for schema in schemas]

    @server.tool(name="list_tables", description="List the tables and views in a schema.")
    async def list_tables(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        tables = await introspection.list_tables(_driver(ctx), schema)
        return [asdict(table) for table in tables]

    @server.tool(name="describe_table", description="Describe the columns of a table, in ordinal order.")
    async def describe_table(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        columns = await introspection.describe_table(_driver(ctx), schema, table)
        return [asdict(column) for column in columns]

    @server.tool(name="list_indexes", description="List the indexes defined on a table.")
    async def list_indexes(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        indexes = await introspection.list_indexes(_driver(ctx), schema, table)
        return [asdict(index) for index in indexes]

    @server.tool(name="list_extensions", description="List the extensions installed in the database.")
    async def list_extensions(ctx: _Ctx) -> list[dict[str, Any]]:
        extensions = await introspection.list_extensions(_driver(ctx))
        return [asdict(extension) for extension in extensions]


def _register_query(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_select",
        description=(
            "Validate and run a read-only SQL query. Writes, DDL, and other "
            "unsafe statements are rejected before execution."
        ),
    )
    async def run_select(ctx: _Ctx, sql: str) -> dict[str, Any]:
        result = await query.run_select(_driver(ctx), sql)
        return asdict(result)

    @server.tool(
        name="explain_query",
        description=(
            "Return the PostgreSQL execution plan for a query without running "
            "it. The query is validated by the same safety allowlist as run_select."
        ),
    )
    async def explain_query(ctx: _Ctx, sql: str) -> dict[str, Any]:
        result = await query.explain_query(_driver(ctx), sql)
        return asdict(result)


def register_tools(server: FastMCP[AppContext]) -> None:
    """Register every MCP tool on the given server."""
    _register_server_info(server)
    _register_introspection(server)
    _register_query(server)
