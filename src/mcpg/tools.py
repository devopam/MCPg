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

from mcpg import (
    __version__,
    extensions,
    health,
    indexing,
    introspection,
    query,
    textsearch,
    workload,
    write,
)
from mcpg._vendor.sql import SqlDriver
from mcpg.config import Settings
from mcpg.context import AppContext
from mcpg.policy import Capability, is_permitted

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

    @server.tool(
        name="list_available_extensions",
        description="List every extension available to the database, with whether it is installed.",
    )
    async def list_available_extensions(ctx: _Ctx) -> list[dict[str, Any]]:
        extensions = await introspection.list_available_extensions(_driver(ctx))
        return [asdict(extension) for extension in extensions]


def _register_query(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_select",
        description=(
            "Validate and run a read-only SQL query. Writes, DDL, and other "
            "unsafe statements are rejected before execution."
        ),
    )
    async def run_select(ctx: _Ctx, sql: str, max_rows: int = query.DEFAULT_MAX_ROWS) -> dict[str, Any]:
        result = await query.run_select(_driver(ctx), sql, max_rows=max_rows)
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

    @server.tool(
        name="analyze_query_plan",
        description=(
            "Summarise a query's execution plan: total estimated cost, "
            "estimated rows, node types used, and any sequentially-scanned tables."
        ),
    )
    async def analyze_query_plan(ctx: _Ctx, sql: str) -> dict[str, Any]:
        result = await query.analyze_query_plan(_driver(ctx), sql)
        return asdict(result)


def _register_health(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="check_database_health",
        description=(
            "Run database health checks: connection utilisation, buffer cache "
            "hit ratio, tables needing vacuum, and invalid indexes."
        ),
    )
    async def check_database_health(ctx: _Ctx) -> dict[str, Any]:
        report = await health.check_database_health(_driver(ctx))
        return asdict(report)

    @server.tool(
        name="analyze_workload",
        description=(
            "Return the slowest queries by mean execution time, via the "
            "pg_stat_statements extension. Reports availability=false if the "
            "extension is not installed."
        ),
    )
    async def analyze_workload(ctx: _Ctx, limit: int = workload.DEFAULT_LIMIT) -> dict[str, Any]:
        report = await workload.analyze_workload(_driver(ctx), limit=limit)
        return asdict(report)

    @server.tool(
        name="recommend_indexes",
        description=("Recommend tables that may benefit from indexing — large tables read mostly by sequential scan."),
    )
    async def recommend_indexes(
        ctx: _Ctx, min_live_tuples: int = indexing.DEFAULT_MIN_LIVE_TUPLES
    ) -> list[dict[str, Any]]:
        recommendations = await indexing.recommend_indexes(_driver(ctx), min_live_tuples=min_live_tuples)
        return [asdict(recommendation) for recommendation in recommendations]

    @server.tool(
        name="fuzzy_search",
        description=(
            "Rank a text column's values by pg_trgm trigram similarity to a "
            "search term. mode='word' (default) matches fragments within "
            "longer text; mode='full' compares whole strings. Reports "
            "available=false if pg_trgm is not installed."
        ),
    )
    async def fuzzy_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        term: str,
        mode: str = textsearch.DEFAULT_FUZZY_MODE,
        limit: int = textsearch.DEFAULT_LIMIT,
        threshold: float = textsearch.DEFAULT_THRESHOLD,
    ) -> dict[str, Any]:
        result = await textsearch.fuzzy_search(
            _driver(ctx), schema, table, column, term, mode=mode, limit=limit, threshold=threshold
        )
        return asdict(result)

    @server.tool(
        name="full_text_search",
        description=(
            "Rank a text column's documents against a full-text query using "
            "PostgreSQL's built-in tsvector/tsquery. The query accepts "
            "web-search syntax (quoted phrases, or, - exclusion)."
        ),
    )
    async def full_text_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        search_query: str,
        config: str = textsearch.DEFAULT_TEXT_CONFIG,
        limit: int = textsearch.DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        matches = await textsearch.full_text_search(
            _driver(ctx), schema, table, column, search_query, config=config, limit=limit
        )
        return [asdict(match) for match in matches]

    @server.tool(
        name="vector_search",
        description=(
            "Find the rows nearest to a query vector by pgvector distance "
            "(metric: l2, cosine, or inner_product). Reports available=false "
            "if the pgvector extension is not installed."
        ),
    )
    async def vector_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        query_vector: list[float],
        metric: str = textsearch.DEFAULT_VECTOR_METRIC,
        limit: int = textsearch.DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        result = await textsearch.vector_search(
            _driver(ctx), schema, table, column, query_vector, metric=metric, limit=limit
        )
        return asdict(result)

    @server.tool(
        name="geo_search",
        description=(
            "Find the rows nearest to a lon/lat point by PostGIS distance. "
            "Reports available=false if the postgis extension is not installed."
        ),
    )
    async def geo_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        longitude: float,
        latitude: float,
        limit: int = textsearch.DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        result = await textsearch.geo_search(_driver(ctx), schema, table, column, longitude, latitude, limit=limit)
        return asdict(result)


def _register_write(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_write",
        description=(
            "Execute a single INSERT, UPDATE, or DELETE statement in a "
            "read-write transaction. Add a RETURNING clause to receive "
            "affected rows. Available only in unrestricted access mode."
        ),
    )
    async def run_write(ctx: _Ctx, sql: str) -> dict[str, Any]:
        result = await write.run_write(_driver(ctx), sql)
        return asdict(result)


def _register_ddl(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_ddl",
        description=(
            "Execute a single DDL statement (CREATE/ALTER/DROP and related). "
            "Available only in unrestricted access mode with MCPG_ALLOW_DDL enabled."
        ),
    )
    async def run_ddl(ctx: _Ctx, sql: str) -> dict[str, Any]:
        result = await write.run_ddl(_driver(ctx), sql)
        return asdict(result)

    @server.tool(
        name="enable_extension",
        description=(
            "Enable a known PostgreSQL extension (CREATE EXTENSION IF NOT "
            "EXISTS). Only allowlisted extensions may be enabled. Available "
            "only in unrestricted access mode with MCPG_ALLOW_DDL enabled."
        ),
    )
    async def enable_extension(ctx: _Ctx, name: str) -> dict[str, Any]:
        result = await extensions.enable_extension(_driver(ctx), name)
        return asdict(result)


def register_tools(server: FastMCP[AppContext], settings: Settings) -> None:
    """Register the MCP tools permitted by the configured access mode.

    ``get_server_info`` is always available. Read tools (introspection,
    queries) are exposed whenever the READ capability is permitted, which is
    every mode. Write tools require the WRITE capability — unrestricted mode.
    The DDL tool additionally requires the ``MCPG_ALLOW_DDL`` opt-in.
    """
    _register_server_info(server)
    if is_permitted(settings.access_mode, Capability.READ):
        _register_introspection(server)
        _register_query(server)
        _register_health(server)
    if is_permitted(settings.access_mode, Capability.WRITE):
        _register_write(server)
    if is_permitted(settings.access_mode, Capability.DDL) and settings.allow_ddl:
        _register_ddl(server)
