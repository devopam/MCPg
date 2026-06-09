"""MCP tool definitions for MCPg.

Tool *logic* lives in dedicated modules (e.g. ``mcpg.introspection``) and is
unit-tested directly. This module holds the thin MCP wrappers and
``register_tools``, which ``create_server`` calls.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, TypeVar, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from mcpg import (
    __version__,
    advisors,
    audit,
    audit_trail,
    composite,
    cron,
    cursors,
    cypher,
    data_movement,
    diagrams,
    diesel,
    drizzle,
    ecto,
    ent,
    extensions,
    graph,
    graph_diagram,
    graph_mgmt,
    health,
    indexing,
    introspection,
    io_stats,
    jooq,
    liveops,
    locks,
    maintenance,
    migration_history,
    migration_ingestion,
    migrations,
    naming,
    nl2sql,
    partman,
    prisma,
    query,
    rag_efficiency,
    rag_telemetry,
    rls,
    schema_diff,
    schema_docs,
    shell,
    sqlalchemy_export,
    sqlc,
    test_data,
    textsearch,
    timescaledb,
    turboquant,
    vector_ops,
    vector_tuner_advanced,
    vector_tuning,
    walinspect,
    workload,
    write,
)
from mcpg._vendor.sql import SqlDriver
from mcpg.config import Settings
from mcpg.context import AppContext
from mcpg.policy import Capability, is_permitted

# The MCP request context FastMCP injects into every tool.
_Ctx = Context[ServerSession, AppContext, Any]


def _with_example(description: str, example: str) -> str:
    """Append a one-line invocation example to an MCP tool description.

    Agents picking a tool from a long list of candidates have to
    reason about call shape from the description alone. Tacking a
    canonical example onto the end gives them a concrete starting
    point — a fast win for tools whose argument shape isn't obvious
    from the name (anything with multiple optional params or
    schema/table/column tuples).

    The example is rendered as pseudo-Python (``tool_name(arg=value)``)
    rather than raw MCP JSON because that's how every other tool's
    docstring already speaks and because every MCP client we've seen
    happily translates from the readable form. New tools should
    follow this style: short, one-line, illustrative, no edge cases.
    """
    return f"{description}\n\nExample: `{example}`"


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """High-level facts about a running MCPg server."""

    mcpg_version: str
    access_mode: str
    transport: str
    database_connected: bool
    nl2sql_default_provider: str | None
    nl2sql_available_providers: list[str]


def build_server_info(app: AppContext) -> ServerInfo:
    """Assemble server info from the application context."""
    return ServerInfo(
        mcpg_version=__version__,
        access_mode=app.settings.access_mode.value,
        transport=app.settings.transport.value,
        database_connected=app.database.is_connected,
        nl2sql_default_provider=app.settings.nl2sql_provider,
        nl2sql_available_providers=[p for p, _ in app.settings.nl2sql_api_keys],
    )


T = TypeVar("T")


def _subprocess_limits(settings: Settings) -> shell.SubprocessLimits:
    """Build the subprocess hardening policy from active settings."""
    return shell.SubprocessLimits(
        bin_allowlist=settings.subprocess_bin_allowlist,
        cpu_seconds=settings.subprocess_cpu_seconds,
        memory_mb=settings.subprocess_memory_mb,
    )


async def _cached_call(  # noqa: UP047
    ctx: _Ctx,
    key_prefix: str,
    func: Callable[[], Awaitable[T]],
    *key_args: Any,
) -> T:
    """Execute and cache the result of the given callable if caching is enabled."""
    cache = ctx.request_context.lifespan_context.cache
    if not cache.is_enabled():
        return await func()

    import hashlib
    import json

    from mcpg.tenancy import resolve_role

    settings = ctx.request_context.lifespan_context.settings
    role = resolve_role(settings.default_role) or "none"

    # Hash serialized arguments + request tenant role to prevent key collisions and privilege leak
    arg_bytes = json.dumps({"args": key_args, "role": role}, sort_keys=True).encode("utf-8")
    arg_hash = hashlib.sha256(arg_bytes).hexdigest()
    key = f"{key_prefix}:{arg_hash}"

    cached = await cache.get(key)
    if cached is not None:
        return cast(T, cached)

    result = await func()
    await cache.set(key, result)
    return result


def _check_heavy_diagnostics(ctx: _Ctx, tool_name: str) -> None:
    """Raise a RuntimeError if heavy diagnostics are disabled by settings."""
    settings = ctx.request_context.lifespan_context.settings
    if not settings.enable_heavy_diagnostics:
        raise RuntimeError(
            f"The tool {tool_name!r} has been disabled by the server administrator "
            "(MCPG_ENABLE_HEAVY_DIAGNOSTICS is set to false)."
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

    @server.tool(
        name="get_metrics_exposition",
        description=(
            "Return the in-process Prometheus-format metrics for this MCPg "
            "server. Three series: mcpg_tool_calls_total (counter by tool / "
            "status), mcpg_tool_duration_seconds (histogram by tool with "
            "sum and count). Useful when the HTTP transport's /metrics "
            "endpoint is unreachable (e.g. running over stdio) or to fetch "
            "via the MCP protocol itself."
        ),
    )
    async def get_metrics_exposition(ctx: _Ctx) -> str:
        del ctx  # context unused; tool reads from process-wide singleton
        from mcpg.observability import render_prometheus

        return render_prometheus()


def _register_introspection(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_schemas",
        description=_with_example(
            "List database schemas, excluding PostgreSQL's own schemas unless include_system is true.",
            "list_schemas(include_system=false)",
        ),
    )
    async def list_schemas(ctx: _Ctx, include_system: bool = False) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            schemas = await introspection.list_schemas(_driver(ctx), include_system=include_system)
            return [asdict(schema) for schema in schemas]

        return await _cached_call(ctx, "list_schemas", _run, include_system)

    @server.tool(
        name="list_tables",
        description=_with_example(
            "List the tables and views in a schema, flagging partitioned tables and partitions.",
            "list_tables(schema='public')",
        ),
    )
    async def list_tables(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            tables = await introspection.list_tables(_driver(ctx), schema)
            return [asdict(table) for table in tables]

        return await _cached_call(ctx, "list_tables", _run, schema)

    @server.tool(
        name="describe_table",
        description=_with_example(
            "Describe the columns of a table, in ordinal order.",
            "describe_table(schema='public', table='users')",
        ),
    )
    async def describe_table(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            columns = await introspection.describe_table(_driver(ctx), schema, table)
            return [asdict(column) for column in columns]

        return await _cached_call(ctx, "describe_table", _run, schema, table)

    @server.tool(
        name="list_indexes",
        description=_with_example(
            "List the indexes defined on a table.",
            "list_indexes(schema='public', table='users')",
        ),
    )
    async def list_indexes(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            indexes = await introspection.list_indexes(_driver(ctx), schema, table)
            return [asdict(index) for index in indexes]

        return await _cached_call(ctx, "list_indexes", _run, schema, table)

    @server.tool(
        name="list_constraints",
        description=_with_example(
            "List a table's constraints — primary/foreign keys, unique, check, exclusion.",
            "list_constraints(schema='public', table='orders')",
        ),
    )
    async def list_constraints(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            constraints = await introspection.list_constraints(_driver(ctx), schema, table)
            return [asdict(constraint) for constraint in constraints]

        return await _cached_call(ctx, "list_constraints", _run, schema, table)

    @server.tool(
        name="list_foreign_keys",
        description=_with_example(
            "List foreign keys in a schema, resolved to columns and referenced table.",
            "list_foreign_keys(schema='public')",
        ),
    )
    async def list_foreign_keys(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            fks = await introspection.list_foreign_keys(_driver(ctx), schema)
            return [asdict(fk) for fk in fks]

        return await _cached_call(ctx, "list_foreign_keys", _run, schema)

    @server.tool(
        name="list_views",
        description="List the views and materialized views in a schema, with their definitions.",
    )
    async def list_views(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            views = await introspection.list_views(_driver(ctx), schema)
            return [asdict(view) for view in views]

        return await _cached_call(ctx, "list_views", _run, schema)

    @server.tool(
        name="list_functions",
        description="List the functions and procedures defined in a schema.",
    )
    async def list_functions(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            functions = await introspection.list_functions(_driver(ctx), schema)
            return [asdict(function) for function in functions]

        return await _cached_call(ctx, "list_functions", _run, schema)

    @server.tool(
        name="list_triggers",
        description="List the user-defined triggers on a table.",
    )
    async def list_triggers(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            triggers = await introspection.list_triggers(_driver(ctx), schema, table)
            return [asdict(trigger) for trigger in triggers]

        return await _cached_call(ctx, "list_triggers", _run, schema, table)

    @server.tool(
        name="list_partitions",
        description="Describe how a table is partitioned (strategy and bounds) and list its partitions.",
    )
    async def list_partitions(ctx: _Ctx, schema: str, table: str) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            partition_set = await introspection.list_partitions(_driver(ctx), schema, table)
            return asdict(partition_set)

        return await _cached_call(ctx, "list_partitions", _run, schema, table)

    @server.tool(
        name="list_roles",
        description="List the database roles and their attributes, excluding PostgreSQL's own roles "
        "unless include_system is true.",
    )
    async def list_roles(ctx: _Ctx, include_system: bool = False) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            roles = await introspection.list_roles(_driver(ctx), include_system=include_system)
            return [asdict(role) for role in roles]

        return await _cached_call(ctx, "list_roles", _run, include_system)

    @server.tool(
        name="list_grants",
        description="List the privileges granted on a table — who may do what to it.",
    )
    async def list_grants(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            grants = await introspection.list_grants(_driver(ctx), schema, table)
            return [asdict(grant) for grant in grants]

        return await _cached_call(ctx, "list_grants", _run, schema, table)

    @server.tool(
        name="list_policies",
        description="List the Row-Level-Security policies on a table, and whether row security is enabled.",
    )
    async def list_policies(ctx: _Ctx, schema: str, table: str) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            policy_set = await introspection.list_policies(_driver(ctx), schema, table)
            return asdict(policy_set)

        return await _cached_call(ctx, "list_policies", _run, schema, table)

    @server.tool(
        name="list_sequences",
        description="List the sequences defined in a schema, with their range, increment, and last value.",
    )
    async def list_sequences(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            sequences = await introspection.list_sequences(_driver(ctx), schema)
            return [asdict(sequence) for sequence in sequences]

        return await _cached_call(ctx, "list_sequences", _run, schema)

    @server.tool(
        name="list_enums",
        description="List the enum types in a schema, with their labels in sort order.",
    )
    async def list_enums(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            enums = await introspection.list_enums(_driver(ctx), schema)
            return [asdict(enum) for enum in enums]

        return await _cached_call(ctx, "list_enums", _run, schema)

    @server.tool(
        name="list_domains",
        description="List the domain types in a schema, with base type, default, and check constraints.",
    )
    async def list_domains(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            domains = await introspection.list_domains(_driver(ctx), schema)
            return [asdict(domain) for domain in domains]

        return await _cached_call(ctx, "list_domains", _run, schema)

    @server.tool(
        name="list_composite_types",
        description="List the standalone composite types in a schema with their attributes.",
    )
    async def list_composite_types(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            types = await introspection.list_composite_types(_driver(ctx), schema)
            return [asdict(t) for t in types]

        return await _cached_call(ctx, "list_composite_types", _run, schema)

    @server.tool(
        name="list_foreign_data_wrappers",
        description="List the foreign-data wrappers installed in the database.",
    )
    async def list_foreign_data_wrappers(ctx: _Ctx) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            wrappers = await introspection.list_foreign_data_wrappers(_driver(ctx))
            return [asdict(wrapper) for wrapper in wrappers]

        return await _cached_call(ctx, "list_foreign_data_wrappers", _run)

    @server.tool(
        name="list_foreign_servers",
        description="List the foreign servers defined in the database, with their FDW and options.",
    )
    async def list_foreign_servers(ctx: _Ctx) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            servers = await introspection.list_foreign_servers(_driver(ctx))
            return [asdict(server_info) for server_info in servers]

        return await _cached_call(ctx, "list_foreign_servers", _run)

    @server.tool(
        name="list_foreign_tables",
        description="List the foreign tables in a schema, with their server and options.",
    )
    async def list_foreign_tables(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            tables = await introspection.list_foreign_tables(_driver(ctx), schema)
            return [asdict(table) for table in tables]

        return await _cached_call(ctx, "list_foreign_tables", _run, schema)

    @server.tool(
        name="list_user_mappings",
        description="List role-to-foreign-server mappings; the catch-all appears as user='public'.",
    )
    async def list_user_mappings(ctx: _Ctx) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            mappings = await introspection.list_user_mappings(_driver(ctx))
            return [asdict(mapping) for mapping in mappings]

        return await _cached_call(ctx, "list_user_mappings", _run)

    @server.tool(
        name="list_publications",
        description="List logical-replication publications with the tables and operations they include.",
    )
    async def list_publications(ctx: _Ctx) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            publications = await introspection.list_publications(_driver(ctx))
            return [asdict(publication) for publication in publications]

        return await _cached_call(ctx, "list_publications", _run)

    @server.tool(
        name="list_subscriptions",
        description="List logical-replication subscriptions; requires superuser to see any rows.",
    )
    async def list_subscriptions(ctx: _Ctx) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            subscriptions = await introspection.list_subscriptions(_driver(ctx))
            return [asdict(subscription) for subscription in subscriptions]

        return await _cached_call(ctx, "list_subscriptions", _run)

    @server.tool(name="list_extensions", description="List the extensions installed in the database.")
    async def list_extensions(ctx: _Ctx) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            extensions = await introspection.list_extensions(_driver(ctx))
            return [asdict(extension) for extension in extensions]

        return await _cached_call(ctx, "list_extensions", _run)

    @server.tool(
        name="list_available_extensions",
        description="List every extension available to the database, with whether it is installed.",
    )
    async def list_available_extensions(ctx: _Ctx) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            extensions = await introspection.list_available_extensions(_driver(ctx))
            return [asdict(extension) for extension in extensions]

        return await _cached_call(ctx, "list_available_extensions", _run)

    @server.tool(
        name="list_generated_columns",
        description=(
            "List every GENERATED ALWAYS AS (...) STORED column in a schema, "
            "with its data type, the underlying expression, and whether it's "
            "stored or virtual. PostgreSQL today supports only the stored "
            "form; the kind field is reported anyway so the response shape "
            "is forward-compatible when PG adds virtual columns."
        ),
    )
    async def list_generated_columns(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            cols = await introspection.list_generated_columns(_driver(ctx), schema)
            return [asdict(c) for c in cols]

        return await _cached_call(ctx, "list_generated_columns", _run, schema)

    @server.tool(
        name="list_locks",
        description=(
            "List currently-held and waiting locks, joined with backend "
            "state from pg_stat_activity. Ordered by (granted ASC, pid) so "
            "waiting locks float to the top. Returns lock type, mode, "
            "qualified relation name when applicable, transaction / "
            "virtualxid, the application_name + state + wait event of the "
            "owning backend, and the first 200 chars of its query. Read-only."
        ),
    )
    async def list_locks(ctx: _Ctx, limit: int = locks.DEFAULT_LOCK_LIMIT) -> list[dict[str, Any]]:
        rows = await locks.list_locks(_driver(ctx), limit=limit)
        return [asdict(row) for row in rows]

    @server.tool(
        name="find_blocking_chains",
        description=(
            "Return (blocked, blocking) backend pairs via pg_blocking_pids. "
            "Each row pairs a backend waiting on a Lock with one PID "
            "holding the lock that's preventing progress. Cycles are "
            "possible (A blocks B, B blocks A); render with care. Read-only."
        ),
    )
    async def find_blocking_chains(ctx: _Ctx, limit: int = locks.DEFAULT_BLOCKING_LIMIT) -> list[dict[str, Any]]:
        rows = await locks.find_blocking_chains(_driver(ctx), limit=limit)
        return [asdict(row) for row in rows]

    @server.tool(
        name="walk_blocking_chains",
        description=(
            "Walk and reconstruct the lock-wait graph of the database. Detects deadlock cycles, "
            "traces linear blocking paths to their root blockers, and renders a Mermaid flowchart "
            "representing the lock dependency graph. Read-only."
        ),
    )
    async def walk_blocking_chains(ctx: _Ctx, limit: int = locks.DEFAULT_BLOCKING_LIMIT) -> dict[str, Any]:
        report = await locks.walk_blocking_chains(_driver(ctx), limit=limit)
        return asdict(report)

    @server.tool(
        name="read_pg_stat_io",
        description=(
            "Read the pg_stat_io view (PostgreSQL 16+). Reports per "
            "(backend_type, object, context) cumulative I/O activity — "
            "reads, writes, extends, evictions, hits, fsyncs. Useful for "
            "spotting buffer-cache misses and write amplification. On "
            "PostgreSQL 14 / 15 the view doesn't exist, so the tool "
            "returns available=false and an empty list."
        ),
    )
    async def read_pg_stat_io(ctx: _Ctx) -> dict[str, Any]:
        report = await io_stats.read_pg_stat_io(_driver(ctx))
        return asdict(report)

    @server.tool(
        name="read_pg_buffercache_summary",
        description=(
            "Read a high-level summary of the PostgreSQL shared buffer cache usage. "
            "Reports total buffers, free/used buffers, dirty buffers, and average usage count. "
            "Requires the pg_buffercache extension. If not installed, returns available=false."
        ),
    )
    async def read_pg_buffercache_summary(ctx: _Ctx) -> dict[str, Any]:
        report = await io_stats.read_pg_buffercache_summary(_driver(ctx))
        return asdict(report)

    @server.tool(
        name="read_pg_buffercache_relations",
        description=(
            "Read the list of database relations taking up the most space in the PostgreSQL shared buffer cache. "
            "Reports buffered size, percentage of shared buffers, percent of relation buffered, "
            "average usage count, and dirty pages. Allows filtering by schema. "
            "Requires the pg_buffercache extension. If not installed, returns available=false."
        ),
    )
    async def read_pg_buffercache_relations(
        ctx: _Ctx,
        schema: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        report = await io_stats.read_pg_buffercache_relations(_driver(ctx), schema=schema, limit=limit)
        return asdict(report)

    @server.tool(
        name="read_pg_wal_records",
        description=(
            "Read Write-Ahead Log (WAL) records information over a specified LSN range. "
            "Requires the pg_walinspect extension. If not installed, returns available=false."
        ),
    )
    async def read_pg_wal_records(
        ctx: _Ctx,
        start_lsn: str,
        end_lsn: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        report = await walinspect.read_pg_wal_records(_driver(ctx), start_lsn, end_lsn, limit)
        return asdict(report)

    @server.tool(
        name="read_pg_wal_stats",
        description=(
            "Read Write-Ahead Log (WAL) record statistics over a specified LSN range, grouped by "
            "resource manager or record type. "
            "Requires the pg_walinspect extension. If not installed, returns available=false."
        ),
    )
    async def read_pg_wal_stats(
        ctx: _Ctx,
        start_lsn: str,
        end_lsn: str | None = None,
        per_record: bool = False,
    ) -> dict[str, Any]:
        report = await walinspect.read_pg_wal_stats(_driver(ctx), start_lsn, end_lsn, per_record)
        return asdict(report)

    @server.tool(
        name="get_compact_schema",
        description=(
            "Return a highly condensed, token-efficient text summary of a schema's "
            "tables, columns, primary keys, nullability, and relations to save context window tokens."
        ),
    )
    async def get_compact_schema(ctx: _Ctx, schema: str) -> str:
        async def _run() -> str:
            return await introspection.get_compact_schema(_driver(ctx), schema)

        return await _cached_call(ctx, "get_compact_schema", _run, schema)

    @server.tool(
        name="read_migration_history",
        description=(
            "Query and summarize historical migrations applied to the database by popular migration "
            "frameworks (Alembic, Flyway, Diesel, Django, Prisma, Golang Migrate, Goose, Sequelize). "
            "Allows filtering by schema."
        ),
    )
    async def read_migration_history(ctx: _Ctx, schema: str | None = None) -> dict[str, Any]:
        report = await migration_history.read_migration_history(_driver(ctx), schema=schema)
        return asdict(report)


def _register_diagrams(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="generate_schema_diagram",
        description=_with_example(
            "Render a Mermaid ER diagram for a schema. Views and foreign tables are "
            "excluded; partitions are excluded by default — pass include_partitions=true "
            "to draw each partition as its own entity.",
            "generate_schema_diagram(schema='public', include_partitions=false)",
        ),
    )
    async def generate_schema_diagram(ctx: _Ctx, schema: str, include_partitions: bool = False) -> str:
        _check_heavy_diagnostics(ctx, "generate_schema_diagram")

        async def _run() -> str:
            return await diagrams.generate_schema_diagram(_driver(ctx), schema, include_partitions=include_partitions)

        return await _cached_call(ctx, "generate_schema_diagram", _run, schema, include_partitions)

    @server.tool(
        name="generate_fk_cascade_graph",
        description=(
            "Build a Mermaid graph LR of foreign-key cascade chains in a "
            "schema. Each edge runs from the referencing table to the "
            "referenced table, labelled with the cascade action(s) on "
            "DELETE / UPDATE. By default only FKs with at least one "
            "CASCADE / SET NULL / SET DEFAULT action are included — "
            "those are the ones that produce a write blast radius. Pass "
            "include_all=true to include NO ACTION / RESTRICT FKs too "
            "(full FK topology view). Cross-schema FK targets are "
            "rendered as separate nodes prefixed with their schema."
        ),
    )
    async def generate_fk_cascade_graph(ctx: _Ctx, schema: str, include_all: bool = False) -> str:
        _check_heavy_diagnostics(ctx, "generate_fk_cascade_graph")

        async def _run() -> str:
            return await diagrams.generate_fk_cascade_graph(_driver(ctx), schema, include_all=include_all)

        return await _cached_call(ctx, "generate_fk_cascade_graph", _run, schema, include_all)

    @server.tool(
        name="generate_schema_docs",
        description=_with_example(
            "Generate a detailed Markdown reference of a schema's "
            "tables, columns, constraints, indexes, views, foreign tables, "
            "and custom enums along with comments / descriptions. Optional "
            "include_samples fetches a few distinct, non-null values for each column.",
            "generate_schema_docs(schema='public', include_samples=true)",
        ),
    )
    async def generate_schema_docs(ctx: _Ctx, schema: str, include_samples: bool = False) -> str:
        _check_heavy_diagnostics(ctx, "generate_schema_docs")

        async def _run() -> str:
            return await schema_docs.generate_schema_docs(_driver(ctx), schema, include_samples=include_samples)

        return await _cached_call(ctx, "generate_schema_docs", _run, schema, include_samples)


def _register_schema_diff(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="compare_schemas",
        description=_with_example(
            "Return the structural diff between two schemas — tables/columns/"
            "indexes/constraints/foreign-keys added, removed, or changed. "
            "Base tables only; views and custom types are not compared. "
            "Renames surface as a paired add + remove.",
            "compare_schemas(left_schema='public', right_schema='staging')",
        ),
    )
    async def compare_schemas(ctx: _Ctx, left_schema: str, right_schema: str) -> dict[str, Any]:
        diff = await schema_diff.compare_schemas(_driver(ctx), left_schema, right_schema)
        return asdict(diff)


def _register_rag_efficiency(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="analyze_vector_search_efficiency",
        description=(
            "Cross-backend retrieval-quality report for a pgvector or "
            "pg_turboquant ANN index. Detects the backend (HNSW / IVFFlat / "
            "turboquant), sweeps the matching per-backend knob "
            "(ef_search / probes / candidate_limit) across a multiplier "
            "curve, computes recall@k vs a brute-force exact baseline, "
            "Spearman + Kendall rank correlation, per-query p50/p95 "
            "wall-clock latency, and (for turboquant) the page-pruning "
            "ratio from tq_last_scan_stats. Emits findings: "
            "``baseline_recall_low`` (CRITICAL), ``rerank_lift_flat`` / "
            "``rerank_lift_steep`` / ``ranking_degraded`` / "
            "``pruning_ineffective`` (WARNING). Burns "
            "sample_size x (1 + len(candidate_multipliers)) queries; "
            "ad-hoc diagnostic, not a cron tool. Requires the vector "
            "extension; turboquant-arm metrics require pg_turboquant."
        ),
    )
    async def analyze_vector_search_efficiency(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        id_column: str,
        index_name: str | None = None,
        k: int = 10,
        sample_size: int = 30,
        candidate_multipliers: list[int] | None = None,
        metric: str = "cosine",
    ) -> dict[str, Any]:
        report = await rag_efficiency.analyze_vector_search_efficiency(
            _driver(ctx),
            schema,
            table,
            column,
            id_column,
            index_name=index_name,
            k=k,
            sample_size=sample_size,
            candidate_multipliers=tuple(candidate_multipliers) if candidate_multipliers else (1, 2, 4, 10),
            metric=metric,
        )
        return asdict(report)


def _register_rag_analytics(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="analyze_reranker_lift",
        description=(
            "Per-query Spearman + Kendall correlation between bi-encoder "
            "and cross-encoder ranks, aggregated across queries in the "
            "window. Low correlation = the reranker is actively reordering "
            "(doing real work); high correlation = the reranker mostly "
            "confirms the bi-encoder order. Optional ``model`` / "
            "``retrieval_index`` filters. Surfaces ``reranker_idle`` "
            "(WARNING) when the reranker rarely changes ordering. Reads "
            "from mcpg_rag.rerank_events; returns empty when the table "
            "doesn't exist."
        ),
    )
    async def analyze_reranker_lift(
        ctx: _Ctx,
        days: int = 7,
        model: str | None = None,
        retrieval_index: str | None = None,
    ) -> dict[str, Any]:
        report = await rag_efficiency.analyze_reranker_lift(
            _driver(ctx), days=days, model=model, retrieval_index=retrieval_index
        )
        return asdict(report)

    @server.tool(
        name="analyze_topk_stability",
        description=(
            "Jaccard overlap between top-K-by-bi-rank and top-K-by-cross-rank "
            "per query, aggregated. High mean Jaccard means the reranker "
            "isn't actually changing the top-K membership. Surfaces "
            "``topk_stable`` (WARNING) when the rerank is barely earning "
            "its place at this K. Reads from mcpg_rag.rerank_events."
        ),
    )
    async def analyze_topk_stability(
        ctx: _Ctx,
        days: int = 7,
        k: int = 10,
        model: str | None = None,
        retrieval_index: str | None = None,
    ) -> dict[str, Any]:
        report = await rag_efficiency.analyze_topk_stability(
            _driver(ctx), days=days, k=k, model=model, retrieval_index=retrieval_index
        )
        return asdict(report)

    @server.tool(
        name="analyze_rerank_score_distribution",
        description=(
            "Equal-width histogram of cross_encoder_score values over the "
            "window plus the top-decile share. Surfaces ``score_clustering`` "
            "(WARNING) when the reranker isn't discriminating (more than "
            "half of scores land in the top decile of the range). Reads "
            "from mcpg_rag.rerank_events."
        ),
    )
    async def analyze_rerank_score_distribution(
        ctx: _Ctx,
        days: int = 7,
        model: str | None = None,
        n_buckets: int = 20,
    ) -> dict[str, Any]:
        report = await rag_efficiency.analyze_rerank_score_distribution(
            _driver(ctx), days=days, model=model, n_buckets=n_buckets
        )
        return asdict(report)

    @server.tool(
        name="analyze_rerank_ndcg",
        description=(
            "NDCG@k under bi-encoder ordering vs cross-encoder ordering, "
            "averaged across labeled queries (``ground_truth_relevance IS "
            "NOT NULL``). Reports the delta (cross - bi) — positive = the "
            "rerank is adding real ranking quality, negative = it's hurting. "
            "Surfaces ``rerank_hurts_ndcg`` (CRITICAL) or "
            "``rerank_lifts_ndcg`` (GOOD evidence). Reads from "
            "mcpg_rag.rerank_events; returns zero counts when no labeled "
            "rows exist in the window."
        ),
    )
    async def analyze_rerank_ndcg(
        ctx: _Ctx,
        days: int = 7,
        k: int = 10,
        model: str | None = None,
        retrieval_index: str | None = None,
    ) -> dict[str, Any]:
        report = await rag_efficiency.analyze_rerank_ndcg(
            _driver(ctx), days=days, k=k, model=model, retrieval_index=retrieval_index
        )
        return asdict(report)

    @server.tool(
        name="recommend_rerank_strategy",
        description=(
            "Roll-up advisor over the four analytics for one window. "
            "Returns a single headline ``summary`` + the full list of "
            "findings. Built from whichever combination of "
            "``reranker_idle`` / ``topk_stable`` / ``score_clustering`` / "
            "``rerank_hurts_ndcg`` / ``rerank_lifts_ndcg`` fires. Also "
            "feeds the ``RAG Reranker Pipeline`` category in audit_database. "
            "Reads from mcpg_rag.rerank_events."
        ),
    )
    async def recommend_rerank_strategy(
        ctx: _Ctx,
        days: int = 7,
        retrieval_index: str | None = None,
    ) -> dict[str, Any]:
        report = await rag_efficiency.recommend_rerank_strategy(
            _driver(ctx), days=days, retrieval_index=retrieval_index
        )
        return asdict(report)


def _register_vector_tuning(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="tune_vector_index",
        description=(
            "Recommend an ivfflat or hnsw configuration for a pgvector column. "
            "Reads the live row count and column dimension, applies the standard "
            "pgvector heuristics, and returns the parameters plus a ready-to-run "
            "CREATE INDEX statement. Requires the vector extension."
        ),
    )
    async def tune_vector_index(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        index_type: str = "hnsw",
        metric: str = "l2",
    ) -> dict[str, Any]:
        result = await vector_tuning.tune_vector_index(
            _driver(ctx), schema, table, column, index_type=index_type, metric=metric
        )
        return asdict(result)

    @server.tool(
        name="vector_recall_at_k",
        description=(
            "Measure recall@k of an existing pgvector index against a brute-force "
            "ground truth (function-form distance, which pgvector documents as "
            "non-indexed). Returns the mean overlap over a sample of rows from the "
            "table. Requires the vector extension."
        ),
    )
    async def vector_recall_at_k(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        id_column: str,
        k: int = 10,
        sample_size: int = 20,
        metric: str = "l2",
    ) -> dict[str, Any]:
        report = await vector_tuning.vector_recall_at_k(
            _driver(ctx),
            schema,
            table,
            column,
            id_column,
            k=k,
            sample_size=sample_size,
            metric=metric,
        )
        return asdict(report)

    @server.tool(
        name="migrate_vector_to_halfvec",
        description=(
            "Generate a DDL plan that converts a pgvector vector(N) "
            "column to halfvec(N) — halving per-element storage "
            "(4 → 2 bytes) with typically negligible recall impact at "
            "d ≥ 768. Reads the column's current type + dimension from "
            "the catalog, finds every index on the column, and emits an "
            "ordered `migration_sql` plan: DROP each affected index, "
            "ALTER COLUMN to halfvec(N) via a `USING` cast, then "
            "recreate each index with its halfvec opclass. Also returns "
            "a mirror `rollback_sql` that restores the original "
            "vector(N) type plus the original index definitions. "
            "Nothing is executed — feed the plan through the shadow-"
            "migration workflow (`prepare_migration` / "
            "`validate_migration_schema`) before applying. Returns "
            "`already_halfvec=true` (and an empty plan) when the column "
            "is already halfvec, and refuses any index whose opclass "
            "has no halfvec sibling rather than rewriting it "
            "incorrectly. Requires the vector extension."
        ),
    )
    async def migrate_vector_to_halfvec(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
    ) -> dict[str, Any]:
        plan = await vector_tuning.migrate_vector_to_halfvec(_driver(ctx), schema, table, column)
        return asdict(plan)

    @server.tool(
        name="analyze_hnsw_recall",
        description=(
            "Sweeps ef_search values to measure the latency and recall trade-off curve "
            "for a given pgvector query vector against exact brute-force ground truth. "
            "Requires the vector extension."
        ),
    )
    async def analyze_hnsw_recall(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        query_vector: list[float] | str,
        k: int = 10,
        metric: str = "l2",
    ) -> list[dict[str, Any]]:
        return await vector_tuner_advanced.analyze_hnsw_recall(
            _driver(ctx),
            schema,
            table,
            column,
            query_vector,
            k=k,
            metric=metric,
        )

    @server.tool(
        name="analyze_distance_metric",
        description=(
            "Recommend a pgvector distance metric (cosine | l2 | "
            "inner_product) from the embedding-magnitude distribution. "
            "Samples up to `sample_size` non-NULL rows of "
            "schema.table.column, computes each embedding's L2 norm, "
            "and applies a small heuristic: pre-normalised (CV < 5% "
            "and mean ≈ 1.0) → inner_product; nearly-constant magnitude "
            "but not unit-norm → cosine (same ranking as L2, safer "
            "default); variable magnitude → cosine (normalises out "
            "heterogeneous sources). Returns the metric + a rationale + "
            "the underlying distribution stats. Reports available=false "
            "if the pgvector extension is not installed."
        ),
    )
    async def analyze_distance_metric(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        sample_size: int = vector_ops.DEFAULT_SAMPLE_SIZE,
    ) -> dict[str, Any]:
        result = await vector_ops.analyze_distance_metric(
            _driver(ctx),
            schema,
            table,
            column,
            sample_size=sample_size,
        )
        return asdict(result)

    @server.tool(
        name="cross_table_similarity",
        description=(
            "Find the k rows in target_schema.target_table most "
            "similar to a specific row in source_schema.source_table. "
            "Locates the source row via source_id_column = "
            "source_id_value, reads its embedding from "
            "source_embedding_column, then issues a pgvector k-NN "
            "query against target_embedding_column. Both columns must "
            "be vector(N) of the same N — verified from the catalog "
            "up front so a mismatch fails with a clear error rather "
            "than a cast error. Useful for entity-resolution / linking "
            "across tables whose embeddings come from different models "
            "but share a dimension. Returns "
            "source_embedding_found=false when no row matches the id "
            "value. Reports available=false if pgvector is not installed."
        ),
    )
    async def cross_table_similarity(
        ctx: _Ctx,
        source_schema: str,
        source_table: str,
        source_embedding_column: str,
        source_id_column: str,
        source_id_value: Any,
        target_schema: str,
        target_table: str,
        target_embedding_column: str,
        k: int = vector_ops.DEFAULT_K,
        metric: str = "l2",
    ) -> dict[str, Any]:
        result = await vector_ops.cross_table_similarity(
            _driver(ctx),
            source_schema=source_schema,
            source_table=source_table,
            source_embedding_column=source_embedding_column,
            source_id_column=source_id_column,
            source_id_value=source_id_value,
            target_schema=target_schema,
            target_table=target_table,
            target_embedding_column=target_embedding_column,
            k=k,
            metric=metric,
        )
        return asdict(result)

    @server.tool(
        name="cluster_vectors",
        description=_with_example(
            "k-means cluster a pgvector column. Samples up to "
            "`sample_size` (default 5000) non-NULL rows of "
            "schema.table.embedding_column, runs Lloyd's algorithm "
            "with k-means++ seeding (`seed` for determinism), and "
            "returns `centroids` (one per cluster, with size) + "
            "`assignments` (per-row cluster index + distance). When "
            "`id_column` is set each assignment carries that column's "
            "value; otherwise the row's positional sample index. "
            "metric='l2' (default — squared Euclidean) or 'cosine' "
            "(vectors normalised; centroids re-normalised every "
            "iteration). `k` >= 2 and there must be at least 2k "
            "parseable rows. Reports available=false if pgvector is "
            "not installed.",
            "cluster_vectors(schema='public', table='docs', embedding_column='embedding', k=8, metric='cosine')",
        ),
    )
    async def cluster_vectors(
        ctx: _Ctx,
        schema: str,
        table: str,
        embedding_column: str,
        k: int,
        id_column: str | None = None,
        sample_size: int = vector_ops.DEFAULT_CLUSTER_SAMPLE_SIZE,
        max_iterations: int = vector_ops.DEFAULT_MAX_ITERATIONS,
        metric: str = "l2",
        seed: int = 42,
    ) -> dict[str, Any]:
        result = await vector_ops.cluster_vectors(
            _driver(ctx),
            schema,
            table,
            embedding_column,
            k=k,
            id_column=id_column,
            sample_size=sample_size,
            max_iterations=max_iterations,
            metric=metric,
            seed=seed,
        )
        return asdict(result)

    @server.tool(
        name="detect_vector_outliers",
        description=(
            "Flag pgvector rows whose embedding sits far from any "
            "cluster centroid. Samples up to `sample_size` (default "
            "5000) non-NULL rows of schema.table.embedding_column, "
            "clusters them with k-means (same engine as "
            "`cluster_vectors`), then per cluster computes a z-score "
            "on the distance from each row to its centroid and flags "
            "rows whose z-score exceeds `zscore_threshold` (default "
            "3.0). Per-cluster scoring catches rows that are "
            "weird-for-their-group rather than weird-overall, which "
            "is usually what 'find outliers' should mean. Returns "
            "`outliers` sorted by z-score descending (capped at "
            "`max_results`), `total_outliers` (the unclipped count), "
            "and `cluster_stats` (per-cluster mean / std of within-"
            "cluster distances). When `id_column` is set each "
            "outlier carries that column's value; otherwise the "
            "row's positional sample index. `k` >= 2 and there must "
            "be at least 2k parseable rows. Reports available=false "
            "if pgvector is not installed."
        ),
    )
    async def detect_vector_outliers(
        ctx: _Ctx,
        schema: str,
        table: str,
        embedding_column: str,
        id_column: str | None = None,
        k: int = vector_ops.DEFAULT_OUTLIER_K,
        zscore_threshold: float = vector_ops.DEFAULT_OUTLIER_ZSCORE,
        sample_size: int = vector_ops.DEFAULT_CLUSTER_SAMPLE_SIZE,
        max_iterations: int = vector_ops.DEFAULT_MAX_ITERATIONS,
        metric: str = "l2",
        seed: int = 42,
        max_results: int = vector_ops.DEFAULT_OUTLIER_MAX_RESULTS,
    ) -> dict[str, Any]:
        result = await vector_ops.detect_vector_outliers(
            _driver(ctx),
            schema,
            table,
            embedding_column,
            id_column=id_column,
            k=k,
            zscore_threshold=zscore_threshold,
            sample_size=sample_size,
            max_iterations=max_iterations,
            metric=metric,
            seed=seed,
            max_results=max_results,
        )
        return asdict(result)

    @server.tool(
        name="monitor_embedding_drift",
        description=_with_example(
            "Compare two time windows of a pgvector column and flag "
            "distributional drift. Samples up to `sample_size` (default "
            "5000) non-NULL embeddings from each window (filtered by "
            "`timestamp_column`), computes the centroid (per-dimension "
            "mean vector) and L2-norm distribution of each, then "
            "reports the cosine distance between the two centroids "
            "(the main drift signal), the relative change in mean / "
            "std of the L2-norm distribution, and a boolean "
            "`drift_detected` that flips when cosine distance exceeds "
            "`drift_threshold` (default 0.05). Each window is treated "
            "as a half-open `[start, end)` interval. Useful for "
            "ops monitoring of embedding pipelines — an upstream "
            "model swap typically shows up as a large centroid "
            "cosine distance even if the norm distribution looks "
            "stable. `insufficient_data` is returned distinctly from "
            "`drift_detected=false` when either window is empty. "
            "Reports `available=false` if pgvector is not installed.",
            "monitor_embedding_drift(schema='public', table='docs', "
            "embedding_column='embedding', timestamp_column='created_at', "
            "baseline_start='2026-01-01', baseline_end='2026-02-01', "
            "current_start='2026-02-01', current_end='2026-03-01')",
        ),
    )
    async def monitor_embedding_drift(
        ctx: _Ctx,
        schema: str,
        table: str,
        embedding_column: str,
        timestamp_column: str,
        baseline_start: str,
        baseline_end: str,
        current_start: str,
        current_end: str,
        sample_size: int = vector_ops.DEFAULT_DRIFT_SAMPLE_SIZE,
        drift_threshold: float = vector_ops.DEFAULT_DRIFT_THRESHOLD,
    ) -> dict[str, Any]:
        report = await vector_ops.monitor_embedding_drift(
            _driver(ctx),
            schema,
            table,
            embedding_column,
            timestamp_column,
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            current_start=current_start,
            current_end=current_end,
            sample_size=sample_size,
            drift_threshold=drift_threshold,
        )
        return asdict(report)


def _register_prisma(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="generate_prisma_schema",
        description=(
            "Read a PostgreSQL schema and emit a valid Prisma `.prisma` schema string "
            "(mirrors `prisma db pull`). Covers tables, columns, primary/foreign keys, "
            "unique constraints, indexes, and enums. Views, foreign tables, partitions, "
            "triggers, functions, and policies are out of scope; unmappable types fall "
            'back to `Unsupported("...")`.'
        ),
    )
    async def generate_prisma_schema(ctx: _Ctx, schema: str) -> str:
        return await prisma.generate_prisma_schema(_driver(ctx), schema)

    @server.tool(
        name="generate_drizzle_schema",
        description=(
            "Read a PostgreSQL schema and emit a Drizzle ORM TypeScript schema "
            "string (drizzle-orm/pg-core). Covers tables, columns with PG-native "
            "types, primary/foreign keys, unique constraints, indexes, defaults, "
            "and enums. Single-column FKs emit column-level .references(); "
            "composite FKs are a documented v1 gap. Views, foreign tables, "
            "partitions, triggers, and functions are out of scope."
        ),
    )
    async def generate_drizzle_schema(ctx: _Ctx, schema: str) -> str:
        return await drizzle.generate_drizzle_schema(_driver(ctx), schema)

    @server.tool(
        name="generate_diesel_schema",
        description=(
            "Read a PostgreSQL schema and emit a Diesel ORM (Rust) schema.rs. "
            "One `table!` macro per table with column SQL types, `Nullable<T>` "
            "for nullable columns, plus `joinable!` declarations for single-"
            "column intra-schema FKs and an `allow_tables_to_appear_in_same_query!` "
            "macro so multi-table joins type-check. Enum types are emitted as "
            "Text-backed wrapper enums in a `pg_enum` module so the output "
            "works without `diesel_derive_enum`. Composite FKs are a "
            "documented v1 gap."
        ),
    )
    async def generate_diesel_schema(ctx: _Ctx, schema: str) -> str:
        return await diesel.generate_diesel_schema(_driver(ctx), schema)

    @server.tool(
        name="generate_jooq_config",
        description=(
            "Read a PostgreSQL schema and emit a jooq-codegen configuration XML "
            "pointing at it. Unlike the other exporters, jOOQ generates Java "
            "code itself from a live database — the artefact here is the "
            "configuration file the user feeds to mvn jooq-codegen:generate "
            "(or the Gradle task). The XML lists every base table explicitly "
            "via an <includes> regex, excludes MCPg's bookkeeping tables, and "
            "emits a <forcedType> for every json / jsonb column so they map "
            "to org.jooq.JSON / org.jooq.JSONB out of the box. Default Java "
            "package is com.example.jooq; override via the target_package arg."
        ),
    )
    async def generate_jooq_config(
        ctx: _Ctx,
        schema: str,
        target_package: str = "com.example.jooq",
        target_directory: str = "src/main/java",
    ) -> str:
        return await jooq.generate_jooq_config(
            _driver(ctx),
            schema,
            target_package=target_package,
            target_directory=target_directory,
        )

    @server.tool(
        name="generate_ent_schemas",
        description=(
            "Read a PostgreSQL schema and emit Ent (Go) Schema struct files — "
            "one .go file per table. Each file exports a struct that lists "
            "field.X(...) calls for every column, edge.To(...) for single-"
            "column intra-schema FKs, and field.Enum().Values() for enum-typed "
            "columns. Composite FKs are a documented v1 gap. Returns a JSON "
            "object {filename: source} so the agent can write each file."
        ),
    )
    async def generate_ent_schemas(ctx: _Ctx, schema: str) -> dict[str, str]:
        return await ent.generate_ent_schemas(_driver(ctx), schema)

    @server.tool(
        name="generate_ecto_schemas",
        description=(
            "Read a PostgreSQL schema and emit Ecto (Elixir) schema modules — "
            "one .ex file per table, named after the singularised table. Each "
            "module uses Ecto.Schema with field declarations, belongs_to for "
            "single-column intra-schema FKs, and timestamps() when both "
            "inserted_at and updated_at exist. The Elixir top-level module is "
            "configurable via app_module (default MyApp). Returns a JSON "
            "object {filename: source} so the agent can write each file."
        ),
    )
    async def generate_ecto_schemas(ctx: _Ctx, schema: str, app_module: str = "MyApp") -> dict[str, str]:
        return await ecto.generate_ecto_schemas(_driver(ctx), schema, app_module=app_module)

    @server.tool(
        name="generate_sqlalchemy_models",
        description=(
            "Read a PostgreSQL schema and emit a SQLAlchemy 2.0 declarative "
            "models file (DeclarativeBase + Mapped[T] + mapped_column). Covers "
            "tables, columns with PG-native types (incl. jsonb via "
            "sqlalchemy.dialects.postgresql.JSONB), primary keys, single-column "
            "FKs via ForeignKey(), unique constraints (column-level + composite "
            "via __table_args__), defaults, and enums (emitted as Python "
            "enum.Enum classes). Composite FKs are a documented v1 gap."
        ),
    )
    async def generate_sqlalchemy_models(ctx: _Ctx, schema: str) -> str:
        return await sqlalchemy_export.generate_sqlalchemy_models(_driver(ctx), schema)

    @server.tool(
        name="generate_sqlc_schema",
        description=(
            "Read a PostgreSQL schema and emit a sqlc-friendly schema.sql "
            "(plain DDL). Order: CREATE SCHEMA, CREATE TYPE for each enum, "
            "CREATE TABLE statements (columns only), ALTER TABLE ADD "
            "CONSTRAINT (PK / unique / check / foreign key in that order), "
            "then CREATE INDEX for non-constraint indexes. The file replays "
            "cleanly against an empty database so FKs land after all "
            "referenced tables exist. In-process — no MCPG_ALLOW_SHELL needed."
        ),
    )
    async def generate_sqlc_schema(ctx: _Ctx, schema: str) -> str:
        return await sqlc.generate_sqlc_schema(_driver(ctx), schema)


def _register_advisors(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_advisors",
        description=(
            "Run a set of catalog-driven advisor rules against a schema and return "
            "the aggregated findings. Rules cover missing primary keys, unindexed "
            "foreign keys, duplicate indexes, and nullable timestamps without time "
            "zone. Advisory only — no writes."
        ),
    )
    async def run_advisors(ctx: _Ctx, schema: str) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "run_advisors")

        async def _run() -> dict[str, Any]:
            report = await advisors.run_advisors(_driver(ctx), schema)
            return asdict(report)

        return await _cached_call(ctx, "run_advisors", _run, schema)

    @server.tool(
        name="find_unused_objects",
        description=(
            "Find tables and indexes with zero scans since pg_stat was last "
            "reset — a strong signal of dead code, but NOT a verdict. Tables "
            "report seq+idx scan counts, write counts, and estimated row "
            "count; indexes report size and definition. Excludes PRIMARY KEY "
            "and UNIQUE indexes (PG needs those regardless of scans). Run "
            "this after the database has been hot for a meaningful period — "
            "fresh stats produce false positives."
        ),
    )
    async def find_unused_objects(ctx: _Ctx, schema: str) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "find_unused_objects")

        async def _run() -> dict[str, Any]:
            report = await advisors.find_unused_objects(_driver(ctx), schema)
            return asdict(report)

        return await _cached_call(ctx, "find_unused_objects", _run, schema)

    @server.tool(
        name="find_sensitive_columns",
        description=(
            "Flag columns whose names or types look like they hold sensitive "
            "data (passwords, tokens, PII, financial info, health records). "
            "Pure heuristic — no row sampling, no value introspection. "
            "Categories: credential, financial, contact, identifier, "
            "health, government_id, location. Each finding carries a "
            "confidence (high / medium / low) so an agent can filter for "
            "a first review pass. Treat as a SIGNAL, not a verdict — "
            "a column named email_template_id matches the email pattern "
            "but isn't itself an email address."
        ),
    )
    async def find_sensitive_columns(ctx: _Ctx, schema: str) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "find_sensitive_columns")

        async def _run() -> dict[str, Any]:
            report = await advisors.find_sensitive_columns(_driver(ctx), schema)
            return asdict(report)

        return await _cached_call(ctx, "find_sensitive_columns", _run, schema)

    @server.tool(
        name="lint_naming_conventions",
        description=(
            "Lint table / column / index naming in a schema. Detects "
            "the majority case style (snake_case / camelCase / "
            "PascalCase / SCREAMING_SNAKE) per schema and per table, "
            "then flags outliers. Also flags indexes whose names do "
            "not start with a conventional prefix (idx_, ix_, pk_, "
            "uq_, fk_ by default). Findings carry the offender's style "
            "and the detected majority — agents can use the style "
            "field to filter for renames vs accept-as-is. Pure read."
        ),
    )
    async def lint_naming_conventions(ctx: _Ctx, schema: str) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "lint_naming_conventions")

        async def _run() -> dict[str, Any]:
            report = await naming.lint_naming_conventions(_driver(ctx), schema)
            return asdict(report)

        return await _cached_call(ctx, "lint_naming_conventions", _run, schema)

    @server.tool(
        name="test_rls_for_role",
        description=(
            "Test what an RLS-bound role can read from a table. Reports "
            "whether RLS is enabled on the table, lists the policies "
            "that apply to the given role, counts the rows the role "
            "can read, and returns up to sample_size rows so the agent "
            "can inspect them. Runs as the target role inside a "
            "READ ONLY transaction — no writes can leak. Pure read."
        ),
    )
    async def test_rls_for_role(
        ctx: _Ctx, schema: str, table: str, role: str, sample_size: int = rls.DEFAULT_RLS_SAMPLE_SIZE
    ) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "test_rls_for_role")

        async def _run() -> dict[str, Any]:
            result = await rls.test_rls_for_role(_driver(ctx), schema, table, role, sample_size=sample_size)
            return asdict(result)

        return await _cached_call(ctx, "test_rls_for_role", _run, schema, table, role, sample_size)

    @server.tool(
        name="generate_test_data",
        description=(
            "Generate synthetic INSERT statements for a table — typed "
            "values respecting column type, NOT NULL, and DEFAULT. "
            "Returns the SQL as strings; does NOT execute it. Useful "
            "for seeding dev / staging environments. The generator is "
            "deterministic when a seed is provided. Foreign keys are "
            "NOT resolved — the caller must pre-seed referenced rows "
            "or drop the FK before applying. Hard cap of 10000 rows "
            "per call. Pure read (the actual writes go through "
            "run_write under unrestricted mode)."
        ),
    )
    async def generate_test_data(
        ctx: _Ctx, schema: str, table: str, rows: int = test_data.DEFAULT_ROW_COUNT, seed: int | None = None
    ) -> dict[str, Any]:
        dataset = await test_data.generate_test_data(_driver(ctx), schema, table, rows=rows, seed=seed)
        return asdict(dataset)

    @server.tool(
        name="optimize_query",
        description=(
            "Analyze a SQL query for syntax anti-patterns and performance issues "
            "using EXPLAIN plan costs and index scans, returning an optimized version."
        ),
    )
    async def optimize_query(ctx: _Ctx, sql: str) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "optimize_query")

        async def _run() -> dict[str, Any]:
            res = await advisors.optimize_query(_driver(ctx), sql)
            return asdict(res)

        return await _cached_call(ctx, "optimize_query", _run, sql)


def _register_composite(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="summarize_table",
        description=_with_example(
            "Return a one-stop snapshot of a table: columns, primary key, "
            "foreign keys, every other constraint, indexes, storage + "
            "row-count + last-vacuum/analyze stats, and (optionally) a "
            "short sample of rows. Replaces what would otherwise be 4-5 "
            "individual tool calls. Set sample_rows=0 on wide / jsonb-"
            "heavy tables where the sample isn't useful.",
            "summarize_table(schema='public', table='users', sample_rows=5)",
        ),
    )
    async def summarize_table(ctx: _Ctx, schema: str, table: str, sample_rows: int = 5) -> dict[str, Any]:
        result = await composite.summarize_table(_driver(ctx), schema, table, sample_rows=sample_rows)
        return asdict(result)

    @server.tool(
        name="why_is_this_slow",
        description=_with_example(
            "Diagnose why a SQL query might be slow, in one call. Runs "
            "EXPLAIN (FORMAT JSON) — does NOT execute the query — walks the "
            "plan tree, snapshots concurrent active queries + blocking "
            "lock pairs, reads the cluster-wide cache hit ratio, and "
            "produces categorised suggestions (plan / contention / cache / "
            "maintenance). Read-only; safe to run on a statement the agent "
            "doesn't want to materialise yet.",
            "why_is_this_slow(sql='SELECT * FROM orders WHERE customer_id = 42')",
        ),
    )
    async def why_is_this_slow(ctx: _Ctx, sql: str) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "why_is_this_slow")

        async def _run() -> dict[str, Any]:
            result = await composite.why_is_this_slow(_driver(ctx), sql)
            return asdict(result)

        return await _cached_call(ctx, "why_is_this_slow", _run, sql)


def _register_data_movement(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="export_query",
        description=_with_example(
            "Run a read-only SQL query and serialise its rows to CSV or JSON. "
            "Reuses the SQL-safety checks of run_select. Truncates at `limit` "
            "rows and flags it in the result so callers can paginate.",
            "export_query(sql='SELECT id, email FROM users', format='csv', limit=10000)",
        ),
    )
    async def export_query(
        ctx: _Ctx, sql: str, format: str = "csv", limit: int = data_movement.DEFAULT_EXPORT_LIMIT
    ) -> dict[str, Any]:
        result = await data_movement.export_query(_driver(ctx), sql, format=format, limit=limit)
        return asdict(result)

    @server.tool(
        name="export_table",
        description=(
            "Serialise every row in schema.table (up to `limit`) to CSV or JSON. "
            "Schema and table names must be plain identifiers."
        ),
    )
    async def export_table(
        ctx: _Ctx,
        schema: str,
        table: str,
        format: str = "csv",
        limit: int = data_movement.DEFAULT_EXPORT_LIMIT,
    ) -> dict[str, Any]:
        result = await data_movement.export_table(_driver(ctx), schema, table, format=format, limit=limit)
        return asdict(result)


def _register_data_movement_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="import_csv",
        description=(
            "Bulk-load CSV content into schema.table via COPY ... FROM STDIN. "
            "The CSV text is sent verbatim — the caller is responsible for "
            "correctness (matching column count, proper quoting). `header=true` "
            "skips the first line. Optional `columns` restricts loading to the "
            "named columns in order; unlisted columns take their default. "
            "Performs writes — requires unrestricted mode."
        ),
    )
    async def import_csv(
        ctx: _Ctx,
        schema: str,
        table: str,
        content: str,
        header: bool = True,
        delimiter: str = ",",
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        database = ctx.request_context.lifespan_context.database
        result = await data_movement.import_csv(
            database,
            schema,
            table,
            content,
            header=header,
            delimiter=delimiter,
            columns=columns,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="import_json",
        description=(
            "Bulk-load a JSON array of objects into schema.table. Parses the "
            "array, derives column names from the first row (or from `columns` "
            "when given), and runs a parametrised INSERT once per row. Nested "
            "dict/list values are JSON-serialised so they round-trip into jsonb "
            "columns. Missing keys in later rows bind as NULL. Performs writes "
            "— requires unrestricted mode."
        ),
    )
    async def import_json(
        ctx: _Ctx, schema: str, table: str, content: str, columns: list[str] | None = None
    ) -> dict[str, Any]:
        database = ctx.request_context.lifespan_context.database
        result = await data_movement.import_json(
            database,
            schema,
            table,
            content,
            columns=columns,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="import_vectors",
        description=(
            "Bulk-load embeddings into a pgvector vector(N) column. Reads "
            "the column's declared N from the catalog and validates every "
            "row in `content` against it BEFORE any INSERT — a dimension "
            "mismatch on row 1000 fails the whole call rather than leaving "
            "999 partial inserts behind. format='json' (default) expects a "
            "JSON array of objects whose `embedding_column` field is a list "
            "of numbers or a pgvector text literal; format='csv' expects a "
            "header row with `embedding_column` (and `id_column` when set) "
            "and cells that are bracketed literals or comma-separated "
            "numbers. When `id_column` is given, the parallel column receives "
            "each row's identifier. Errors when the column isn't pgvector "
            "vector(N) (so dimension validation can't run). Performs writes "
            "— requires unrestricted mode."
        ),
    )
    async def import_vectors(
        ctx: _Ctx,
        schema: str,
        table: str,
        embedding_column: str,
        content: str,
        format: str = "json",
        id_column: str | None = None,
    ) -> dict[str, Any]:
        database = ctx.request_context.lifespan_context.database
        result = await data_movement.import_vectors(
            database,
            schema,
            table,
            embedding_column,
            content,
            format=format,
            id_column=id_column,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def _register_migrations(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="prepare_migration",
        description=_with_example(
            "Stage a candidate migration against a shadow clone of `target_schema`. "
            "Replicates the target schema's structure into mcpg_shadow_<id>, applies "
            "`candidate_sql` there, then runs compare_schemas(target, shadow) so the "
            "agent can review the structural diff before completing. Returns the "
            "migration id, shadow schema name, TTL, and the diff. Performs DDL — "
            "requires unrestricted mode + MCPG_ALLOW_DDL.",
            "prepare_migration(name='add_user_avatar', target_schema='public', "
            "candidate_sql='ALTER TABLE users ADD COLUMN avatar_url text')",
        ),
    )
    async def prepare_migration(
        ctx: _Ctx, name: str, target_schema: str, candidate_sql: str, ttl_minutes: int = 60
    ) -> dict[str, Any]:
        result = await migrations.prepare_migration(
            _driver(ctx),
            name=name,
            target_schema=target_schema,
            candidate_sql=candidate_sql,
            ttl_minutes=ttl_minutes,
        )
        return {
            "id": result.id,
            "target_schema": result.target_schema,
            "shadow_schema": result.shadow_schema,
            "ttl_expires_at": result.ttl_expires_at.isoformat(),
            "diff": asdict(result.diff),
        }

    @server.tool(
        name="validate_migration",
        description=(
            "Apply candidate_sql to a TRANSIENT shadow of target_schema "
            "that's pre-populated with up to sample_rows_per_table rows "
            "copied from each base table. The shadow is dropped before "
            "returning regardless of outcome. Catches failure modes a "
            "pure structural diff misses: NOT NULL added to a column "
            "with existing NULLs, CHECK constraints violated by live "
            "rows, type narrowings that fail on real values, triggers "
            "that error against actual data. Reports per-table "
            "rows_before / rows_after so the effect of a "
            "DELETE-shaped candidate is visible. error is non-null "
            "iff the candidate raised. Performs DDL — requires "
            "unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def validate_migration(
        ctx: _Ctx,
        target_schema: str,
        candidate_sql: str,
        sample_rows_per_table: int = migrations.DEFAULT_VALIDATION_SAMPLE_ROWS,
    ) -> dict[str, Any]:
        result = await migrations.validate_migration(
            _driver(ctx),
            target_schema=target_schema,
            candidate_sql=candidate_sql,
            sample_rows_per_table=sample_rows_per_table,
        )
        return asdict(result)

    @server.tool(
        name="validate_migration_schema",
        description=(
            "Verify a candidate migration against a reference schema. Clones target_schema's "
            "structure into a transient shadow schema, applies candidate_sql, and compares the "
            "shadow schema with reference_schema using compare_schemas. The shadow is dropped "
            "before returning. Returns whether the candidate applied, any error, and the "
            "structural diff if applied. Performs DDL — requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def validate_migration_schema(
        ctx: _Ctx,
        target_schema: str,
        reference_schema: str,
        candidate_sql: str,
    ) -> dict[str, Any]:
        result = await migrations.validate_migration_schema(
            _driver(ctx),
            target_schema=target_schema,
            reference_schema=reference_schema,
            candidate_sql=candidate_sql,
        )
        return {
            "target_schema": result.target_schema,
            "reference_schema": result.reference_schema,
            "applied": result.applied,
            "error": result.error,
            "diff": asdict(result.diff) if result.diff is not None else None,
        }

    @server.tool(
        name="complete_migration",
        description=(
            "Apply a prepared migration's candidate SQL to its target schema. "
            "Refuses if the migration is not in 'prepared' status or its TTL "
            "has expired. Drops the shadow on success and marks the row "
            "completed. Performs DDL — requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def complete_migration(ctx: _Ctx, migration_id: str) -> dict[str, Any]:
        result = await migrations.complete_migration(_driver(ctx), migration_id)
        await ctx.request_context.lifespan_context.cache.clear()
        return {
            "id": result.id,
            "target_schema": result.target_schema,
            "completed_at": result.completed_at.isoformat(),
            "statements_run": result.statements_run,
        }

    @server.tool(
        name="cancel_migration",
        description=(
            "Drop a prepared migration's shadow schema and mark it cancelled. "
            "Idempotent — calling cancel on a non-existent or already-completed "
            "migration returns shadow_dropped=false without raising."
        ),
    )
    async def cancel_migration(ctx: _Ctx, migration_id: str) -> dict[str, Any]:
        result = await migrations.cancel_migration(_driver(ctx), migration_id)
        return {"id": result.id, "shadow_dropped": result.shadow_dropped}

    @server.tool(
        name="list_unapplied_migration_scripts",
        description=(
            "List on-disk migration scripts that haven't been applied "
            "yet. Walks `scripts_dir` (one level deep) for "
            "framework-specific files — Flyway `V<version>__<desc>.sql`, "
            "Alembic `<revision>_<slug>.py`, Liquibase `<changeset>.sql` "
            "— extracts each script's identifier from its filename, "
            "then cross-references against the framework's history "
            "table (`flyway_schema_history`, `alembic_version`, "
            "`databasechangelog`). Returns the pending list, the "
            "applied identifiers, and a one-line first-comment "
            "preview per pending script. `available=false` when no "
            "history table exists yet (greenfield database); the "
            "pending list still surfaces every on-disk script so a "
            "from-scratch plan is possible. Read-only DB-side; "
            "filesystem access is gated by "
            "`MCPG_MIGRATION_SCRIPTS_ROOTS` — by default the tool "
            "refuses every path."
        ),
    )
    async def list_unapplied_migration_scripts(
        ctx: _Ctx,
        framework: str,
        scripts_dir: str,
        history_schema: str | None = None,
    ) -> dict[str, Any]:
        settings = ctx.request_context.lifespan_context.settings
        report = await migration_ingestion.list_pending_migrations(
            _driver(ctx),
            framework,
            scripts_dir,
            history_schema=history_schema,
            allowed_roots=settings.migration_scripts_roots,
        )
        return asdict(report)

    @server.tool(
        name="list_pending_migrations",
        description=(
            "List migrations currently in 'prepared' status, newest first. Sweeps "
            "expired entries (drops their shadows, flips status to 'expired') "
            "before listing."
        ),
    )
    async def list_pending_migrations(ctx: _Ctx) -> list[dict[str, Any]]:
        records = await migrations.list_pending_migrations(_driver(ctx))
        return [
            {
                "id": r.id,
                "prepared_at": r.prepared_at.isoformat(),
                "target_schema": r.target_schema,
                "shadow_schema": r.shadow_schema,
                "status": r.status,
                "ttl_expires_at": r.ttl_expires_at.isoformat(),
                "candidate_sql_preview": r.candidate_sql[:200],
            }
            for r in records
        ]


def _register_timescaledb_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_hypertables",
        description=(
            "List every TimescaleDB hypertable visible to the current "
            "role, with chunk count, compression flag, and total size. "
            "Reports available=false when the timescaledb extension is "
            "not installed."
        ),
    )
    async def list_hypertables(ctx: _Ctx) -> dict[str, Any]:
        result = await timescaledb.list_hypertables(_driver(ctx))
        return asdict(result)

    @server.tool(
        name="list_chunks",
        description=(
            "List the chunks of a TimescaleDB hypertable with each chunk's "
            "range_start / range_end and whether it has been compressed. "
            "Empty list when the table is not a hypertable."
        ),
    )
    async def list_chunks(ctx: _Ctx, schema: str, table: str) -> dict[str, Any]:
        result = await timescaledb.list_chunks(_driver(ctx), schema, table)
        return asdict(result)


def _register_timescaledb_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="create_hypertable",
        description=(
            "Convert an existing table into a TimescaleDB hypertable on "
            "`time_column`. Validates schema / table / column names against "
            "the plain-identifier allowlist and the chunk interval against "
            "a TimescaleDB-style pattern (e.g. '7 days', '1 hour'). "
            "Requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def create_hypertable(
        ctx: _Ctx,
        schema: str,
        table: str,
        time_column: str,
        chunk_time_interval: str = "7 days",
        if_not_exists: bool = True,
    ) -> dict[str, Any]:
        result = await timescaledb.create_hypertable(
            _driver(ctx),
            schema,
            table,
            time_column,
            chunk_time_interval=chunk_time_interval,
            if_not_exists=if_not_exists,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="add_compression_policy",
        description=(
            "Enable TimescaleDB column-store compression on a hypertable and "
            "schedule a policy that compresses chunks older than "
            "`compress_after` (e.g. '7 days'). Requires unrestricted mode "
            "+ MCPG_ALLOW_DDL."
        ),
    )
    async def add_compression_policy(
        ctx: _Ctx, schema: str, table: str, compress_after: str = "7 days"
    ) -> dict[str, Any]:
        result = await timescaledb.add_compression_policy(_driver(ctx), schema, table, compress_after=compress_after)
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="add_retention_policy",
        description=(
            "Schedule a TimescaleDB retention policy that drops hypertable "
            "chunks older than `drop_after` (e.g. '30 days'). Requires "
            "unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def add_retention_policy(ctx: _Ctx, schema: str, table: str, drop_after: str = "30 days") -> dict[str, Any]:
        result = await timescaledb.add_retention_policy(_driver(ctx), schema, table, drop_after=drop_after)
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def _register_listen(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="subscribe_channel",
        description=(
            "Open a PostgreSQL LISTEN on `channel` and return a subscription "
            "id. Notifications buffer in process memory; poll for them via "
            "poll_notifications. Channel name must match [A-Za-z_][A-Za-z0-9_]*. "
            "Subscriptions are lost on server restart. Requires unrestricted "
            "mode + MCPG_ALLOW_LISTEN."
        ),
    )
    async def subscribe_channel(ctx: _Ctx, channel: str) -> dict[str, Any]:
        manager = ctx.request_context.lifespan_context.listen_manager
        sub_id = await manager.subscribe(channel)
        return {"subscription_id": sub_id, "channel": channel}

    @server.tool(
        name="poll_notifications",
        description=(
            "Drain up to `max_messages` notifications from `subscription_id`. "
            "When the queue is empty, waits at most `timeout_ms` for the first "
            "notification (0 = return immediately). Each notification carries "
            "{channel, payload, delivered_at, dropped_count}; dropped_count is "
            "non-zero only on the first message after a queue overflow."
        ),
    )
    async def poll_notifications(
        ctx: _Ctx, subscription_id: str, timeout_ms: int = 0, max_messages: int = 100
    ) -> list[dict[str, Any]]:
        manager = ctx.request_context.lifespan_context.listen_manager
        notifications = await manager.poll(subscription_id, timeout_ms=timeout_ms, max_messages=max_messages)
        return [asdict(n) for n in notifications]

    @server.tool(
        name="unsubscribe_channel",
        description=(
            "Remove a subscription. Returns true if it existed. The underlying "
            "LISTEN is dropped when the last subscription on the channel goes "
            "away. Requires unrestricted mode + MCPG_ALLOW_LISTEN."
        ),
    )
    async def unsubscribe_channel(ctx: _Ctx, subscription_id: str) -> dict[str, Any]:
        manager = ctx.request_context.lifespan_context.listen_manager
        removed = await manager.unsubscribe(subscription_id)
        return {"removed": removed}

    @server.tool(
        name="list_notification_subscriptions",
        description=(
            "List active LISTEN subscriptions in this server process as "
            "{subscription_id, channel} pairs. Subscriptions are process-local "
            "and lost on restart."
        ),
    )
    async def list_notification_subscriptions(ctx: _Ctx) -> list[dict[str, str]]:
        manager = ctx.request_context.lifespan_context.listen_manager
        return [{"subscription_id": sub_id, "channel": ch} for sub_id, ch in manager.active_subscriptions()]


def _register_data_movement_shell(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="dump_database",
        description=(
            "Run pg_dump against the connected database and return the SQL dump "
            "as a string (for `format='plain'`) or base64-encoded bytes (for "
            "`custom`/`tar`). Credentials pass through libpq env vars, never on "
            "the command line. The result includes `output_truncated` and "
            "`timed_out` flags so the caller can re-run with a higher cap or a "
            "narrower scope. Performs subprocess execution — requires "
            "unrestricted mode + MCPG_ALLOW_SHELL."
        ),
    )
    async def dump_database(ctx: _Ctx, format: str = "plain", schema_only: bool = False) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        result = await data_movement.dump_database(
            app.settings.database_url,
            timeout_sec=app.settings.shell_timeout_sec,
            max_output_bytes=app.settings.shell_max_output_bytes,
            format=format,
            schema_only=schema_only,
            limits=_subprocess_limits(app.settings),
        )
        return asdict(result)

    @server.tool(
        name="restore_database",
        description=(
            "Restore a dump into the connected database. `format='plain'` pipes "
            "the SQL text in `content` through psql with --single-transaction + "
            "ON_ERROR_STOP; `custom`/`tar` base64-decode `content` and pipe the "
            "bytes through pg_restore. Credentials pass through libpq env vars, "
            "never on the command line. Performs subprocess execution — requires "
            "unrestricted mode + MCPG_ALLOW_SHELL."
        ),
    )
    async def restore_database(ctx: _Ctx, content: str, format: str = "plain") -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        result = await data_movement.restore_database(
            app.settings.database_url,
            content,
            timeout_sec=app.settings.shell_timeout_sec,
            max_output_bytes=app.settings.shell_max_output_bytes,
            format=format,
            limits=_subprocess_limits(app.settings),
        )
        await app.cache.clear()
        return asdict(result)

    @server.tool(
        name="copy_table_between_databases",
        description=(
            "Copy a single table from one database to another by piping "
            "pg_dump (source) into pg_restore (destination). The source URL "
            "is the caller-supplied source_url; the destination is the "
            "configured database URL. Specify at least one of include_schema "
            "/ include_data. Credentials pass through libpq env vars on each "
            "leg, never on the command line. If the captured pg_dump archive "
            "would exceed MCPG_SHELL_MAX_OUTPUT_BYTES, the tool errors BEFORE "
            "pg_restore runs (a truncated custom-format archive cannot be "
            "safely restored). Performs subprocess execution — requires "
            "unrestricted mode + MCPG_ALLOW_SHELL."
        ),
    )
    async def copy_table_between_databases(
        ctx: _Ctx,
        source_url: str,
        schema: str,
        table: str,
        include_schema: bool,
        include_data: bool,
    ) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        result = await data_movement.copy_table_between_databases(
            source_url,
            app.settings.database_url,
            schema,
            table,
            include_schema=include_schema,
            include_data=include_data,
            timeout_sec=app.settings.shell_timeout_sec,
            max_output_bytes=app.settings.shell_max_output_bytes,
            limits=_subprocess_limits(app.settings),
        )
        await app.cache.clear()
        return asdict(result)


def _register_audit_trail(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_audit_events",
        description=(
            "List recent rows from mcpg_audit.events (newest first). Returns an "
            "empty list when MCPG_AUDIT_PERSIST has never been turned on (no "
            "audit table yet). Optionally filter by tool name."
        ),
    )
    async def list_audit_events(ctx: _Ctx, limit: int = 100, tool: str | None = None) -> list[dict[str, Any]]:
        events = await audit_trail.list_audit_events(_driver(ctx), limit=limit, tool=tool)
        return [asdict(event) for event in events]

    @server.tool(
        name="verify_audit_chain",
        description="Verify the HMAC-SHA256 signature chain of persisted audit events in mcpg_audit.events.",
    )
    async def verify_audit_chain(ctx: _Ctx) -> dict[str, Any]:
        from mcpg.audit_integrity import verify_audit_chain as vac

        return await vac(_driver(ctx))


def _register_query(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_select",
        description=_with_example(
            "Validate and run a read-only SQL query. Writes, DDL, and other "
            "unsafe statements are rejected before execution.",
            "run_select(sql='SELECT id, email FROM users LIMIT 10', max_rows=1000)",
        ),
    )
    async def run_select(ctx: _Ctx, sql: str, max_rows: int = query.DEFAULT_MAX_ROWS) -> dict[str, Any]:
        result = await query.run_select(_driver(ctx), sql, max_rows=max_rows)
        return asdict(result)

    @server.tool(
        name="run_select_parallel",
        description=(
            "Run up to parallel_limit read-only SELECTs concurrently. Each "
            "statement is validated by the same safety allowlist as "
            "run_select; one bad query does not abort the others — its "
            "error is captured in its own outcome slot. Useful for "
            "dashboard-style fan-out where round-trip latency dominates "
            "(e.g. fetching counters / aggregates from several tables at "
            "once). Each outcome includes an index so the caller can "
            "correlate results without relying on ordering."
        ),
    )
    async def run_select_parallel(
        ctx: _Ctx,
        statements: list[str],
        max_rows: int = query.DEFAULT_MAX_ROWS,
        parallel_limit: int = query.DEFAULT_PARALLEL_LIMIT,
    ) -> dict[str, Any]:
        result = await query.run_select_parallel(
            _driver(ctx),
            statements,
            max_rows=max_rows,
            parallel_limit=parallel_limit,
        )
        return asdict(result)

    @server.tool(
        name="open_cursor",
        description=(
            "Open a server-side cursor for a SELECT query. The cursor "
            "holds the result set on the server side so an agent can "
            "page through millions of rows without loading them all. "
            "SQL is validated by the same safety allowlist as "
            "run_select. Returns the cursor_id; fetch the rows with "
            "fetch_cursor and close with close_cursor (or let the "
            "5-minute TTL clean up). Hard cap of 16 concurrent cursors."
        ),
    )
    async def open_cursor(ctx: _Ctx, sql: str) -> dict[str, Any]:
        manager = ctx.request_context.lifespan_context.cursor_manager
        info = await manager.open(_driver(ctx), sql)
        return asdict(info)

    @server.tool(
        name="fetch_cursor",
        description=(
            "Fetch the next batch from an open server-side cursor. "
            "exhausted=true means the FETCH returned fewer rows than "
            "requested — stop polling. batch_size defaults to 100; "
            "hard cap is 10000 per call."
        ),
    )
    async def fetch_cursor(
        ctx: _Ctx,
        cursor_id: str,
        batch_size: int = cursors.DEFAULT_FETCH_BATCH,
    ) -> dict[str, Any]:
        manager = ctx.request_context.lifespan_context.cursor_manager
        result = await manager.fetch(cursor_id, batch_size=batch_size)
        return asdict(result)

    @server.tool(
        name="close_cursor",
        description=(
            "Close a server-side cursor and release its dedicated "
            "connection. Idempotent — returns closed=false when the "
            "cursor was not open (already closed, expired, or never "
            "existed)."
        ),
    )
    async def close_cursor(ctx: _Ctx, cursor_id: str) -> dict[str, Any]:
        manager = ctx.request_context.lifespan_context.cursor_manager
        closed = await manager.close(cursor_id)
        return {"cursor_id": cursor_id, "closed": closed}

    @server.tool(
        name="list_cursors",
        description=(
            "List every currently-open server-side cursor with its SQL, "
            "rows_returned so far, age in seconds, and the TTL after "
            "which it'll be auto-closed."
        ),
    )
    async def list_cursors(ctx: _Ctx) -> list[dict[str, Any]]:
        manager = ctx.request_context.lifespan_context.cursor_manager
        infos = await manager.list_open()
        return [asdict(info) for info in infos]

    @server.tool(
        name="explain_query",
        description=_with_example(
            "Return the PostgreSQL execution plan for a query without running "
            "it. The query is validated by the same safety allowlist as run_select.",
            "explain_query(sql='SELECT * FROM orders WHERE created_at > now() - interval \\'7 days\\'')",
        ),
    )
    async def explain_query(ctx: _Ctx, sql: str) -> dict[str, Any]:
        result = await query.explain_query(_driver(ctx), sql)
        return asdict(result)

    @server.tool(
        name="analyze_query_plan",
        description=_with_example(
            "Summarise a query's execution plan: total estimated cost, "
            "estimated rows, node types used, and any sequentially-scanned tables.",
            "analyze_query_plan(sql='SELECT * FROM orders WHERE customer_id = 42')",
        ),
    )
    async def analyze_query_plan(ctx: _Ctx, sql: str) -> dict[str, Any]:
        result = await query.analyze_query_plan(_driver(ctx), sql)
        return asdict(result)

    @server.tool(
        name="translate_nl_to_sql",
        description=_with_example(
            "Translate a natural-language question into a read-only "
            "PostgreSQL query against `schema`. The LLM provider "
            "(anthropic / openai / gemini) sees a compact brief of "
            "the schema (tables, columns, foreign keys) and is "
            "instructed to return JSON with `sql` and `explanation`. "
            "When execute=true, the generated SQL goes through the "
            "SAME safety allowlist as run_select before running — "
            "writes / DDL / multi-statement input are rejected even "
            "if the model produced them. Returns the SQL, model "
            "rationale, and (when executed) rows / columns / "
            "row_count. table_filter narrows the brief to a known "
            "subset when the question is clearly scoped. "
            "`provider`, when supplied, selects which configured "
            "LLM provider to call (use this to route between "
            "anthropic / openai / gemini per-call when multiple are "
            "configured); when omitted, MCPg uses the default "
            "(MCPG_NL2SQL_PROVIDER, otherwise the first available "
            "in preference order anthropic → openai → gemini). Call "
            "get_server_info to see which providers are configured.",
            "translate_nl_to_sql(question='top 10 customers by revenue last month', schema='public', execute=true)",
        ),
    )
    async def translate_nl_to_sql(
        ctx: _Ctx,
        question: str,
        schema: str,
        provider: str | None = None,
        execute: bool = False,
        table_filter: list[str] | None = None,
        max_rows: int = query.DEFAULT_MAX_ROWS,
    ) -> dict[str, Any]:
        settings = ctx.request_context.lifespan_context.settings
        api_keys = dict(settings.nl2sql_api_keys)

        chosen = (provider or settings.nl2sql_provider or "").strip().lower() or None
        if chosen is None:
            # No provider arg AND no default configured AND no vendor keys
            # in the env — provider= alone can't fix this, the operator
            # needs to set at least one vendor API key.
            raise nl2sql.NL2SQLError(
                "translate_nl_to_sql has no provider configured. Set at "
                "least one of ANTHROPIC_API_KEY / OPENAI_API_KEY / "
                "GEMINI_API_KEY (or GOOGLE_API_KEY) in the server's "
                "environment. The tool's provider= argument selects "
                "between providers that are already configured — it can't "
                "supply credentials on its own."
            )
        if not nl2sql.is_valid_provider(chosen):
            raise nl2sql.NL2SQLError(f"unknown NL→SQL provider {chosen!r}; supported: anthropic, openai, gemini")
        api_key = api_keys.get(chosen)
        if api_key is None:
            configured = sorted(api_keys) or ["(none)"]
            raise nl2sql.NL2SQLError(
                f"provider {chosen!r} is not configured (currently configured: "
                f"{', '.join(configured)}). Set {nl2sql.VENDOR_ENV_VAR_HINT[chosen]} "
                "in the environment, or pick a configured provider via the "
                "provider= argument."
            )

        # The operator's MCPG_NL2SQL_MODEL / MCPG_NL2SQL_BASE_URL overrides
        # only apply when this call uses the default provider — overriding
        # an Anthropic-shaped model id on an OpenAI call would just break.
        is_default = chosen == settings.nl2sql_provider
        model = settings.nl2sql_model if (is_default and settings.nl2sql_model) else nl2sql.DEFAULT_MODELS[chosen]
        base_url = settings.nl2sql_base_url if is_default else None

        llm = nl2sql.build_provider(chosen, api_key, base_url=base_url)
        result = await nl2sql.translate_nl_to_sql(
            _driver(ctx),
            provider=llm,
            model=model,
            question=question,
            schema=schema,
            execute=execute,
            table_filter=tuple(table_filter) if table_filter else None,
            max_tokens=settings.nl2sql_max_tokens,
            max_rows=max_rows,
        )
        return asdict(result)


def _register_health(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="check_database_health",
        description=_with_example(
            "Run database health checks: connection utilisation, buffer cache "
            "hit ratio, tables needing vacuum, and invalid indexes.",
            "check_database_health()",
        ),
    )
    async def check_database_health(ctx: _Ctx) -> dict[str, Any]:
        report = await health.check_database_health(_driver(ctx))
        return asdict(report)

    @server.tool(
        name="audit_database",
        description=(
            "Run a deep, comprehensive DBA-level database performance, logs, "
            "and health audit over the specified schema. Scans memory, checkpoints, "
            "temp file spills, contention locks, dead tuple cleanliness, and "
            "optionally scans custom logging tables."
        ),
    )
    async def audit_database(ctx: _Ctx, schema: str, log_table: str | None = None) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "audit_database")

        async def _run() -> dict[str, Any]:
            report = await audit.audit_database(_driver(ctx), schema, log_table=log_table)
            return asdict(report)

        return await _cached_call(ctx, "audit_database", _run, schema, log_table)

    @server.tool(
        name="analyze_workload",
        description=_with_example(
            "Return the slowest queries by mean execution time, via the "
            "pg_stat_statements extension. Reports availability=false if the "
            "extension is not installed.",
            "analyze_workload(limit=10)",
        ),
    )
    async def analyze_workload(ctx: _Ctx, limit: int = workload.DEFAULT_LIMIT) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "analyze_workload")

        async def _run() -> dict[str, Any]:
            report = await workload.analyze_workload(_driver(ctx), limit=limit)
            return asdict(report)

        return await _cached_call(ctx, "analyze_workload", _run, limit)

    @server.tool(
        name="detect_n_plus_one",
        description=(
            "Surface query templates in pg_stat_statements that look like "
            "an N+1 loop: hundreds of calls, each returning at most a row "
            "or two, with meaningful total wall-clock time spent. Returns "
            "the candidates sorted by total time descending so the worst "
            "offender appears first. Thresholds (min_calls, "
            "max_rows_per_call, min_total_ms) are tunable. Treat results "
            "as candidates for investigation, NOT verdicts — a hot "
            "cache-miss pattern on a primary-key lookup can trip the same "
            "shape. Reports availability=false if pg_stat_statements is "
            "not installed."
        ),
    )
    async def detect_n_plus_one(
        ctx: _Ctx,
        min_calls: int = workload.DEFAULT_MIN_CALLS,
        max_rows_per_call: float = workload.DEFAULT_MAX_ROWS_PER_CALL,
        min_total_ms: float = workload.DEFAULT_MIN_TOTAL_MS,
        limit: int = workload.DEFAULT_NPLUSONE_LIMIT,
    ) -> dict[str, Any]:
        _check_heavy_diagnostics(ctx, "detect_n_plus_one")

        async def _run() -> dict[str, Any]:
            report = await workload.detect_n_plus_one(
                _driver(ctx),
                min_calls=min_calls,
                max_rows_per_call=max_rows_per_call,
                min_total_ms=min_total_ms,
                limit=limit,
            )
            return asdict(report)

        return await _cached_call(ctx, "detect_n_plus_one", _run, min_calls, max_rows_per_call, min_total_ms, limit)

    @server.tool(
        name="recommend_indexes",
        description=_with_example(
            "Recommend tables that may benefit from indexing — large tables read mostly by sequential scan.",
            "recommend_indexes(min_live_tuples=10000)",
        ),
    )
    async def recommend_indexes(
        ctx: _Ctx, min_live_tuples: int = indexing.DEFAULT_MIN_LIVE_TUPLES
    ) -> list[dict[str, Any]]:
        _check_heavy_diagnostics(ctx, "recommend_indexes")

        async def _run() -> list[dict[str, Any]]:
            recommendations = await indexing.recommend_indexes(_driver(ctx), min_live_tuples=min_live_tuples)
            return [asdict(recommendation) for recommendation in recommendations]

        return await _cached_call(ctx, "recommend_indexes", _run, min_live_tuples)

    @server.tool(
        name="recommend_index_drops",
        description=_with_example(
            "Sibling of `recommend_indexes` for indexes to remove. Walks "
            "`pg_stat_user_indexes` + `pg_stat_user_tables` for existing "
            "indexes that look like pure cost — large on disk but never "
            "(or barely) scanned. Three reason codes, descending strength: "
            "`never_used` (no recorded idx_scans since the last stats "
            "reset — candidate for drop, but verify before removal), "
            "`scan_no_fetch` "
            "(planner picks it but it returns no rows — usually existence-"
            "check pattern), `rarely_used` (scan rate below "
            "`low_scan_ratio` of the table's total scan activity). "
            "Primary-key / unique / exclusion-constraint indexes are "
            "excluded (dropping those would be a schema change, not a "
            "performance win); indexes below `min_index_size_bytes` are "
            "skipped too. Returns a ready-to-run `DROP INDEX CONCURRENTLY` "
            "statement per candidate. Read-only advisor — execution is on "
            "the operator.",
            "recommend_index_drops(schema='public', min_index_size_bytes=1000000, low_scan_ratio=0.01)",
        ),
    )
    async def recommend_index_drops(
        ctx: _Ctx,
        schema: str | None = None,
        min_index_size_bytes: int = indexing.DEFAULT_MIN_INDEX_SIZE_BYTES,
        low_scan_ratio: float = indexing.DEFAULT_LOW_SCAN_RATIO,
    ) -> list[dict[str, Any]]:
        _check_heavy_diagnostics(ctx, "recommend_index_drops")

        async def _run() -> list[dict[str, Any]]:
            candidates = await indexing.recommend_index_drops(
                _driver(ctx),
                schema=schema,
                min_index_size_bytes=min_index_size_bytes,
                low_scan_ratio=low_scan_ratio,
            )
            return [asdict(c) for c in candidates]

        return await _cached_call(ctx, "recommend_index_drops", _run, schema, min_index_size_bytes, low_scan_ratio)

    @server.tool(
        name="fuzzy_search",
        description=_with_example(
            "Rank a text column's values by pg_trgm trigram similarity to a "
            "search term. mode='word' (default) matches fragments within "
            "longer text; mode='full' compares whole strings. Reports "
            "available=false if pg_trgm is not installed.",
            "fuzzy_search(schema='public', table='users', column='name', term='janne', mode='word')",
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
        description=_with_example(
            "Rank a text column's documents against a full-text query using "
            "PostgreSQL's built-in tsvector/tsquery. The query accepts "
            "web-search syntax (quoted phrases, or, - exclusion).",
            "full_text_search(schema='public', table='articles', column='body', search_query='\"new york\" OR -draft')",
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
        description=_with_example(
            "Find the rows nearest to a query vector by pgvector distance "
            "(metric: l2, cosine, or inner_product). Reports available=false "
            "if the pgvector extension is not installed.",
            "vector_search(schema='public', table='docs', column='embedding', "
            "query_vector=[0.1, 0.2, ...], metric='cosine', limit=10)",
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
        name="vector_range_search",
        description=(
            "Return every row within `max_distance` of a query vector (a "
            "threshold-based query rather than top-k). Useful for de-dup, "
            "similarity gating, and clustering pre-passes. Still ordered by "
            "distance and capped at `limit` to avoid pulling huge result "
            "sets. Reports available=false if the pgvector extension is "
            "not installed."
        ),
    )
    async def vector_range_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        query_vector: list[float],
        max_distance: float,
        metric: str = textsearch.DEFAULT_VECTOR_METRIC,
        limit: int = textsearch.DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        result = await textsearch.vector_range_search(
            _driver(ctx),
            schema,
            table,
            column,
            query_vector,
            max_distance,
            metric=metric,
            limit=limit,
        )
        return asdict(result)

    @server.tool(
        name="mmr_search",
        description=_with_example(
            "Diversity-aware vector search: fetch `fetch_k` nearest "
            "candidates by pgvector distance, then re-rank with Maximal "
            "Marginal Relevance to return `k` rows that are relevant but "
            "not near-duplicates — better LLM context than raw top-k. "
            "`lambda_mult` in [0,1] trades relevance (1.0) for diversity "
            "(0.0); default 0.5. Relevance + diversity are cosine "
            "similarities computed over candidate embeddings, so the "
            "result is independent of the recall-pass `metric`. Each hit "
            "carries its relevance, mmr_score, and selection rank. "
            "Reports available=false if the pgvector extension is not "
            "installed.",
            "mmr_search(schema='public', table='docs', column='embedding', "
            "query_vector=[0.1, ...], k=10, lambda_mult=0.5)",
        ),
    )
    async def mmr_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        query_vector: list[float],
        k: int = textsearch.DEFAULT_LIMIT,
        fetch_k: int | None = None,
        lambda_mult: float = textsearch.DEFAULT_MMR_LAMBDA,
        metric: str = textsearch.DEFAULT_VECTOR_METRIC,
    ) -> dict[str, Any]:
        result = await textsearch.mmr_search(
            _driver(ctx),
            schema,
            table,
            column,
            query_vector,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
            metric=metric,
        )
        return asdict(result)

    @server.tool(
        name="hybrid_search",
        description=_with_example(
            "Combine vector and full-text ranking via reciprocal-rank fusion "
            "(RRF) — pulls candidates from each source, then fuses them so "
            "rows ranked highly in EITHER source surface. Closes the gap "
            "between pure vector (misses keyword/identifier matches) and "
            "pure full-text (misses semantic synonyms). Parameters: "
            "vector_column, text_column, query_vector, text_query, plus "
            "metric / text_config / limit / candidate_pool / rrf_k tunables. "
            "Each match carries vector_rank, fts_rank, the fused rrf_score, "
            "and (when present) the original distance + ts_rank values.",
            "hybrid_search(schema='public', table='docs', "
            "vector_column='embedding', text_column='body', "
            "query_vector=[0.1, ...], text_query='postgresql tuning')",
        ),
    )
    async def hybrid_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        vector_column: str,
        text_column: str,
        query_vector: list[float],
        text_query: str,
        metric: str = textsearch.DEFAULT_VECTOR_METRIC,
        text_config: str = textsearch.DEFAULT_TEXT_CONFIG,
        limit: int = textsearch.DEFAULT_LIMIT,
        candidate_pool: int = 50,
    ) -> dict[str, Any]:
        result = await textsearch.hybrid_search(
            _driver(ctx),
            schema,
            table,
            vector_column,
            text_column,
            query_vector,
            text_query,
            metric=metric,
            text_config=text_config,
            limit=limit,
            candidate_pool=candidate_pool,
        )
        return asdict(result)

    @server.tool(
        name="recommend_vector_quantization",
        description=(
            "Scan a schema for `vector(N)` columns whose storage could be "
            "halved by switching to pgvector v0.7+'s `halfvec(N)` type "
            "(16-bit float). Returns one recommendation per qualifying "
            "column with current vs suggested bytes, the savings ratio, "
            "and a one-line rationale. Skips columns that are already "
            "non-`vector` and small tables where the absolute saving "
            "wouldn't justify the migration."
        ),
    )
    async def recommend_vector_quantization(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        recommendations = await textsearch.recommend_vector_quantization(_driver(ctx), schema)
        return [asdict(rec) for rec in recommendations]

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


def _register_liveops(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_active_queries",
        description=(
            "List the queries currently running on the server, with each "
            "backend's wait event, duration, and the PIDs blocking it."
        ),
    )
    async def list_active_queries(ctx: _Ctx) -> list[dict[str, Any]]:
        queries = await liveops.list_active_queries(_driver(ctx))
        return [asdict(active) for active in queries]

    @server.tool(
        name="verify_connection_encryption",
        description=(
            "Report whether MCPg's own connection to PostgreSQL is "
            "TLS-encrypted, including the negotiated protocol version, "
            "cipher, and key bits (from pg_stat_ssl). Also returns a "
            "cluster-wide tally of encrypted vs unencrypted backends "
            "(a lower bound under non-superuser privileges). Complements "
            "the startup TLS-enforcement check by confirming the live "
            "connection actually came up encrypted."
        ),
    )
    async def verify_connection_encryption(ctx: _Ctx) -> dict[str, Any]:
        return asdict(await liveops.verify_connection_encryption(_driver(ctx)))

    @server.tool(
        name="monitor_index_build",
        description=(
            "Surface every active CREATE INDEX and its progress from "
            "pg_stat_progress_create_index (PG12+, no extension). One "
            "row per build with pid, schema.relation.index_name, the "
            "command, phase label, blocks_done/total, tuples_done/total, "
            "and a computed progress_pct (blocks first, tuples as "
            "fallback, null when neither phase reports a denominator). "
            "Useful next to list_active_queries when an HNSW / IVFFlat "
            "build on a big table is taking longer than expected."
        ),
    )
    async def monitor_index_build(ctx: _Ctx) -> list[dict[str, Any]]:
        builds = await liveops.monitor_index_build(_driver(ctx))
        return [asdict(b) for b in builds]

    @server.tool(
        name="list_replicas",
        description=(
            "Report the health of every configured read replica. Each "
            "entry shows index, password-obfuscated DSN, whether the "
            "replica is currently degraded (skipped from routing), the "
            "last error that took it out, and how many seconds remain "
            "before it's re-probed. Returns an empty list when no "
            "replicas are configured."
        ),
    )
    async def list_replicas(ctx: _Ctx) -> list[dict[str, Any]]:
        db = ctx.request_context.lifespan_context.database
        replica_pool = db.replica_pool
        if replica_pool is None:
            return []
        return [asdict(info) for info in await replica_pool.snapshot()]

    @server.tool(
        name="list_cron_jobs",
        description=(
            "List the pg_cron jobs registered in the database. Returns an empty list when pg_cron is not installed."
        ),
    )
    async def list_cron_jobs(ctx: _Ctx) -> list[dict[str, Any]]:
        jobs = await cron.list_cron_jobs(_driver(ctx))
        return [asdict(job) for job in jobs]


def _register_turboquant_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_turboquant_indexes",
        description=(
            "List every pg_turboquant ANN index in the database along with "
            "the metadata payload that tq_index_metadata() reports for it: "
            "algorithm_version, quantizer_family, residual_sketch_kind, "
            "fast_path_eligible, capability_flags, delta_state, and "
            "maintenance_recommended. Returns an empty list when the "
            "pg_turboquant extension is not installed."
        ),
    )
    async def list_turboquant_indexes(ctx: _Ctx) -> list[dict[str, Any]]:
        infos = await turboquant.list_turboquant_indexes(_driver(ctx))
        return [asdict(info) for info in infos]

    @server.tool(
        name="get_turboquant_index_metadata",
        description=(
            "Fetch the tq_index_metadata payload for a single turboquant "
            "index (schema.index). Documented fields are surfaced as typed "
            "attributes; the full upstream payload is preserved in "
            "``raw_metadata`` so advisors can reach unanticipated fields. "
            "Raises when the extension is not installed or no turboquant "
            "index by that name exists."
        ),
    )
    async def get_turboquant_index_metadata(ctx: _Ctx, schema: str, index: str) -> dict[str, Any]:
        info = await turboquant.get_turboquant_index_metadata(_driver(ctx), schema, index)
        return asdict(info)

    @server.tool(
        name="get_turboquant_heap_stats",
        description=(
            "Return the exact heap row count tq_index_heap_stats() reports "
            "for a single turboquant index (schema.index). The raw upstream "
            "payload is preserved in ``raw`` for any extra counters upstream "
            "may add. Requires the pg_turboquant extension."
        ),
    )
    async def get_turboquant_heap_stats(ctx: _Ctx, schema: str, index: str) -> dict[str, Any]:
        stats = await turboquant.get_turboquant_heap_stats(_driver(ctx), schema, index)
        return asdict(stats)

    @server.tool(
        name="get_turboquant_last_scan_stats",
        description=(
            "Return the backend-local JSON tq_last_scan_stats() reports for "
            "the most recent turboquant scan: score_mode, simd_kernel, "
            "pages_scanned, pages_pruned, plus the raw payload. Returns "
            "``null`` when the extension is absent or no turboquant scan "
            "has run on this connection yet."
        ),
    )
    async def get_turboquant_last_scan_stats(ctx: _Ctx) -> dict[str, Any] | None:
        stats = await turboquant.get_turboquant_last_scan_stats(_driver(ctx))
        return asdict(stats) if stats is not None else None

    @server.tool(
        name="recommend_turboquant_maintenance",
        description=(
            "Walk every pg_turboquant index and emit advisor findings. Rules "
            "currently surfaced: ``prerequisites_unmet`` (CRITICAL — pgvector "
            "is missing) and ``delta_tier_large`` (WARNING — upstream's own "
            "``delta_health.merge_recommended=true`` advisory, emits a "
            "``tq_maintain_index`` suggested_action). Each finding carries a "
            "ready-to-run ``suggested_action`` SQL statement. Returns an empty "
            "list when the extension is not installed. Also feeds the "
            "``pg_turboquant Indexes`` category in audit_database."
        ),
    )
    async def recommend_turboquant_maintenance(ctx: _Ctx) -> list[dict[str, Any]]:
        findings = await turboquant.recommend_turboquant_maintenance(_driver(ctx))
        return [asdict(f) for f in findings]

    @server.tool(
        name="turboquant_approx_candidates",
        description=(
            "Run tq_approx_candidates against a turboquant index — approximate "
            "k-NN retrieval, no exact rerank. ``metric`` is 'cosine' | "
            "'inner_product' | 'l2' (mapped to upstream's runtime metric "
            "text). ``half_precision=True`` switches to the halfvec overload. "
            "``probes`` / ``oversample_factor`` are optional per-query knobs "
            "(consider calling ``recommend_turboquant_query_knobs`` first). "
            "Requires the pg_turboquant extension."
        ),
    )
    async def turboquant_approx_candidates(
        ctx: _Ctx,
        schema: str,
        table: str,
        id_column: str,
        embedding_column: str,
        query_vector: list[float] | str,
        metric: str,
        candidate_limit: int,
        probes: int | None = None,
        oversample_factor: int | None = None,
        half_precision: bool = False,
    ) -> list[dict[str, Any]]:
        candidates = await turboquant.turboquant_approx_candidates(
            _driver(ctx),
            schema,
            table,
            id_column,
            embedding_column,
            query_vector,
            metric,
            candidate_limit,
            probes=probes,
            oversample_factor=oversample_factor,
            half_precision=half_precision,
        )
        return [asdict(c) for c in candidates]

    @server.tool(
        name="turboquant_rerank_candidates",
        description=(
            "Run tq_rerank_candidates against a turboquant index — approximate "
            "retrieval followed by SQL-side exact rerank to ``final_limit`` "
            "results. Returns the candidates with both approximate and exact "
            "ranks / distances. ``half_precision=True`` switches to the "
            "halfvec overload. Requires the pg_turboquant extension."
        ),
    )
    async def turboquant_rerank_candidates(
        ctx: _Ctx,
        schema: str,
        table: str,
        id_column: str,
        embedding_column: str,
        query_vector: list[float] | str,
        metric: str,
        candidate_limit: int,
        final_limit: int,
        probes: int | None = None,
        oversample_factor: int | None = None,
        half_precision: bool = False,
    ) -> list[dict[str, Any]]:
        candidates = await turboquant.turboquant_rerank_candidates(
            _driver(ctx),
            schema,
            table,
            id_column,
            embedding_column,
            query_vector,
            metric,
            candidate_limit,
            final_limit,
            probes=probes,
            oversample_factor=oversample_factor,
            half_precision=half_precision,
        )
        return [asdict(c) for c in candidates]

    @server.tool(
        name="recommend_turboquant_query_knobs",
        description=(
            "Run tq_recommended_query_knobs — per-query knob advisor. Two "
            "modes: plain (just ``candidate_limit`` + optional "
            "``final_limit``) gives generic recommendations; index-aware "
            "(supply both ``index_schema`` and ``index_name``, plus optional "
            "``filter_selectivity``) specialises the recommendations to the "
            "named index's catalog state. Returns ``probes``, "
            "``oversample_factor``, ``max_visited_codes``, "
            "``max_visited_pages`` — pass these to "
            "``turboquant_approx_candidates`` / "
            "``turboquant_rerank_candidates``. Requires pg_turboquant."
        ),
    )
    async def recommend_turboquant_query_knobs(
        ctx: _Ctx,
        candidate_limit: int,
        final_limit: int | None = None,
        index_schema: str | None = None,
        index_name: str | None = None,
        filter_selectivity: float | None = None,
    ) -> dict[str, Any]:
        knobs = await turboquant.recommend_turboquant_query_knobs(
            _driver(ctx),
            candidate_limit,
            final_limit=final_limit,
            index_schema=index_schema,
            index_name=index_name,
            filter_selectivity=filter_selectivity,
        )
        return asdict(knobs)


def _register_turboquant_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="maintain_turboquant_index",
        description=(
            "Run tq_maintain_index on a turboquant index — lightweight "
            "merge / compaction of the physical delta tier. The wrapper "
            "pre-flights that the named index is actually a turboquant "
            "index (catalog lookup on pg_am) before invoking upstream, "
            "so the call can't be turned into a way to probe arbitrary "
            "indexes for error messages. Returns wall-clock timestamps "
            "and elapsed duration measured client-side; the PG return "
            "value of tq_maintain_index is intentionally not parsed "
            "(upstream doesn't document a return shape). Available only "
            "in unrestricted mode; requires pg_turboquant installed."
        ),
    )
    async def maintain_turboquant_index(ctx: _Ctx, schema: str, index: str) -> dict[str, Any]:
        result = await turboquant.maintain_turboquant_index(_driver(ctx), schema, index)
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def _register_rag_telemetry_write(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="log_rerank_event",
        description=(
            "Insert one row into mcpg_rag.rerank_events — one (query, candidate) "
            "pair from a RAG reranker step. ``query_hash`` is the caller-computed "
            "join key (raw bytes; SHA-256 is conventional but not required). "
            "``bi_encoder_score`` may be null (some retrieval paths don't expose "
            "a score); ``cross_encoder_score`` is required. ``extra`` is a "
            "free-form dict serialised as jsonb (caller-specific fields: "
            "latency_ms, variant tag, user_id, etc). Available only in "
            "unrestricted mode; the table must be created first via "
            "``setup_rag_telemetry``."
        ),
    )
    async def log_rerank_event(
        ctx: _Ctx,
        query_hash: bytes,
        retrieval_index: str,
        retrieval_backend: str,
        candidate_id: int,
        bi_encoder_score: float | None,
        bi_encoder_rank: int,
        cross_encoder_score: float,
        cross_encoder_rank: int,
        reranker_model: str,
        used_in_context: bool = False,
        ground_truth_relevance: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await rag_telemetry.log_rerank_event(
            _driver(ctx),
            query_hash=query_hash,
            retrieval_index=retrieval_index,
            retrieval_backend=retrieval_backend,
            candidate_id=candidate_id,
            bi_encoder_score=bi_encoder_score,
            bi_encoder_rank=bi_encoder_rank,
            cross_encoder_score=cross_encoder_score,
            cross_encoder_rank=cross_encoder_rank,
            reranker_model=reranker_model,
            used_in_context=used_in_context,
            ground_truth_relevance=ground_truth_relevance,
            extra=extra,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def _register_rag_telemetry_ddl(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="setup_rag_telemetry",
        description=(
            "Create the ``mcpg_rag`` schema, ``rerank_events`` table, and the "
            "three supporting indexes (occurred_at, query_hash, "
            "(reranker_model, occurred_at)). Idempotent — safe to re-run. "
            "Returns ``{schema_created, table_created, indexes_created}`` so "
            "the caller can tell first-run from no-op. Required before any "
            "``log_rerank_event`` call or the Phase-D reranker analytics. "
            "Performs DDL — requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def setup_rag_telemetry(ctx: _Ctx) -> dict[str, Any]:
        database = ctx.request_context.lifespan_context.database
        result = await rag_telemetry.setup_rag_telemetry(database)
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def _register_turboquant_ddl(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="create_turboquant_index",
        description=(
            "Build a CREATE INDEX … USING turboquant statement under tight "
            "allowlists and run it on autocommit (CONCURRENTLY can't run "
            "inside a transaction). ``metric`` is 'cosine' | 'inner_product' "
            "| 'l2' (mapped to the matching tq_*_ops opclass). Index options "
            "``bits`` (1..64), ``lists`` (0..1_000_000), ``transform`` "
            "(allowlist: 'hadamard'), ``normalized`` (bool) are all optional; "
            "any not supplied are omitted from the WITH clause so upstream's "
            "defaults apply. The rendered CREATE INDEX SQL is returned in "
            "``create_sql`` for auditability. Performs DDL — requires "
            "unrestricted mode + MCPG_ALLOW_DDL; pg_turboquant installed."
        ),
    )
    async def create_turboquant_index(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        index_name: str,
        metric: str,
        bits: int | None = None,
        lists: int | None = None,
        transform: str | None = None,
        normalized: bool | None = None,
        concurrently: bool = True,
    ) -> dict[str, Any]:
        database = ctx.request_context.lifespan_context.database
        result = await turboquant.create_turboquant_index(
            database,
            schema,
            table,
            column,
            index_name,
            metric,
            bits=bits,
            lists=lists,
            transform=transform,
            normalized=normalized,
            concurrently=concurrently,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="reindex_turboquant_index",
        description=(
            "REINDEX a turboquant index. Pre-flight confirms the named "
            "index is actually a turboquant index (catalog lookup on "
            "pg_am) before running. ``concurrently=True`` is the default "
            "and runs on autocommit since REINDEX CONCURRENTLY can't "
            "run inside a transaction. Performs DDL — requires "
            "unrestricted mode + MCPG_ALLOW_DDL; pg_turboquant installed."
        ),
    )
    async def reindex_turboquant_index(
        ctx: _Ctx,
        schema: str,
        index: str,
        concurrently: bool = True,
    ) -> dict[str, Any]:
        database = ctx.request_context.lifespan_context.database
        result = await turboquant.reindex_turboquant_index(database, schema, index, concurrently=concurrently)
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def _register_cron_write(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="schedule_cron_job",
        description=(
            "Register a pg_cron job. ``schedule`` is a cron expression or "
            "pg_cron interval shortcut (e.g. '30 seconds'). Available only "
            "in unrestricted mode; requires pg_cron installed."
        ),
    )
    async def schedule_cron_job(ctx: _Ctx, name: str, schedule: str, command: str) -> dict[str, Any]:
        result = await cron.schedule_cron_job(_driver(ctx), name, schedule, command)
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="unschedule_cron_job",
        description=(
            "Unschedule a pg_cron job by name. Returns ``removed=true`` when the "
            "job existed. Available only in unrestricted mode."
        ),
    )
    async def unschedule_cron_job(ctx: _Ctx, name: str) -> dict[str, Any]:
        removed = await cron.unschedule_cron_job(_driver(ctx), name)
        await ctx.request_context.lifespan_context.cache.clear()
        return {"name": name, "removed": removed}

    @server.tool(
        name="schedule_logical_backup",
        description=(
            "Schedule a recurring pg_dump via pg_cron + COPY TO PROGRAM. The "
            "scheduled job runs pg_dump on the database host's filesystem and "
            "writes the dump to ``destination`` on that host. ``database`` is "
            "required — pg_dump invoked through COPY TO PROGRAM does not "
            "inherit the connection's database and falls back to the OS user "
            "name without ``-d``. ``destination`` must be an absolute POSIX "
            "path with only [A-Za-z0-9_./-] (no shell metacharacters) so it "
            "cannot escape the COPY TO PROGRAM shell string; ``database`` "
            "additionally allows the hyphen common in real DB names. "
            "``format`` is 'plain' | 'custom' | 'tar'; ``compress`` pipes "
            "through gzip; ``port`` defaults to 5432. COPY TO PROGRAM is "
            "PostgreSQL-superuser-only, so the connected role must be "
            "superuser for the scheduled job to succeed at runtime. Available "
            "only in unrestricted mode; requires pg_cron installed."
        ),
    )
    async def schedule_logical_backup(
        ctx: _Ctx,
        name: str,
        schedule: str,
        destination: str,
        database: str,
        format: str = "plain",
        schema_only: bool = False,
        compress: bool = False,
        pg_dump_path: str = "pg_dump",
        port: int = 5432,
    ) -> dict[str, Any]:
        result = await cron.schedule_logical_backup(
            _driver(ctx),
            name,
            schedule,
            destination,
            database,
            format=format,
            schema_only=schema_only,
            compress=compress,
            pg_dump_path=pg_dump_path,
            port=port,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def _register_partman(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="partman_create_parent",
        description=(
            "Register a partitioned table with pg_partman. ``partition_type`` "
            "must be 'range', 'list', or 'native'. Performs DDL — requires "
            "unrestricted mode + MCPG_ALLOW_DDL; pg_partman installed."
        ),
    )
    async def partman_create_parent(
        ctx: _Ctx,
        parent_table: str,
        control_column: str,
        partition_interval: str,
        partition_type: str = "range",
    ) -> dict[str, Any]:
        result = await partman.partman_create_parent(
            _driver(ctx),
            parent_table,
            control_column,
            partition_interval,
            partition_type=partition_type,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="partman_run_maintenance",
        description=(
            "Run pg_partman maintenance — add forward partitions and drop "
            "retired ones. Pass parent_table to scope to one parent; omit "
            "to run for every managed parent. Performs DDL — requires "
            "unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def partman_run_maintenance(ctx: _Ctx, parent_table: str | None = None) -> dict[str, Any]:
        result = await partman.partman_run_maintenance(_driver(ctx), parent_table)
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="partman_drop_partition",
        description=(
            "Drop pg_partman partitions older than ``retention``. Time-controlled "
            "parents take a PG interval (e.g. '30 days'); id-controlled parents "
            "take an integer-like string with control_is_time=false. Returns the "
            "dropped partition names. Performs DDL — requires unrestricted mode "
            "+ MCPG_ALLOW_DDL."
        ),
    )
    async def partman_drop_partition(
        ctx: _Ctx,
        parent_table: str,
        retention: str,
        control_is_time: bool = True,
    ) -> dict[str, Any]:
        dropped = await partman.partman_drop_partition(
            _driver(ctx),
            parent_table,
            retention,
            control_is_time=control_is_time,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return {"parent_table": parent_table, "dropped": dropped}


def _register_write(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_write",
        description=(
            "Execute a single INSERT, UPDATE, or DELETE statement in a "
            "read-write transaction. Add a RETURNING clause to receive "
            "affected rows. Available only in unrestricted access mode. "
            "When MCPG_AUDIT_PERSIST is on, one row is appended to "
            "mcpg_audit.events for every call."
        ),
    )
    async def run_write(ctx: _Ctx, sql: str) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        result = await write.run_write(_driver(ctx), sql, audit_persist=app.settings.audit_persist)
        await app.cache.clear()
        return asdict(result)

    @server.tool(
        name="seed_table_with_sample_data",
        description=(
            "Generate and execute synthetic INSERT statements to seed a table with sample data. "
            "Values respect column types, NOT NULL, and DEFAULT constraints. Foreign keys are NOT "
            "resolved — you must pre-seed referenced rows or drop the FK before seeding. "
            "Hard cap of 10000 rows. Available only in unrestricted access mode."
        ),
    )
    async def seed_table_with_sample_data(
        ctx: _Ctx,
        schema: str,
        table: str,
        rows: int = test_data.DEFAULT_ROW_COUNT,
        seed: int | None = None,
    ) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        result = await test_data.seed_table_with_sample_data(
            _driver(ctx),
            schema=schema,
            table=table,
            rows=rows,
            seed=seed,
        )
        await app.cache.clear()
        return asdict(result)


def _register_maintenance(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_maintenance",
        description=(
            "Run VACUUM or ANALYZE against one table (operation: vacuum, "
            "analyze, or vacuum_analyze). Available only in unrestricted mode."
        ),
    )
    async def run_maintenance(ctx: _Ctx, operation: str, schema: str, table: str) -> dict[str, Any]:
        database = ctx.request_context.lifespan_context.database
        result = await maintenance.run_maintenance(database, operation, schema, table)
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="prune_audit_events",
        description=(
            "Delete persisted audit events older than older_than_days from "
            "mcpg_audit.events — a cron-friendly retention helper for the "
            "otherwise-unbounded audit table. Returns the number deleted, the "
            "cutoff timestamp, and the rows remaining. Refuses to run when "
            "MCPG_AUDIT_INTEGRITY is enabled (pruning would break the HMAC "
            "signature chain). Available only in unrestricted mode."
        ),
    )
    async def prune_audit_events(ctx: _Ctx, older_than_days: int) -> dict[str, Any]:
        settings = ctx.request_context.lifespan_context.settings
        result = await audit_trail.prune_audit_events(
            _driver(ctx),
            older_than_days=older_than_days,
            integrity_enabled=settings.audit_integrity,
        )
        return asdict(result)


def _register_backend_control(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="cancel_query",
        description=(
            "Cancel the query running on a backend PID (pg_cancel_backend); "
            "the connection stays open. Available only in unrestricted mode."
        ),
    )
    async def cancel_query(ctx: _Ctx, pid: int) -> dict[str, Any]:
        result = await liveops.cancel_query(_driver(ctx), pid)
        return asdict(result)

    @server.tool(
        name="terminate_backend",
        description=(
            "Terminate a backend PID (pg_terminate_backend), closing its "
            "connection. Available only in unrestricted mode."
        ),
    )
    async def terminate_backend(ctx: _Ctx, pid: int) -> dict[str, Any]:
        result = await liveops.terminate_backend(_driver(ctx), pid)
        return asdict(result)


def _register_ddl(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="run_ddl",
        description=(
            "Execute a single DDL statement (CREATE/ALTER/DROP and related). "
            "Available only in unrestricted access mode with MCPG_ALLOW_DDL "
            "enabled. When schema+table hints are supplied, the result "
            "includes a before/after column snapshot for that table. When "
            "MCPG_AUDIT_PERSIST is on, one row is appended to "
            "mcpg_audit.events for every call."
        ),
    )
    async def run_ddl(ctx: _Ctx, sql: str, schema: str | None = None, table: str | None = None) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        result = await write.run_ddl(
            _driver(ctx),
            sql,
            audit_persist=app.settings.audit_persist,
            schema=schema,
            table=table,
        )
        await app.cache.clear()
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
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def _register_graphs_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_graphs",
        description="List all active Apache AGE property graphs in the database.",
    )
    async def list_graphs(ctx: _Ctx) -> list[dict[str, Any]]:
        app = ctx.request_context.lifespan_context
        res = await graph.list_graphs(app)
        return [dict(x) for x in res]

    @server.tool(
        name="describe_graph",
        description=("Describe the schema structure, vertex labels, and edge labels of a specific property graph."),
    )
    async def describe_graph(ctx: _Ctx, graph_name: str) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        res = await graph.describe_graph(app, graph_name)
        return dict(res)

    @server.tool(
        name="run_cypher",
        description=(
            "Execute an openCypher query on a specific graph database. "
            "Supports read queries (MATCH) and write/modifying queries "
            "(CREATE, SET, DELETE, MERGE, REMOVE)."
        ),
    )
    async def run_cypher(
        ctx: _Ctx,
        graph_name: str,
        cypher_query: str,
    ) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        res = await cypher.run_cypher(app, graph_name, cypher_query)
        return dict(res)

    @server.tool(
        name="generate_graph_diagram",
        description=(
            "Generate a Mermaid flowchart diagram representing nodes and "
            "relationships in a property graph to visualize its schema and topology."
        ),
    )
    async def generate_graph_diagram(ctx: _Ctx, graph_name: str, limit: int = 50) -> str:
        _check_heavy_diagnostics(ctx, "generate_graph_diagram")

        async def _run() -> str:
            app = ctx.request_context.lifespan_context
            res = await graph_diagram.generate_graph_diagram(app, graph_name, limit=limit)
            return res["mermaid"]

        return await _cached_call(ctx, "generate_graph_diagram", _run, graph_name, limit)


def _register_graphs_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="create_graph",
        description=(
            "Create a new Apache AGE property graph space in the database. "
            "Performs DDL — requires DDL permission enabled."
        ),
    )
    async def create_graph(ctx: _Ctx, graph_name: str) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        res = await graph_mgmt.create_graph(app, graph_name)
        await app.cache.clear()
        return dict(res)

    @server.tool(
        name="drop_graph",
        description=(
            "Delete an Apache AGE property graph space, dropping all its nodes, "
            "edges, and backing tables. Performs DDL — requires DDL permission enabled."
        ),
    )
    async def drop_graph(ctx: _Ctx, graph_name: str, cascade: bool = True) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        res = await graph_mgmt.drop_graph(app, graph_name, cascade=cascade)
        await app.cache.clear()
        return dict(res)


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
        _register_diagrams(server)
        _register_schema_diff(server)
        _register_vector_tuning(server)
        _register_rag_efficiency(server)
        _register_rag_analytics(server)
        _register_prisma(server)
        _register_advisors(server)
        _register_composite(server)
        _register_data_movement(server)
        _register_audit_trail(server)
        _register_query(server)
        _register_health(server)
        _register_liveops(server)
        _register_turboquant_reads(server)
        _register_timescaledb_reads(server)
        _register_graphs_reads(server)
    if is_permitted(settings.access_mode, Capability.WRITE):
        _register_write(server)
        _register_maintenance(server)
        _register_backend_control(server)
        _register_cron_write(server)
        _register_turboquant_writes(server)
        _register_rag_telemetry_write(server)
        _register_data_movement_writes(server)
    if is_permitted(settings.access_mode, Capability.DDL) and settings.allow_ddl:
        _register_ddl(server)
        _register_partman(server)
        _register_turboquant_ddl(server)
        _register_rag_telemetry_ddl(server)
        _register_timescaledb_writes(server)
        _register_graphs_writes(server)
    if is_permitted(settings.access_mode, Capability.MIGRATE) and settings.allow_ddl:
        _register_migrations(server)
    if is_permitted(settings.access_mode, Capability.SHELL) and settings.allow_shell:
        _register_data_movement_shell(server)
    if is_permitted(settings.access_mode, Capability.LISTEN) and settings.allow_listen:
        _register_listen(server)
