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
    aio,
    audit,
    audit_trail,
    composite,
    config_advisor,
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
    logical_replication,
    maintenance,
    migration_history,
    migration_ingestion,
    migrations,
    naming,
    nl2sql,
    partman,
    pg19_ddl,
    pg19_partitions,
    pg19_runtime,
    pg19_skip_scan,
    pg19_stats,
    pg_prewarm,
    pg_search,
    pgq,
    prisma,
    query,
    rag_efficiency,
    rag_telemetry,
    redis_fdw,
    repack,
    rls,
    schema_diff,
    schema_docs,
    session_advisor,
    shell,
    sqlalchemy_export,
    sqlc,
    test_data,
    test_row_factory,
    textsearch,
    timescaledb,
    turboquant,
    vector_ops,
    vector_tuner_advanced,
    vector_tuning,
    wait_for_lsn,
    walinspect,
    warehousepg,
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
    """High-level facts about a running MCPg server.

    ``wal_level`` and ``effective_wal_level`` are sourced from the
    server via ``current_setting('...', true)`` when a driver is
    supplied to :func:`build_server_info`; ``None`` when the database
    isn't connected or the GUC isn't recognised on this PG version
    (PG ≤ 18 doesn't ship ``effective_wal_level``).
    """

    mcpg_version: str
    access_mode: str
    transport: str
    database_connected: bool
    nl2sql_default_provider: str | None
    nl2sql_available_providers: list[str]
    wal_level: str | None = None
    effective_wal_level: str | None = None


async def build_server_info(app: AppContext, *, driver: SqlDriver | None = None) -> ServerInfo:
    """Assemble server info from the application context.

    When ``driver`` is supplied and the database is connected, queries
    the live ``wal_level`` and PG 19's ``effective_wal_level`` GUCs.
    Both fall back to ``None`` on driver errors and on PG ≤ 18 (where
    ``effective_wal_level`` doesn't exist) — the agent can tell the
    cluster's actual emit-state apart from the configured
    intent-state when they diverge.
    """
    wal_level: str | None = None
    effective_wal_level: str | None = None
    if driver is not None and app.database.is_connected:
        try:
            from mcpg.pg19_runtime import _string_setting

            wal_level = await _string_setting(driver, "wal_level")
            effective_wal_level = await _string_setting(driver, "effective_wal_level")
        except Exception:
            # GUC probe failures don't block the rest of the server-info
            # response — the connection may be transiently down or the
            # GUC may not exist on this PG version. Both manifest as
            # `None` in the surfaced result, which matches the "not
            # known on this server" semantics.
            pass
    return ServerInfo(
        mcpg_version=__version__,
        access_mode=app.settings.access_mode.value,
        transport=app.settings.transport.value,
        database_connected=app.database.is_connected,
        nl2sql_default_provider=app.settings.nl2sql_provider,
        nl2sql_available_providers=[p for p, _ in app.settings.nl2sql_api_keys],
        wal_level=wal_level,
        effective_wal_level=effective_wal_level,
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
        description=(
            "Return the MCPg server version, access mode, transport, database "
            "connection status, and PostgreSQL `wal_level` / `effective_wal_level` "
            "(the latter is `None` on PG ≤ 18 where the GUC doesn't exist; on "
            "PG 19+ a divergence between the two indicates a reload is still "
            "pending)."
        ),
    )
    async def get_server_info(ctx: _Ctx) -> dict[str, Any]:
        info = await build_server_info(
            ctx.request_context.lifespan_context,
            driver=_driver(ctx) if ctx.request_context.lifespan_context.database.is_connected else None,
        )
        return asdict(info)

    @server.tool(
        name="describe_self",
        description=(
            "Return a high-level summary of what mcpg can do, organised into "
            "capability buckets (schema introspection, query execution, vector "
            "search, RAG telemetry, audit trail, migrations, time-series, "
            "etc.). Call this first when discovering mcpg's surface — it's "
            "much more compact than walking the full tool catalogue. Returns "
            "an object with `headline`, `version`, `tool_count`, "
            "`capability_count`, and a `capabilities` list, where each "
            "capability has `id`, `name`, `summary`, `detail`, "
            "`headline_tools` (top 3-6 tools to reach for first), `tool_count`, "
            "and `all_tools` (the full list in that bucket). Read-only; no "
            "database access. Pair with `list_tools` (MCP-protocol) when you "
            "need every tool's full schema."
        ),
    )
    async def describe_self(ctx: _Ctx) -> dict[str, Any]:
        del ctx  # purely-static response; no per-request state
        # Pull the live tool list off the FastMCP instance so the per-bucket
        # counts stay accurate even if a stricter flag profile hides some
        # tools. The protected attribute access is intentional — FastMCP
        # doesn't expose a public iterator yet.
        registered_tools = list(await server.list_tools())
        names = [t.name for t in registered_tools]
        from mcpg.about import build_capability_summary

        return build_capability_summary(names)

    @server.tool(
        name="describe_tool",
        description=(
            "Return the full registered schema for one MCP tool by name — "
            "`description`, `input_schema`, `output_schema`, and which "
            "capability bucket it belongs to. Use this when an agent hits "
            "a tool error and needs to verify the call shape without "
            "re-walking the full `describe_self` payload (handy when the "
            "transport surfaces only `tools/call`, not `tools/list`). "
            "Returns `registered=false` plus a `did_you_mean` suggestion "
            "list when the name isn't on this server. Read-only; no "
            "database access. Example: `describe_tool(name='run_select')`"
        ),
    )
    async def describe_tool(ctx: _Ctx, name: str) -> dict[str, Any]:
        del ctx  # purely-static response; no per-request state
        from mcpg.tool_introspection import (
            build_missing_tool_descriptor,
            build_tool_descriptor,
        )

        registered_tools = list(await server.list_tools())
        for tool in registered_tools:
            if tool.name == name:
                return build_tool_descriptor(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.inputSchema,
                    output_schema=tool.outputSchema,
                )
        return build_missing_tool_descriptor(
            name,
            registered_names=[t.name for t in registered_tools],
        )

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
            "List database schemas, excluding PostgreSQL's own schemas unless include_system is true. "
            "Returns a list of objects with `name`.",
            "list_schemas(include_system=false)",
        ),
    )
    async def list_schemas(ctx: _Ctx, include_system: bool = False) -> list[introspection.SchemaInfo]:
        async def _run() -> list[introspection.SchemaInfo]:
            schemas = await introspection.list_schemas(_driver(ctx), include_system=include_system)
            return schemas

        return await _cached_call(ctx, "list_schemas", _run, include_system)

    @server.tool(
        name="list_tables",
        description=_with_example(
            "List the tables and views in a schema, flagging partitioned tables and partitions. "
            "Returns a list of objects with `name`, `type` ('table' or 'view'), `partitioned`, "
            "`is_partition`.",
            "list_tables(schema='public')",
        ),
    )
    async def list_tables(ctx: _Ctx, schema: str) -> list[introspection.TableInfo]:
        async def _run() -> list[introspection.TableInfo]:
            tables = await introspection.list_tables(_driver(ctx), schema)
            return tables

        return await _cached_call(ctx, "list_tables", _run, schema)

    @server.tool(
        name="describe_table",
        description=_with_example(
            "Describe the columns of a table, in ordinal order. "
            "Returns a list of objects with `name`, `data_type`, `nullable`, `default`, "
            "and `vector_dimension` (set only for pgvector `vector(N)` columns).",
            "describe_table(schema='public', table='users')",
        ),
    )
    async def describe_table(ctx: _Ctx, schema: str, table: str) -> list[introspection.ColumnInfo]:
        async def _run() -> list[introspection.ColumnInfo]:
            columns = await introspection.describe_table(_driver(ctx), schema, table)
            return columns

        return await _cached_call(ctx, "describe_table", _run, schema, table)

    @server.tool(
        name="list_indexes",
        description=_with_example(
            "List the indexes defined on a table. "
            "Returns a list of objects with `name`, `method` (btree / gin / gist / brin / hash / "
            "spgist / hnsw / ivfflat / …), `definition` (the CREATE INDEX statement), "
            "and `partitioned`.",
            "list_indexes(schema='public', table='users')",
        ),
    )
    async def list_indexes(ctx: _Ctx, schema: str, table: str) -> list[introspection.IndexInfo]:
        async def _run() -> list[introspection.IndexInfo]:
            indexes = await introspection.list_indexes(_driver(ctx), schema, table)
            return indexes

        return await _cached_call(ctx, "list_indexes", _run, schema, table)

    @server.tool(
        name="list_constraints",
        description=_with_example(
            "List a table's constraints — primary/foreign keys, unique, check, exclusion. "
            "Returns a list of objects with `name`, `type`, and `definition` (the constraint SQL).",
            "list_constraints(schema='public', table='orders')",
        ),
    )
    async def list_constraints(ctx: _Ctx, schema: str, table: str) -> list[introspection.ConstraintInfo]:
        async def _run() -> list[introspection.ConstraintInfo]:
            constraints = await introspection.list_constraints(_driver(ctx), schema, table)
            return constraints

        return await _cached_call(ctx, "list_constraints", _run, schema, table)

    @server.tool(
        name="list_foreign_keys",
        description=_with_example(
            "List foreign keys in a schema, resolved to columns and referenced table. "
            "Returns a list of objects with `name`, `from_table`, `from_columns`, `to_schema`, "
            "`to_table`, `to_columns`.",
            "list_foreign_keys(schema='public')",
        ),
    )
    async def list_foreign_keys(ctx: _Ctx, schema: str) -> list[introspection.ForeignKeyInfo]:
        async def _run() -> list[introspection.ForeignKeyInfo]:
            fks = await introspection.list_foreign_keys(_driver(ctx), schema)
            return fks

        return await _cached_call(ctx, "list_foreign_keys", _run, schema)

    @server.tool(
        name="list_views",
        description=(
            "List the views and materialized views in a schema, with their definitions. "
            "Returns a list of objects with `name`, `materialized` (bool), and `definition` "
            "(the SELECT SQL)."
        ),
    )
    async def list_views(ctx: _Ctx, schema: str) -> list[introspection.ViewInfo]:
        async def _run() -> list[introspection.ViewInfo]:
            views = await introspection.list_views(_driver(ctx), schema)
            return views

        return await _cached_call(ctx, "list_views", _run, schema)

    @server.tool(
        name="list_functions",
        description=(
            "List the functions and procedures defined in a schema. "
            "Returns a list of objects with `name`, `kind` ('function' or 'procedure'), "
            "`arguments` (signature string), `returns` (return-type string), and `language` "
            "(plpgsql / sql / c / etc.)."
        ),
    )
    async def list_functions(ctx: _Ctx, schema: str) -> list[introspection.FunctionInfo]:
        async def _run() -> list[introspection.FunctionInfo]:
            functions = await introspection.list_functions(_driver(ctx), schema)
            return functions

        return await _cached_call(ctx, "list_functions", _run, schema)

    @server.tool(
        name="list_triggers",
        description=(
            "List the user-defined triggers on a table. "
            "Returns a list of objects with `name`, `function` (the called function's "
            "qualified name), and `definition` (the CREATE TRIGGER SQL)."
        ),
    )
    async def list_triggers(ctx: _Ctx, schema: str, table: str) -> list[introspection.TriggerInfo]:
        async def _run() -> list[introspection.TriggerInfo]:
            triggers = await introspection.list_triggers(_driver(ctx), schema, table)
            return triggers

        return await _cached_call(ctx, "list_triggers", _run, schema, table)

    @server.tool(
        name="list_partitions",
        description=(
            "Describe how a table is partitioned (strategy and bounds) and list its partitions. "
            "Returns an object with `partitioned` (bool), `strategy` ('range' / 'list' / 'hash' "
            "or null), and `partitions` (a list of `{name, bounds}` for each partition)."
        ),
    )
    async def list_partitions(ctx: _Ctx, schema: str, table: str) -> introspection.PartitionSet:
        async def _run() -> introspection.PartitionSet:
            partition_set = await introspection.list_partitions(_driver(ctx), schema, table)
            return partition_set

        return await _cached_call(ctx, "list_partitions", _run, schema, table)

    @server.tool(
        name="list_roles",
        description=(
            "List the database roles and their attributes, excluding PostgreSQL's own roles "
            "unless include_system is true. "
            "Returns a list of objects with `name`, `superuser`, `create_role`, `create_db`, "
            "`can_login`, `replication`, `bypass_rls`, `connection_limit`, `member_of`."
        ),
    )
    async def list_roles(ctx: _Ctx, include_system: bool = False) -> list[introspection.RoleInfo]:
        async def _run() -> list[introspection.RoleInfo]:
            roles = await introspection.list_roles(_driver(ctx), include_system=include_system)
            return roles

        return await _cached_call(ctx, "list_roles", _run, include_system)

    @server.tool(
        name="list_grants",
        description=(
            "List the privileges granted on a table — who may do what to it. "
            "Returns a list of objects with `grantee`, `privilege` (SELECT / INSERT / UPDATE "
            "/ DELETE / TRUNCATE / REFERENCES / TRIGGER), `grantable` (bool — may the grantee "
            "regrant), and `grantor`."
        ),
    )
    async def list_grants(ctx: _Ctx, schema: str, table: str) -> list[introspection.GrantInfo]:
        async def _run() -> list[introspection.GrantInfo]:
            grants = await introspection.list_grants(_driver(ctx), schema, table)
            return grants

        return await _cached_call(ctx, "list_grants", _run, schema, table)

    @server.tool(
        name="list_policies",
        description=(
            "List the Row-Level-Security policies on a table, and whether row security is enabled. "
            "Returns an object with `rls_enabled` (bool) and `policies` — a list of "
            "`{name, command (SELECT/INSERT/UPDATE/DELETE/ALL), permissive (bool), roles, "
            "using_expression, check_expression}`."
        ),
    )
    async def list_policies(ctx: _Ctx, schema: str, table: str) -> introspection.PolicySet:
        async def _run() -> introspection.PolicySet:
            policy_set = await introspection.list_policies(_driver(ctx), schema, table)
            return policy_set

        return await _cached_call(ctx, "list_policies", _run, schema, table)

    @server.tool(
        name="list_sequences",
        description=(
            "List the sequences defined in a schema, with their range, increment, and last value. "
            "Returns a list of objects with `name`, `data_type`, `start_value`, `min_value`, "
            "`max_value`, `increment`, `cycle` (bool), `last_value`."
        ),
    )
    async def list_sequences(ctx: _Ctx, schema: str) -> list[introspection.SequenceInfo]:
        async def _run() -> list[introspection.SequenceInfo]:
            sequences = await introspection.list_sequences(_driver(ctx), schema)
            return sequences

        return await _cached_call(ctx, "list_sequences", _run, schema)

    @server.tool(
        name="list_enums",
        description=(
            "List the enum types in a schema, with their labels in sort order. "
            "Returns a list of objects with `name` and `values` (list of label strings, in "
            "the order defined)."
        ),
    )
    async def list_enums(ctx: _Ctx, schema: str) -> list[introspection.EnumInfo]:
        async def _run() -> list[introspection.EnumInfo]:
            enums = await introspection.list_enums(_driver(ctx), schema)
            return enums

        return await _cached_call(ctx, "list_enums", _run, schema)

    @server.tool(
        name="list_domains",
        description=(
            "List the domain types in a schema, with base type, default, and check constraints. "
            "Returns a list of objects with `name`, `base_type`, `nullable`, `default`, and "
            "`constraints` (list of CHECK clauses)."
        ),
    )
    async def list_domains(ctx: _Ctx, schema: str) -> list[introspection.DomainInfo]:
        async def _run() -> list[introspection.DomainInfo]:
            domains = await introspection.list_domains(_driver(ctx), schema)
            return domains

        return await _cached_call(ctx, "list_domains", _run, schema)

    @server.tool(
        name="list_composite_types",
        description=(
            "List the standalone composite types in a schema with their attributes. "
            "Returns a list of objects with `name` and `attributes` (list of "
            "`{name, data_type}` for each field)."
        ),
    )
    async def list_composite_types(ctx: _Ctx, schema: str) -> list[introspection.CompositeTypeInfo]:
        async def _run() -> list[introspection.CompositeTypeInfo]:
            types = await introspection.list_composite_types(_driver(ctx), schema)
            return types

        return await _cached_call(ctx, "list_composite_types", _run, schema)

    @server.tool(
        name="list_foreign_data_wrappers",
        description=(
            "List the foreign-data wrappers installed in the database. "
            "Returns a list of objects with `name`, `handler` (qualified function name), "
            "`validator`, and `options` (dict of wrapper-specific options)."
        ),
    )
    async def list_foreign_data_wrappers(ctx: _Ctx) -> list[introspection.ForeignDataWrapperInfo]:
        async def _run() -> list[introspection.ForeignDataWrapperInfo]:
            wrappers = await introspection.list_foreign_data_wrappers(_driver(ctx))
            return wrappers

        return await _cached_call(ctx, "list_foreign_data_wrappers", _run)

    @server.tool(
        name="list_foreign_servers",
        description=(
            "List the foreign servers defined in the database, with their FDW and options. "
            "Returns a list of objects with `name`, `wrapper` (foreign-data wrapper name), "
            "`type`, `version`, and `options` (dict of server options)."
        ),
    )
    async def list_foreign_servers(ctx: _Ctx) -> list[introspection.ForeignServerInfo]:
        async def _run() -> list[introspection.ForeignServerInfo]:
            servers = await introspection.list_foreign_servers(_driver(ctx))
            return servers

        return await _cached_call(ctx, "list_foreign_servers", _run)

    @server.tool(
        name="list_foreign_tables",
        description=(
            "List the foreign tables in a schema, with their server and options. "
            "Returns a list of objects with `name`, `server` (foreign-server name), "
            "and `options` (dict of per-table options)."
        ),
    )
    async def list_foreign_tables(ctx: _Ctx, schema: str) -> list[introspection.ForeignTableInfo]:
        async def _run() -> list[introspection.ForeignTableInfo]:
            tables = await introspection.list_foreign_tables(_driver(ctx), schema)
            return tables

        return await _cached_call(ctx, "list_foreign_tables", _run, schema)

    @server.tool(
        name="list_user_mappings",
        description=(
            "List role-to-foreign-server mappings; the catch-all appears as user='public'. "
            "Returns a list of objects with `user` (role name or 'public'), `server` "
            "(foreign-server name), and `options` (dict of mapping options, e.g. credentials)."
        ),
    )
    async def list_user_mappings(ctx: _Ctx) -> list[introspection.UserMappingInfo]:
        async def _run() -> list[introspection.UserMappingInfo]:
            mappings = await introspection.list_user_mappings(_driver(ctx))
            return mappings

        return await _cached_call(ctx, "list_user_mappings", _run)

    @server.tool(
        name="list_publications",
        description=(
            "List logical-replication publications with the tables and operations they include. "
            "Returns a list of objects with `name`, `owner`, `all_tables` (bool), "
            "`publishes_insert` / `publishes_update` / `publishes_delete` / `publishes_truncate` "
            "(bools), and `tables` (list of `schema.table` strings)."
        ),
    )
    async def list_publications(ctx: _Ctx) -> list[introspection.PublicationInfo]:
        async def _run() -> list[introspection.PublicationInfo]:
            publications = await introspection.list_publications(_driver(ctx))
            return publications

        return await _cached_call(ctx, "list_publications", _run)

    @server.tool(
        name="list_subscriptions",
        description="List logical-replication subscriptions; requires superuser to see any rows.",
    )
    async def list_subscriptions(ctx: _Ctx) -> list[introspection.SubscriptionInfo]:
        async def _run() -> list[introspection.SubscriptionInfo]:
            subscriptions = await introspection.list_subscriptions(_driver(ctx))
            return subscriptions

        return await _cached_call(ctx, "list_subscriptions", _run)

    @server.tool(
        name="list_extensions",
        description=(
            "List the extensions installed in the database. Returns a list of objects with `name` and `version`."
        ),
    )
    async def list_extensions(ctx: _Ctx) -> list[introspection.ExtensionInfo]:
        async def _run() -> list[introspection.ExtensionInfo]:
            extensions = await introspection.list_extensions(_driver(ctx))
            return extensions

        return await _cached_call(ctx, "list_extensions", _run)

    @server.tool(
        name="list_available_extensions",
        description=(
            "List every extension available to the database, with whether it is installed. "
            "Returns a list of objects with `name`, `default_version`, `installed_version` "
            "(null when not installed), and `installed` (bool)."
        ),
    )
    async def list_available_extensions(ctx: _Ctx) -> list[introspection.AvailableExtension]:
        async def _run() -> list[introspection.AvailableExtension]:
            extensions = await introspection.list_available_extensions(_driver(ctx))
            return extensions

        return await _cached_call(ctx, "list_available_extensions", _run)

    @server.tool(
        name="list_generated_columns",
        description=(
            "List every GENERATED ALWAYS AS (...) STORED column in a schema, "
            "with its data type, the underlying expression, and whether it's "
            "stored or virtual. PostgreSQL today supports only the stored "
            "form; the kind field is reported anyway so the response shape "
            "is forward-compatible when PG adds virtual columns. "
            "Returns a list of objects with `schema`, `table`, `column`, `data_type`, "
            "`expression`, `kind` ('stored' today; reserved for 'virtual')."
        ),
    )
    async def list_generated_columns(ctx: _Ctx, schema: str) -> list[introspection.GeneratedColumnInfo]:
        async def _run() -> list[introspection.GeneratedColumnInfo]:
            cols = await introspection.list_generated_columns(_driver(ctx), schema)
            return cols

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
    async def list_locks(ctx: _Ctx, limit: int = locks.DEFAULT_LOCK_LIMIT) -> list[locks.LockInfo]:
        return await locks.list_locks(_driver(ctx), limit=limit)

    @server.tool(
        name="find_blocking_chains",
        description=(
            "Return (blocked, blocking) backend pairs via pg_blocking_pids. "
            "Each row pairs a backend waiting on a Lock with one PID "
            "holding the lock that's preventing progress. Cycles are "
            "possible (A blocks B, B blocks A); render with care. Read-only."
        ),
    )
    async def find_blocking_chains(ctx: _Ctx, limit: int = locks.DEFAULT_BLOCKING_LIMIT) -> list[locks.BlockingPair]:
        return await locks.find_blocking_chains(_driver(ctx), limit=limit)

    @server.tool(
        name="walk_blocking_chains",
        description=(
            "Walk and reconstruct the lock-wait graph of the database. Detects deadlock cycles, "
            "traces linear blocking paths to their root blockers, and renders a Mermaid flowchart "
            "representing the lock dependency graph. Read-only. "
            "Returns an object with `cycles` (list of detected cycle PID lists), `paths` (linear "
            "blocking paths as PID lists), `roots` (root blocker PIDs), `nodes` (dict keyed by PID "
            "with per-backend lock detail), and `mermaid` (the pre-rendered flowchart string)."
        ),
    )
    async def walk_blocking_chains(ctx: _Ctx, limit: int = locks.DEFAULT_BLOCKING_LIMIT) -> locks.BlockingGraphReport:
        return await locks.walk_blocking_chains(_driver(ctx), limit=limit)

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
    async def read_pg_stat_io(ctx: _Ctx) -> io_stats.IOStatsReport:
        return await io_stats.read_pg_stat_io(_driver(ctx))

    @server.tool(
        name="read_pg_buffercache_summary",
        description=(
            "Read a high-level summary of the PostgreSQL shared buffer cache usage. "
            "Reports total buffers, free/used buffers, dirty buffers, and average usage count. "
            "Requires the pg_buffercache extension. If not installed, returns available=false."
        ),
    )
    async def read_pg_buffercache_summary(ctx: _Ctx) -> io_stats.BufferCacheSummaryReport:
        return await io_stats.read_pg_buffercache_summary(_driver(ctx))

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
    ) -> io_stats.BufferCacheRelationsReport:
        return await io_stats.read_pg_buffercache_relations(_driver(ctx), schema=schema, limit=limit)

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
    ) -> walinspect.WalRecordsReport:
        return await walinspect.read_pg_wal_records(_driver(ctx), start_lsn, end_lsn, limit)

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
    ) -> walinspect.WalStatsReport:
        return await walinspect.read_pg_wal_stats(_driver(ctx), start_lsn, end_lsn, per_record)

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
            "Allows filtering by schema. "
            "Returns an object with one optional list per detected framework: "
            "`alembic`, `flyway`, `diesel`, `django`, `prisma`, `golang_migrate`, `goose`, "
            "`sequelize`. Each entry is null when the framework's table isn't present; "
            "otherwise it carries the framework-specific row shape (e.g. alembic has "
            "`{version_num}`; flyway has `{installed_rank, version, description, type, ...}`)."
        ),
    )
    async def read_migration_history(ctx: _Ctx, schema: str | None = None) -> migration_history.MigrationHistoryReport:
        return await migration_history.read_migration_history(_driver(ctx), schema=schema)


def _register_diagrams(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="generate_schema_diagram",
        description=_with_example(
            "Render a Mermaid ER diagram for a schema. Views and foreign tables are "
            "excluded; partitions are excluded by default — pass include_partitions=true "
            "to draw each partition as its own entity. "
            "Returns the Mermaid `erDiagram` as a string ready to paste into a Markdown block.",
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
            "rendered as separate nodes prefixed with their schema. "
            "Returns the Mermaid `graph LR` diagram as a string."
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
            "include_samples fetches a few distinct, non-null values for each column. "
            "Returns a single Markdown document as a string.",
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
    ) -> rag_efficiency.VectorEfficiencyReport:
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
        return report


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
            "from mcpg_rag.rerank_events; returns a report with zero "
            "counts when the table doesn't exist or the window is empty."
        ),
    )
    async def analyze_reranker_lift(
        ctx: _Ctx,
        days: int = 7,
        model: str | None = None,
        retrieval_index: str | None = None,
    ) -> rag_efficiency.RerankerLiftReport:
        report = await rag_efficiency.analyze_reranker_lift(
            _driver(ctx), days=days, model=model, retrieval_index=retrieval_index
        )
        return report

    @server.tool(
        name="analyze_topk_stability",
        description=(
            "Jaccard overlap between top-K-by-bi-rank and top-K-by-cross-rank "
            "per query, aggregated. High mean Jaccard means the reranker "
            "isn't actually changing the top-K membership. Surfaces "
            "``topk_stable`` (WARNING) when the rerank is barely earning "
            "its place at this K. Reads from mcpg_rag.rerank_events; returns a "
            "report with zero counts when the table doesn't exist or the "
            "window is empty."
        ),
    )
    async def analyze_topk_stability(
        ctx: _Ctx,
        days: int = 7,
        k: int = 10,
        model: str | None = None,
        retrieval_index: str | None = None,
    ) -> rag_efficiency.TopKStabilityReport:
        report = await rag_efficiency.analyze_topk_stability(
            _driver(ctx), days=days, k=k, model=model, retrieval_index=retrieval_index
        )
        return report

    @server.tool(
        name="analyze_rerank_score_distribution",
        description=(
            "Equal-width histogram of cross_encoder_score values over the "
            "window plus the top-decile share. Surfaces ``score_clustering`` "
            "(WARNING) when the reranker isn't discriminating (more than "
            "half of scores land in the top decile of the range). Reads "
            "from mcpg_rag.rerank_events. "
            "Returns an object with `window_days`, `event_count`, `histogram` "
            "(list of counts), `bucket_edges` (list of bucket boundaries), "
            "`top_decile_share`, and `findings` (list of advisory findings)."
        ),
    )
    async def analyze_rerank_score_distribution(
        ctx: _Ctx,
        days: int = 7,
        model: str | None = None,
        retrieval_index: str | None = None,
        n_buckets: int = 20,
    ) -> rag_efficiency.RerankScoreDistributionReport:
        report = await rag_efficiency.analyze_rerank_score_distribution(
            _driver(ctx),
            days=days,
            model=model,
            retrieval_index=retrieval_index,
            n_buckets=n_buckets,
        )
        return report

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
    ) -> rag_efficiency.NDCGReport:
        report = await rag_efficiency.analyze_rerank_ndcg(
            _driver(ctx), days=days, k=k, model=model, retrieval_index=retrieval_index
        )
        return report

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
    ) -> rag_efficiency.RerankRecommendation:
        report = await rag_efficiency.recommend_rerank_strategy(
            _driver(ctx), days=days, retrieval_index=retrieval_index
        )
        return report


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
    ) -> vector_tuning.TuningRecommendation:
        result = await vector_tuning.tune_vector_index(
            _driver(ctx), schema, table, column, index_type=index_type, metric=metric
        )
        return result

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
    ) -> vector_tuning.RecallReport:
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
        return report

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
    ) -> vector_tuning.HalfvecMigrationPlan:
        plan = await vector_tuning.migrate_vector_to_halfvec(_driver(ctx), schema, table, column)
        return plan

    @server.tool(
        name="analyze_hnsw_recall",
        description=(
            "Sweeps ef_search values to measure the latency and recall trade-off curve "
            "for a given pgvector query vector against exact brute-force ground truth. "
            "Requires the vector extension. "
            "Returns a list of objects with `ef_search`, `recall_at_k`, `mean_latency_ms`, "
            "and `p95_latency_ms` — one row per ef_search value tested."
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
        name="recommend_hnsw_ef_search",
        description=_with_example(
            "Recommend an `hnsw.ef_search` value for a target recall@k — "
            "the actionable companion to `analyze_hnsw_recall`. Samples "
            "`sample_queries` rows (default 10) as query vectors, builds an "
            "exact brute-force top-k ground truth per query, sweeps "
            "`ef_values` (default 16/32/64/128/256) measuring mean recall@k "
            "and p50/p95 latency at each, and recommends the smallest value "
            "clearing `target_recall` (default 0.95). Unlike the "
            "single-query curve tool, this VERIFIES an HNSW index actually "
            "exists on the column (returns `has_hnsw_index=false` with "
            "guidance otherwise — a sweep without one just measures "
            "sequential scans). The query row is excluded from its own "
            "results. Requires the vector extension. Returns an object with "
            "`available`, `has_hnsw_index`, `index_name`, `metric`, `k`, "
            "`target_recall`, `sample_queries`, `recommended_ef_search` "
            "(int or null), `detail`, and `sweep` (list of objects with "
            "`ef_search`, `mean_recall_at_k`, `p50_latency_ms`, "
            "`p95_latency_ms`, `meets_target`).",
            "recommend_hnsw_ef_search(schema='public', table='docs', column='embedding', k=10, target_recall=0.95)",
        ),
    )
    async def recommend_hnsw_ef_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        k: int = 10,
        target_recall: float = 0.95,
        sample_queries: int = 10,
        metric: str = "l2",
    ) -> vector_tuner_advanced.HnswRecallRecommendation:
        result = await vector_tuner_advanced.recommend_hnsw_ef_search(
            _driver(ctx),
            schema,
            table,
            column,
            k=k,
            target_recall=target_recall,
            sample_queries=sample_queries,
            metric=metric,
        )
        return result

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
    ) -> vector_ops.DistanceMetricRecommendation:
        result = await vector_ops.analyze_distance_metric(
            _driver(ctx),
            schema,
            table,
            column,
            sample_size=sample_size,
        )
        return result

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
    ) -> vector_ops.CrossTableSimilarityResult:
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
        return result

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
    ) -> vector_ops.ClusterVectorsResult:
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
        return result

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
    ) -> vector_ops.VectorOutlierResult:
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
        return result

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
    ) -> vector_ops.EmbeddingDriftReport:
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
        return report


def _register_prisma(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="generate_prisma_schema",
        description=(
            "Read a PostgreSQL schema and emit a valid Prisma `.prisma` schema string "
            "(mirrors `prisma db pull`). Covers tables, columns, primary/foreign keys, "
            "unique constraints, indexes, and enums. Views, foreign tables, partitions, "
            "triggers, functions, and policies are out of scope; unmappable types fall "
            'back to `Unsupported("...")`. '
            "Returns the rendered `schema.prisma` source as a single string."
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
            "partitions, triggers, and functions are out of scope. "
            "Returns the rendered TypeScript `schema.ts` source as a single string."
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
            "enum.Enum classes). Composite FKs are a documented v1 gap. "
            "Returns the rendered Python `models.py` source as a single string."
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
            "referenced tables exist. In-process — no MCPG_ALLOW_SHELL needed. "
            "Returns the rendered `schema.sql` text as a single string."
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
            "fresh stats produce false positives. "
            "Returns an object with `tables` (list of candidate tables with their stats) "
            "and `indexes` (list of candidate indexes with size and definition)."
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
            "but isn't itself an email address. "
            "Returns an object with `findings` (list of `{schema, table, column, data_type, "
            "category, confidence, matched_pattern}`) and `summary` counts by category."
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
            "field to filter for renames vs accept-as-is. Pure read. "
            "Returns an object with `schema_style` (detected majority), `findings` "
            "(list of style outliers), and `index_prefix_findings` (indexes with "
            "non-conventional prefixes)."
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
        name="generate_test_row_for",
        description=_with_example(
            "Generate ONE realistic test row for a table — catalogue-aware. "
            "Skips identity / generated columns (server fills them in), "
            "samples one existing row from each referenced table for FK "
            "columns (so the row inserts cleanly), and uses column-name "
            "heuristics (`*_email` → `user_N@example.com`, `*_url` → "
            "`https://example.com/r/N`, `*_at` → recent timestamp, etc.) "
            "to make values look like data. Sibling of `generate_test_data` "
            "(bulk) — designed for the shadow-migration workflow where a "
            "single realistic row matters more than volume. Returns an "
            "object with `insert_sql` (one ready-to-execute INSERT), "
            "`columns` (per-column ColumnFill with `sql_literal` + "
            "`heuristic` explanation), `schema`, `table`. Does NOT execute "
            "the INSERT — caller applies via `run_write` when ready.",
            "generate_test_row_for(schema='public', table='orders', seed=42)",
        ),
    )
    async def generate_test_row_for(
        ctx: _Ctx,
        schema: str,
        table: str,
        seed: int | None = None,
        follow_foreign_keys: bool = True,
    ) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            row = await test_row_factory.generate_test_row_for(
                _driver(ctx),
                schema,
                table,
                seed=seed,
                follow_foreign_keys=follow_foreign_keys,
            )
            return asdict(row)

        return await _cached_call(ctx, "generate_test_row_for", _run, schema, table, seed, follow_foreign_keys)

    @server.tool(
        name="analyze_session_cost",
        description=_with_example(
            "Surface hot-path inefficiencies from the audit log. Reads "
            "`mcpg_audit.events` over the last `lookback_minutes` "
            "(default 60, capped at 1440) and flags tools called more "
            "than `hot_threshold` times (default 10). Catalogue-listing "
            "tools (`list_tables` / `list_schemas` / `list_indexes` / "
            "etc.) get a `redundant_listing` finding pointing at "
            "`get_compact_schema`; other tools get a `hot_repeated_call` "
            "finding suggesting caching. Idle sessions get an "
            "`idle_session` finding. When `mcpg_audit.events` doesn't "
            "exist (audit subsystem off) returns `audit_table_present=False` "
            "with a diagnostic. Returns an object with `audit_table_present` "
            "(bool), `events_examined` (int), `lookback_minutes`, "
            "`findings` (list of objects with `reason`, `tool`, "
            "`call_count`, `suggestion`), and `detail`.",
            "analyze_session_cost(lookback_minutes=30, hot_threshold=15)",
        ),
    )
    async def analyze_session_cost(ctx: _Ctx, lookback_minutes: int = 60, hot_threshold: int = 10) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            result = await session_advisor.analyze_session_cost(
                _driver(ctx), lookback_minutes=lookback_minutes, hot_threshold=hot_threshold
            )
            return asdict(result)

        return await _cached_call(ctx, "analyze_session_cost", _run, lookback_minutes, hot_threshold)

    @server.tool(
        name="recommend_headline_tools",
        description=_with_example(
            "Empirically curate `describe_self`'s per-bucket `headline_tools` "
            "from the audit log. Reads `mcpg_audit.events` over the last "
            "`lookback_days` (default 7, capped at 90), groups successful "
            "calls by capability bucket, and reports the top-`top_n` (default "
            "6) tools per bucket with `newcomers` (recommended but not in the "
            "hand-curated current list) and `departures` (currently headlined "
            "but not in the recommendation). The output is a REVIEWABLE "
            "recommendation, not an auto-applied override — operators decide "
            "whether to update `mcpg.about.CAPABILITIES`. Returns "
            "`audit_table_present=False` with a diagnostic when the audit "
            "subsystem is off. Returns an object with `audit_table_present`, "
            "`lookback_days`, `top_n`, `events_examined`, `detail`, and "
            "`buckets` (list of objects with `bucket_id`, `current`, "
            "`recommended`, `newcomers`, `departures`, `call_counts`).",
            "recommend_headline_tools(lookback_days=14, top_n=6)",
        ),
    )
    async def recommend_headline_tools(ctx: _Ctx, lookback_days: int = 7, top_n: int = 6) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            from mcpg.about import CAPABILITIES
            from mcpg.headline_curator import recommend_headline_tools as _recommend

            current = {cap.id: cap.headline_tools for cap in CAPABILITIES}
            report = await _recommend(
                _driver(ctx),
                lookback_days=lookback_days,
                top_n=top_n,
                current_headlines=current,
            )
            return asdict(report)

        return await _cached_call(ctx, "recommend_headline_tools", _run, lookback_days, top_n)

    @server.tool(
        name="audit_sequences",
        description=_with_example(
            "Flag sequences nearing their ceiling — serial / identity / "
            "explicit sequences whose `last_value / max_value` exceeds "
            "`warning_pct` (default 80) or `critical_pct` (default 95). "
            "Sequence overflow is catastrophic and silent until the next "
            "`nextval()` raises 'reached maximum value' — the int4 `serial` "
            "ceiling (2^31-1) is hit far more often than expected. Pure "
            "read; `available=false` on PG < 10 (no pg_sequences). Returns "
            "an object with `available`, `total_examined`, `warning_pct`, "
            "`critical_pct`, `detail`, and `sequences` (at-risk only, "
            "sorted by `used_pct` desc — each with `schema`, `sequence`, "
            "`last_value`, `max_value`, `used_pct`, `remaining`, `status`).",
            "audit_sequences(warning_pct=80, critical_pct=95)",
        ),
    )
    async def audit_sequences(ctx: _Ctx, warning_pct: float = 80.0, critical_pct: float = 95.0) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            result = await config_advisor.audit_sequences(
                _driver(ctx), warning_pct=warning_pct, critical_pct=critical_pct
            )
            return asdict(result)

        return await _cached_call(ctx, "audit_sequences", _run, warning_pct, critical_pct)

    @server.tool(
        name="audit_settings",
        description=_with_example(
            "Sanity-sweep `postgresql.conf` via `pg_settings`. Flags "
            "dangerous toggles (`fsync=off`, `full_page_writes=off`, "
            "`autovacuum=off`, `synchronous_commit=off`), cross-setting "
            "issues (`maintenance_work_mem` < `work_mem`, tiny "
            "`shared_buffers`, low `checkpoint_completion_target`), and — "
            "when `total_ram_mb` is supplied — RAM-relative ratios for "
            "`shared_buffers` / `effective_cache_size` (PostgreSQL can't "
            "see host RAM itself). Pure read. Returns an object with "
            "`ram_aware` (bool), `examined_settings` (list), `detail`, and "
            "`findings` (tripped rules only — each with `code`, `setting`, "
            "`current`, `status`, `suggestion`).",
            "audit_settings(total_ram_mb=16384)",
        ),
    )
    async def audit_settings(ctx: _Ctx, total_ram_mb: int | None = None) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            result = await config_advisor.audit_settings(_driver(ctx), total_ram_mb=total_ram_mb)
            return asdict(result)

        return await _cached_call(ctx, "audit_settings", _run, total_ram_mb)

    @server.tool(
        name="recommend_postgres_conf",
        description=_with_example(
            "Compute pgtune-style `postgresql.conf` recommendations. Pure "
            "calculator — touches no database. Given `total_ram_mb` "
            "(required), `cpu_count` (default 4), `workload` (one of "
            "`web`/`oltp`/`dw`/`desktop`/`mixed`, default `mixed`), "
            "`storage` (one of `ssd`/`hdd`/`san`, default `ssd`), and an "
            "optional `max_connections` override, returns recommended "
            "values for `shared_buffers`, `effective_cache_size`, "
            "`work_mem`, `maintenance_work_mem`, `wal_buffers`, "
            "`min_wal_size`/`max_wal_size`, `checkpoint_completion_target`, "
            "`default_statistics_target`, `random_page_cost`, "
            "`effective_io_concurrency`, and the parallel-worker knobs. "
            "Memory fields are postgres-ready strings; `settings` is the "
            "same data as a flat {guc: value} dict for direct rendering. "
            "Pair with `audit_settings` (audit first, then size).",
            "recommend_postgres_conf(total_ram_mb=16384, cpu_count=8, workload='oltp', storage='ssd')",
        ),
    )
    async def recommend_postgres_conf(
        ctx: _Ctx,
        total_ram_mb: int,
        cpu_count: int = 4,
        workload: str = "mixed",
        storage: str = "ssd",
        max_connections: int | None = None,
    ) -> dict[str, Any]:
        del ctx  # pure calculator — no DB, no cache key needed
        result = config_advisor.recommend_postgres_conf(
            total_ram_mb=total_ram_mb,
            cpu_count=cpu_count,
            workload=workload,
            storage=storage,
            max_connections=max_connections,
        )
        return asdict(result)

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
            "Schema and table names must be plain identifiers. "
            "Returns an object with `format`, `row_count`, `truncated` (bool — true when "
            "the row count hit `limit`), and `content` (the serialised payload as a string)."
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
            "not installed. "
            "Returns an object with `available` (bool) and `hypertables` "
            "(list of `{schema, table, num_chunks, compression_enabled, total_size_bytes}`)."
        ),
    )
    async def list_hypertables(ctx: _Ctx) -> timescaledb.HypertableListResult:
        result = await timescaledb.list_hypertables(_driver(ctx))
        return result

    @server.tool(
        name="list_chunks",
        description=(
            "List the chunks of a TimescaleDB hypertable with each chunk's "
            "range_start / range_end and whether it has been compressed. "
            "Empty list when the table is not a hypertable. "
            "Returns an object with `available` (bool) and `chunks` "
            "(list of `{chunk_name, range_start, range_end, is_compressed, total_size_bytes}`)."
        ),
    )
    async def list_chunks(ctx: _Ctx, schema: str, table: str) -> timescaledb.ChunkListResult:
        result = await timescaledb.list_chunks(_driver(ctx), schema, table)
        return result


def _register_timescaledb_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="create_hypertable",
        description=(
            "Convert an existing table into a TimescaleDB hypertable on "
            "`time_column`. Validates schema / table / column names against "
            "the plain-identifier allowlist and the chunk interval against "
            "a TimescaleDB-style pattern (e.g. '7 days', '1 hour'). "
            "Requires unrestricted mode + MCPG_ALLOW_DDL. "
            "Returns an object with `available` (bool), `function` ('create_hypertable'), "
            "and `details` (the create_hypertable return text or an extension-missing note)."
        ),
    )
    async def create_hypertable(
        ctx: _Ctx,
        schema: str,
        table: str,
        time_column: str,
        chunk_time_interval: str = "7 days",
        if_not_exists: bool = True,
    ) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.create_hypertable(
            _driver(ctx),
            schema,
            table,
            time_column,
            chunk_time_interval=chunk_time_interval,
            if_not_exists=if_not_exists,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="add_compression_policy",
        description=(
            "Enable TimescaleDB column-store compression on a hypertable and "
            "schedule a policy that compresses chunks older than "
            "`compress_after` (e.g. '7 days'). Requires unrestricted mode "
            "+ MCPG_ALLOW_DDL. "
            "Returns an object with `available` (bool), `function` "
            "('add_compression_policy'), and `details` (the scheduled job id or "
            "an extension-missing note)."
        ),
    )
    async def add_compression_policy(
        ctx: _Ctx, schema: str, table: str, compress_after: str = "7 days"
    ) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.add_compression_policy(_driver(ctx), schema, table, compress_after=compress_after)
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="add_retention_policy",
        description=(
            "Schedule a TimescaleDB retention policy that drops hypertable "
            "chunks older than `drop_after` (e.g. '30 days'). Requires "
            "unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def add_retention_policy(
        ctx: _Ctx, schema: str, table: str, drop_after: str = "30 days"
    ) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.add_retention_policy(_driver(ctx), schema, table, drop_after=drop_after)
        await ctx.request_context.lifespan_context.cache.clear()
        return result


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
            "and lost on restart. "
            "Returns a list of objects with `subscription_id` and `channel`."
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
            "unrestricted mode + MCPG_ALLOW_SHELL. "
            "Returns an object with `succeeded` (bool), `stdout`, `stderr`, "
            "`exit_code`, `timed_out` (bool), and `output_truncated` (bool)."
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
        description=(
            "Verify the HMAC-SHA256 signature chain of persisted audit events in mcpg_audit.events. "
            "Returns an object with `verified` (bool), `events_checked` (int), the `first_event_id` "
            "and `last_event_id` covered by the walk, and (on failure) `error` and `first_invalid_id` "
            "pointing at where the chain broke."
        ),
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
            "Return the PostgreSQL execution plan for a query. By default "
            "uses `EXPLAIN (FORMAT JSON)` — plan only, the query is not "
            "executed. Set `io=true` to switch to `EXPLAIN (ANALYZE, "
            "BUFFERS, TIMING)` — runs the query and includes buffer + "
            "I/O timing per node (PG 19 additionally surfaces "
            "asynchronous-I/O block counts). Validated by the same "
            "safety allowlist as `run_select`, so writes / DDL are "
            "rejected.",
            "explain_query(sql='SELECT * FROM orders WHERE customer_id = 42', io=true)",
        ),
    )
    async def explain_query(ctx: _Ctx, sql: str, io: bool = False) -> dict[str, Any]:
        result = await query.explain_query(_driver(ctx), sql, io=io)
        return asdict(result)

    @server.tool(
        name="analyze_query_plan",
        description=_with_example(
            "Summarise a query's execution plan: total estimated cost, "
            "estimated rows, node types used, and any sequentially-scanned tables. "
            "Set `io=true` to run `EXPLAIN (ANALYZE, BUFFERS, TIMING)` instead "
            "of the plan-only variant — adds `actual_total_time_ms`, "
            "`shared_blocks_read/hit`, `io_read_time_ms`, `io_write_time_ms`, "
            "and (PG 19) `aio_read_blocks` / `aio_write_blocks` rolled up across "
            "the plan tree. Reasoning about AIO needs `io=true`.",
            "analyze_query_plan(sql='SELECT * FROM orders WHERE customer_id = 42', io=true)",
        ),
    )
    async def analyze_query_plan(ctx: _Ctx, sql: str, io: bool = False) -> dict[str, Any]:
        result = await query.analyze_query_plan(_driver(ctx), sql, io=io)
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
        # All NL→SQL business logic — provider selection from request
        # arg / env default / configured keys, model + base_url override
        # rules, error shaping for misconfig — lives in mcpg.nl2sql so
        # this wrapper can stay a thin driver + asdict pass-through.
        settings = ctx.request_context.lifespan_context.settings
        params = nl2sql.resolve_provider_call_params(settings, provider)
        llm = nl2sql.build_provider(params.provider_name, params.api_key, base_url=params.base_url)
        result = await nl2sql.translate_nl_to_sql(
            _driver(ctx),
            provider=llm,
            model=params.model,
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
            "hit ratio, tables needing vacuum, and invalid indexes. "
            "Returns an object with `status` ('ok' / 'warning' / 'critical') and "
            "`checks` (a list of `{name, status, detail}` per check).",
            "check_database_health()",
        ),
    )
    async def check_database_health(ctx: _Ctx) -> health.HealthReport:
        return await health.check_database_health(_driver(ctx))

    @server.tool(
        name="audit_database",
        description=(
            "Run a deep, comprehensive DBA-level database performance, logs, "
            "and health audit over the specified schema. Scans memory, checkpoints, "
            "temp file spills, contention locks, dead tuple cleanliness, and "
            "optionally scans custom logging tables. "
            "Returns an object with `timestamp`, `database`, `version`, `overall_health` "
            "('GOOD' / 'WARNING' / 'CRITICAL'), `health_score` (int), `categories` "
            "(per-area results), `top_issues`, `recommendations`, and `raw_stats_snapshot`."
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
        name="read_autovacuum_priority",
        description=_with_example(
            "Return the tables most urgently needing autovacuum, ranked by how "
            "close their dead-tuple count is to the per-table autovacuum "
            "threshold (`autovacuum_vacuum_threshold + "
            "autovacuum_vacuum_scale_factor * reltuples`, honouring per-table "
            "`reloptions` overrides). Each row carries `priority` ('overdue' "
            "if past the threshold, 'watchlist' if within 50%, 'borderline' "
            "otherwise) plus the inputs (`n_dead_tup`, `vacuum_threshold`, "
            "`last_autovacuum`, `autovacuum_enabled`) so the agent can "
            "explain *why* a table landed on the shortlist. Top-level "
            "`overdue_count` lets an agent branch without walking the list. "
            "Read-only catalog query.",
            "read_autovacuum_priority(limit=25)",
        ),
    )
    async def read_autovacuum_priority(ctx: _Ctx, limit: int = 25) -> dict[str, Any]:
        from mcpg import autovacuum

        async def _run() -> dict[str, Any]:
            report = await autovacuum.read_autovacuum_priority(_driver(ctx), limit=limit)
            return asdict(report)

        return await _cached_call(ctx, "read_autovacuum_priority", _run, limit)

    @server.tool(
        name="recommend_indexes",
        description=_with_example(
            "Recommend tables that may benefit from indexing — large tables read mostly by sequential scan. "
            "Returns a list of objects with `schema`, `table`, `live_tuples`, `sequential_scans`, "
            "`index_scans`, and a `reason` explaining why the table is a candidate.",
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
            "available=false if pg_trgm is not installed. "
            "Returns an object with `available` (bool), `matches` (list of "
            "`{value, similarity}` ranked by similarity descending), and `mode`.",
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
    ) -> textsearch.FuzzySearchResult:
        result = await textsearch.fuzzy_search(
            _driver(ctx), schema, table, column, term, mode=mode, limit=limit, threshold=threshold
        )
        return result

    @server.tool(
        name="full_text_search",
        description=_with_example(
            "Rank a text column's documents against a full-text query using "
            "PostgreSQL's built-in tsvector/tsquery. The query accepts "
            "web-search syntax (quoted phrases, or, - exclusion). "
            "Returns a list of objects with the matched row's primary key columns "
            "plus `rank` (ts_rank score, higher = better match).",
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
    ) -> list[textsearch.FullTextMatch]:
        matches = await textsearch.full_text_search(
            _driver(ctx), schema, table, column, search_query, config=config, limit=limit
        )
        return matches

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
    ) -> textsearch.VectorSearchResult:
        result = await textsearch.vector_search(
            _driver(ctx), schema, table, column, query_vector, metric=metric, limit=limit
        )
        return result

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
    ) -> textsearch.VectorSearchResult:
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
        return result

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
    ) -> textsearch.MmrSearchResult:
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
        return result

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
    ) -> textsearch.HybridSearchResult:
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
        return result

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
    async def recommend_vector_quantization(ctx: _Ctx, schema: str) -> list[textsearch.QuantizationRecommendation]:
        recommendations = await textsearch.recommend_vector_quantization(_driver(ctx), schema)
        return recommendations

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
    ) -> textsearch.GeoSearchResult:
        result = await textsearch.geo_search(_driver(ctx), schema, table, column, longitude, latitude, limit=limit)
        return result


def _register_liveops(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_active_queries",
        description=(
            "List the queries currently running on the server, with each "
            "backend's wait event, duration, and the PIDs blocking it. "
            "Returns a list of objects with `pid`, `username`, `application`, `state`, "
            "`wait_event` (null when not waiting), `duration_seconds`, `query`, and "
            "`blocked_by` (list of PIDs holding locks this backend is waiting on)."
        ),
    )
    async def list_active_queries(ctx: _Ctx) -> list[liveops.ActiveQuery]:
        return await liveops.list_active_queries(_driver(ctx))

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
    async def verify_connection_encryption(ctx: _Ctx) -> liveops.ConnectionEncryption:
        return await liveops.verify_connection_encryption(_driver(ctx))

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
            "build on a big table is taking longer than expected. "
            "Returns a list of objects with `pid`, `schema`, `relation`, `index_name`, "
            "`command`, `phase`, `blocks_done`, `blocks_total`, `tuples_done`, "
            "`tuples_total`, and `progress_pct` (null when no denominator is reported)."
        ),
    )
    async def monitor_index_build(ctx: _Ctx) -> list[liveops.IndexBuildProgress]:
        return await liveops.monitor_index_build(_driver(ctx))

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
    async def list_cron_jobs(ctx: _Ctx) -> list[cron.CronJob]:
        return await cron.list_cron_jobs(_driver(ctx))


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
    async def list_turboquant_indexes(ctx: _Ctx) -> list[turboquant.TurboQuantIndexInfo]:
        infos = await turboquant.list_turboquant_indexes(_driver(ctx))
        return infos

    @server.tool(
        name="get_turboquant_index_metadata",
        description=(
            "Fetch the tq_index_metadata payload for a single turboquant "
            "index (schema.index). Documented fields are surfaced as typed "
            "attributes; the full upstream payload is preserved in "
            "``raw_metadata`` so advisors can reach unanticipated fields. "
            "Raises when the extension is not installed or no turboquant "
            "index by that name exists. "
            "Returns an object with `schema`, `index`, `algorithm_version`, "
            "`quantizer_family`, `residual_sketch_kind`, `fast_path_eligible`, "
            "`capability_flags`, `delta_state`, `maintenance_recommended`, and "
            "`raw_metadata` (full upstream payload)."
        ),
    )
    async def get_turboquant_index_metadata(ctx: _Ctx, schema: str, index: str) -> turboquant.TurboQuantIndexInfo:
        info = await turboquant.get_turboquant_index_metadata(_driver(ctx), schema, index)
        return info

    @server.tool(
        name="get_turboquant_heap_stats",
        description=(
            "Return the exact heap row count tq_index_heap_stats() reports "
            "for a single turboquant index (schema.index). The raw upstream "
            "payload is preserved in ``raw`` for any extra counters upstream "
            "may add. Requires the pg_turboquant extension."
        ),
    )
    async def get_turboquant_heap_stats(ctx: _Ctx, schema: str, index: str) -> turboquant.TurboQuantHeapStats:
        stats = await turboquant.get_turboquant_heap_stats(_driver(ctx), schema, index)
        return stats

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
    async def get_turboquant_last_scan_stats(ctx: _Ctx) -> turboquant.TurboQuantLastScanStats | None:
        stats = await turboquant.get_turboquant_last_scan_stats(_driver(ctx))
        return stats if stats is not None else None

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
    async def recommend_turboquant_maintenance(ctx: _Ctx) -> list[turboquant.TurboQuantAdvisorFinding]:
        findings = await turboquant.recommend_turboquant_maintenance(_driver(ctx))
        return findings

    @server.tool(
        name="turboquant_approx_candidates",
        description=(
            "Run tq_approx_candidates against a turboquant index — approximate "
            "k-NN retrieval, no exact rerank. ``metric`` is 'cosine' | "
            "'inner_product' | 'l2' (mapped to upstream's runtime metric "
            "text). ``half_precision=True`` switches to the halfvec overload. "
            "``probes`` / ``oversample_factor`` are optional per-query knobs "
            "(consider calling ``recommend_turboquant_query_knobs`` first). "
            "Requires the pg_turboquant extension. "
            "Returns a list of candidate objects with `candidate_id`, "
            "`approximate_distance`, and `approximate_rank`."
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
    ) -> list[turboquant.TurboQuantCandidate]:
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
        return candidates

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
    ) -> list[turboquant.TurboQuantRerankedCandidate]:
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
        return candidates

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
    ) -> turboquant.TurboQuantQueryKnobs:
        knobs = await turboquant.recommend_turboquant_query_knobs(
            _driver(ctx),
            candidate_limit,
            final_limit=final_limit,
            index_schema=index_schema,
            index_name=index_name,
            filter_selectivity=filter_selectivity,
        )
        return knobs


def _register_pg_search_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_pg_search_indexes",
        description=(
            "List every pg_search BM25 index in the database along with "
            "the parsed reloptions (the ``WITH (...)`` config) for each. "
            "Surfaces the 13 documented bm25 options (key_field, the six "
            "*_fields jsonb configs, layer_sizes, background_layer_sizes, "
            "target_segment_count, mutable_segment_rows, sort_by, "
            "search_tokenizer). The full parsed dict is preserved in "
            "``index_options`` so unsurfaced or future options stay "
            "reachable. Returns an empty list when the pg_search extension "
            "is not installed."
        ),
    )
    async def list_pg_search_indexes(ctx: _Ctx) -> list[pg_search.PgSearchIndexInfo]:
        infos = await pg_search.list_pg_search_indexes(_driver(ctx))
        return infos

    @server.tool(
        name="get_pg_search_index_metadata",
        description=(
            "Fetch the parsed reloptions for a single BM25 index "
            "(schema.index). Same shape as one entry of "
            "``list_pg_search_indexes`` — typed accessors for the 13 "
            "documented options plus the raw ``index_options`` dict. "
            "Raises when the extension is not installed or no BM25 "
            "index by that name exists. "
            "Returns an object with `schema`, `index`, the typed option fields, "
            "and `index_options` (raw reloptions dict for forward-compat)."
        ),
    )
    async def get_pg_search_index_metadata(ctx: _Ctx, schema: str, index: str) -> pg_search.PgSearchIndexInfo:
        info = await pg_search.get_pg_search_index_metadata(_driver(ctx), schema, index)
        return info

    @server.tool(
        name="recommend_pg_search_maintenance",
        description=(
            "Walk every pg_search BM25 index and emit advisor findings. "
            "Rules currently surfaced: ``missing_key_field`` (CRITICAL — "
            "the key_field reloption is required by upstream; an index "
            "without it can't satisfy queries) and ``no_field_configs`` "
            "(WARNING — none of the six *_fields reloptions are set, so "
            "the index falls back to default tokenization for every "
            "indexed column). Each finding carries a ready-to-run "
            "``suggested_action`` SQL statement. Returns an empty list "
            "when the extension is not installed. Also feeds the "
            "``pg_search BM25 Indexes`` category in audit_database."
        ),
    )
    async def recommend_pg_search_maintenance(ctx: _Ctx) -> list[pg_search.PgSearchAdvisorFinding]:
        findings = await pg_search.recommend_pg_search_maintenance(_driver(ctx))
        return findings

    @server.tool(
        name="pg_search_run",
        description=(
            "Run a BM25 keyword search against a pg_search-indexed table. "
            "Returns hits as {id, score, snippets} where ``id`` is the value "
            "of the caller-supplied ``key_field`` and ``score`` is "
            "``pdb.score(t)``. ``columns=None`` searches the whole index; "
            '``columns=["col"]`` restricts to a single text field. '
            "Multi-column search needs the pdb.parse per-field config JSON "
            "and is deferred to a follow-up phase. ``return_snippets=True`` "
            "requires ``snippet_field`` and projects ``pdb.snippets`` over "
            "that column. Requires the pg_search extension. SECURITY: "
            "``snippet_start_tag`` / ``snippet_end_tag`` are not sanitized "
            "(they pass through to upstream as-is). The defaults match "
            "pg_search's HTML defaults; if callers forward untrusted values "
            "and a downstream consumer renders snippets as HTML, that's an "
            "XSS vector — output escaping is the renderer's responsibility."
        ),
    )
    async def pg_search_run(
        ctx: _Ctx,
        schema: str,
        table: str,
        query: str,
        key_field: str,
        limit: int,
        columns: list[str] | None = None,
        return_snippets: bool = False,
        snippet_field: str | None = None,
        snippet_start_tag: str = "<b>",
        snippet_end_tag: str = "</b>",
        snippet_max_num_chars: int = 150,
    ) -> list[pg_search.PgSearchHit]:
        hits = await pg_search.pg_search_run(
            _driver(ctx),
            schema,
            table,
            query,
            key_field,
            columns=columns,
            limit=limit,
            return_snippets=return_snippets,
            snippet_field=snippet_field,
            snippet_start_tag=snippet_start_tag,
            snippet_end_tag=snippet_end_tag,
            snippet_max_num_chars=snippet_max_num_chars,
        )
        return hits

    @server.tool(
        name="pg_search_more_like_this",
        description=(
            "Find rows similar to a seed document via ``pdb.more_like_this`` "
            "+ ``@@@``. ``document_id`` is the value of ``key_field`` for the "
            "seed row. All nine documented pdb.more_like_this tuning args "
            "(``fields`` jsonb, ``min_doc_frequency``, ``max_doc_frequency``, "
            "``min_term_frequency``, ``max_query_terms``, ``min_word_length``, "
            "``max_word_length``, ``boost_factor``, ``stop_words``) are "
            "optional kwargs — when omitted the wrapper does not mention them "
            "in the SQL so upstream's defaults apply. Returns the same "
            "{id, score} hit shape as ``pg_search_run``. Requires the "
            "pg_search extension."
        ),
    )
    async def pg_search_more_like_this(
        ctx: _Ctx,
        schema: str,
        table: str,
        document_id: Any,
        key_field: str,
        limit: int,
        fields: dict[str, Any] | None = None,
        min_doc_frequency: int | None = None,
        max_doc_frequency: int | None = None,
        min_term_frequency: int | None = None,
        max_query_terms: int | None = None,
        min_word_length: int | None = None,
        max_word_length: int | None = None,
        boost_factor: float | None = None,
        stop_words: list[str] | None = None,
    ) -> list[pg_search.PgSearchHit]:
        hits = await pg_search.pg_search_more_like_this(
            _driver(ctx),
            schema,
            table,
            document_id,
            key_field,
            limit=limit,
            fields=fields,
            min_doc_frequency=min_doc_frequency,
            max_doc_frequency=max_doc_frequency,
            min_term_frequency=min_term_frequency,
            max_query_terms=max_query_terms,
            min_word_length=min_word_length,
            max_word_length=max_word_length,
            boost_factor=boost_factor,
            stop_words=stop_words,
        )
        return hits

    @server.tool(
        name="pg_search_parse_query",
        description=(
            "Parse a query string through ``pdb.parse`` and return its "
            "canonical text form for debugging — useful for confirming the "
            "parser interpreted a phrase as expected. ``lenient=True`` "
            "relaxes syntax checking; ``conjunction_mode=True`` treats "
            "space-separated terms as AND-joined rather than OR-joined. "
            "Requires the pg_search extension."
        ),
    )
    async def pg_search_parse_query(
        ctx: _Ctx,
        query_string: str,
        lenient: bool = False,
        conjunction_mode: bool = False,
    ) -> pg_search.PgSearchParsedQuery:
        parsed = await pg_search.pg_search_parse_query(
            _driver(ctx),
            query_string,
            lenient=lenient,
            conjunction_mode=conjunction_mode,
        )
        return parsed

    @server.tool(
        name="hybrid_bm25_vector_search",
        description=(
            "Combine a BM25 search and a pgvector search via Reciprocal "
            "Rank Fusion — the canonical v2 pattern ParadeDB documents in "
            "the 2025-10-22 'Hybrid Search Missing Manual' blog post. "
            "Returns hits as {id, score, bm25_rank, vector_rank}. ``score`` "
            "is the summed ``sum(weight * 1.0 / (k + rank))`` across both "
            "legs; per-leg ranks are surfaced for transparency (either can "
            "be NULL if a row only appeared in one leg's top-K). "
            "``distance_op`` is the pgvector operator ('<=>'/'<->'/'<#>' "
            "— RRF is operator-agnostic). ``bm25_columns=None`` searches "
            'the whole BM25 index; ``bm25_columns=["col"]`` restricts '
            "the BM25 leg to a single field. Defaults mirror upstream's "
            "demonstrated form (cosine, k=60, equal weights, "
            "per_leg_limit=20). Requires the pg_search and pgvector "
            "extensions."
        ),
    )
    async def hybrid_bm25_vector_search(
        ctx: _Ctx,
        schema: str,
        table: str,
        query_text: str,
        query_vector: list[float] | str,
        key_field: str,
        vector_column: str,
        final_limit: int,
        bm25_columns: list[str] | None = None,
        distance_op: str = pg_search.HYBRID_DEFAULT_DISTANCE_OP,
        k: int = pg_search.RRF_DEFAULT_K,
        bm25_weight: float = pg_search.HYBRID_DEFAULT_WEIGHT,
        vector_weight: float = pg_search.HYBRID_DEFAULT_WEIGHT,
        per_leg_limit: int = pg_search.HYBRID_DEFAULT_PER_LEG_LIMIT,
    ) -> list[pg_search.HybridHit]:
        hits = await pg_search.hybrid_bm25_vector_search(
            _driver(ctx),
            schema,
            table,
            query_text=query_text,
            query_vector=query_vector,
            key_field=key_field,
            vector_column=vector_column,
            bm25_columns=bm25_columns,
            distance_op=distance_op,
            k=k,
            bm25_weight=bm25_weight,
            vector_weight=vector_weight,
            per_leg_limit=per_leg_limit,
            final_limit=final_limit,
        )
        return hits


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
    async def maintain_turboquant_index(ctx: _Ctx, schema: str, index: str) -> turboquant.MaintenanceResult:
        result = await turboquant.maintain_turboquant_index(_driver(ctx), schema, index)
        await ctx.request_context.lifespan_context.cache.clear()
        return result


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
            "``setup_rag_telemetry``. "
            "Returns an object with `event_id` (the new mcpg_rag.rerank_events row id)."
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
    ) -> rag_telemetry.LogRerankEventResult:
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
        return result

    @server.tool(
        name="record_efficiency_observation",
        description=(
            "Insert one row into mcpg_rag.efficiency_observations - one "
            "observation per analyze_vector_search_efficiency run. Fields "
            "mirror VectorEfficiencyReport one-to-one; pass the report's "
            "schema_name / table_name / column_name / index_name / "
            "backend / metric / k / sample_size / recall_baseline / "
            "rerank_lift_curve (as list[dict]) / spearman / kendall / "
            "pages_pruned_ratio_p50 / duration_seconds + optional extra "
            "dict. Tool arguments recall_baseline / spearman / kendall "
            "correspond to VectorEfficiencyReport.recall_at_k_baseline / "
            "score_rank_correlation_spearman / score_rank_correlation_kendall "
            "respectively. Available only in unrestricted mode; the table "
            "must be created first via setup_efficiency_observations. "
            "Returns an object with `observation_id` "
            "(the new mcpg_rag.efficiency_observations row id)."
        ),
    )
    async def record_efficiency_observation(
        ctx: _Ctx,
        schema_name: str,
        table_name: str,
        column_name: str,
        index_name: str,
        backend: str,
        metric: str,
        k: int,
        sample_size: int,
        recall_baseline: float | None,
        rerank_lift_curve: list[dict[str, Any]] | None,
        spearman: float | None,
        kendall: float | None,
        pages_pruned_ratio_p50: float | None,
        duration_seconds: float | None,
        extra: dict[str, Any] | None = None,
    ) -> rag_telemetry.RecordEfficiencyObservationResult:
        result = await rag_telemetry.record_efficiency_observation(
            _driver(ctx),
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            index_name=index_name,
            backend=backend,
            metric=metric,
            k=k,
            sample_size=sample_size,
            recall_baseline=recall_baseline,
            rerank_lift_curve=rerank_lift_curve,
            spearman=spearman,
            kendall=kendall,
            pages_pruned_ratio_p50=pages_pruned_ratio_p50,
            duration_seconds=duration_seconds,
            extra=extra,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result


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
    async def setup_rag_telemetry(ctx: _Ctx) -> rag_telemetry.RagTelemetrySetupResult:
        database = ctx.request_context.lifespan_context.database
        result = await rag_telemetry.setup_rag_telemetry(database)
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="setup_efficiency_observations",
        description=(
            "Create the ``mcpg_rag.efficiency_observations`` table + two "
            "indexes (``observed_at``, composite ``(backend, metric, k, "
            "observed_at)``). Idempotent — safe to re-run; returns "
            "``{schema_created, table_created, indexes_created}``. "
            "Required before any ``record_efficiency_observation`` call "
            "or before ``recommend_efficiency_thresholds`` can return "
            "corpus-derived values. Performs DDL — requires unrestricted "
            "mode + MCPG_ALLOW_DDL."
        ),
    )
    async def setup_efficiency_observations(ctx: _Ctx) -> rag_telemetry.EfficiencyObservationsSetupResult:
        database = ctx.request_context.lifespan_context.database
        result = await rag_telemetry.setup_efficiency_observations(database)
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_rag_telemetry_efficiency_read(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="recommend_efficiency_thresholds",
        description=(
            "Compute corpus-percentile thresholds from accumulated "
            "``mcpg_rag.efficiency_observations`` history. Phase E currently "
            "adapts three thresholds: ``baseline_recall_low`` (p10 of "
            "recall_baseline), ``ranking_degraded_spearman`` (p10 of "
            "spearman), and ``pruning_ineffective`` (p10 of "
            "pages_pruned_ratio_p50). The remaining four thresholds stay "
            "at their hardcoded defaults. Filters by ``days`` window + "
            "optional ``backend`` / ``metric`` / ``k`` so callers can ask "
            "'what's normal for HNSW+cosine+k=10 in this deployment' vs "
            "'what's normal globally'. Falls back to defaults (with "
            "``derived_from_corpus=false``) when the corpus is smaller "
            "than the minimum required. "
            "Returns an object with `corpus_size`, `derived_from_corpus` (bool), and "
            "the threshold fields (`baseline_recall_low`, `baseline_recall_low_adapted`, "
            "`ranking_degraded_spearman`, `ranking_degraded_spearman_adapted`, "
            "`pruning_ineffective`, `pruning_ineffective_adapted`, `rerank_lift_flat_delta`, "
            "`rerank_lift_steep_low`, `rerank_lift_steep_high`, and `ranking_degraded_recall`)."
        ),
    )
    async def recommend_efficiency_thresholds(
        ctx: _Ctx,
        days: int = 30,
        backend: str | None = None,
        metric: str | None = None,
        k: int | None = None,
    ) -> rag_telemetry.EfficiencyThresholds:
        result = await rag_telemetry.recommend_efficiency_thresholds(
            _driver(ctx),
            days=days,
            backend=backend,
            metric=metric,
            k=k,
        )
        return result


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
    ) -> turboquant.CreateIndexResult:
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
        return result

    @server.tool(
        name="reindex_turboquant_index",
        description=(
            "REINDEX a turboquant index. Pre-flight confirms the named "
            "index is actually a turboquant index (catalog lookup on "
            "pg_am) before running. ``concurrently=True`` is the default "
            "and runs on autocommit since REINDEX CONCURRENTLY can't "
            "run inside a transaction. Performs DDL — requires "
            "unrestricted mode + MCPG_ALLOW_DDL; pg_turboquant installed. "
            "Returns an object with `schema`, `index`, `concurrently` (bool), "
            "and `reindex_sql` (the rendered REINDEX statement that ran)."
        ),
    )
    async def reindex_turboquant_index(
        ctx: _Ctx,
        schema: str,
        index: str,
        concurrently: bool = True,
    ) -> turboquant.ReindexResult:
        database = ctx.request_context.lifespan_context.database
        result = await turboquant.reindex_turboquant_index(database, schema, index, concurrently=concurrently)
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_pg_search_ddl(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="create_pg_search_index",
        description=(
            "Build a CREATE INDEX … USING bm25 statement under tight "
            "allowlists and run it on autocommit (CONCURRENTLY can't run "
            "inside a transaction). ``columns`` is the list of attribute "
            "names the index covers; ``key_field`` is the primary-key "
            "column (required by upstream). All 13 documented bm25 "
            "reloptions are exposed as kwargs — the 7 JSONB-shaped "
            "options (``text_fields``, ``numeric_fields``, "
            "``boolean_fields``, ``json_fields``, ``range_fields``, "
            "``datetime_fields``, ``search_tokenizer``) accept Python "
            "dicts and serialize via json.dumps; the 2 int options "
            "(``target_segment_count``, ``mutable_segment_rows``) and "
            "3 text options (``layer_sizes``, ``background_layer_sizes``, "
            "``sort_by``) pass through type-aware validation. The "
            "rendered CREATE INDEX SQL is returned in ``create_sql`` "
            "for auditability. Performs DDL — requires unrestricted "
            "mode + MCPG_ALLOW_DDL; pg_search installed."
        ),
    )
    async def create_pg_search_index(
        ctx: _Ctx,
        schema: str,
        table: str,
        columns: list[str],
        index_name: str,
        key_field: str,
        text_fields: dict[str, Any] | None = None,
        numeric_fields: dict[str, Any] | None = None,
        boolean_fields: dict[str, Any] | None = None,
        json_fields: dict[str, Any] | None = None,
        range_fields: dict[str, Any] | None = None,
        datetime_fields: dict[str, Any] | None = None,
        layer_sizes: str | None = None,
        background_layer_sizes: str | None = None,
        target_segment_count: int | None = None,
        mutable_segment_rows: int | None = None,
        sort_by: str | None = None,
        search_tokenizer: dict[str, Any] | None = None,
        concurrently: bool = True,
    ) -> pg_search.CreatePgSearchIndexResult:
        database = ctx.request_context.lifespan_context.database
        result = await pg_search.create_pg_search_index(
            database,
            schema,
            table,
            columns,
            index_name,
            key_field,
            text_fields=text_fields,
            numeric_fields=numeric_fields,
            boolean_fields=boolean_fields,
            json_fields=json_fields,
            range_fields=range_fields,
            datetime_fields=datetime_fields,
            layer_sizes=layer_sizes,
            background_layer_sizes=background_layer_sizes,
            target_segment_count=target_segment_count,
            mutable_segment_rows=mutable_segment_rows,
            sort_by=sort_by,
            search_tokenizer=search_tokenizer,
            concurrently=concurrently,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="reindex_pg_search_index",
        description=(
            "REINDEX a pg_search BM25 index. Pre-flight confirms the "
            "named index actually uses the bm25 access method (catalog "
            "lookup on pg_am) before running. ``concurrently=True`` is "
            "the default and runs on autocommit since REINDEX "
            "CONCURRENTLY can't run inside a transaction. Performs DDL "
            "— requires unrestricted mode + MCPG_ALLOW_DDL; pg_search "
            "installed. "
            "Returns an object with `schema`, `index`, `concurrently` (bool), "
            "and `reindex_sql` (the rendered REINDEX statement that ran)."
        ),
    )
    async def reindex_pg_search_index(
        ctx: _Ctx,
        schema: str,
        index: str,
        concurrently: bool = True,
    ) -> pg_search.ReindexPgSearchResult:
        database = ctx.request_context.lifespan_context.database
        result = await pg_search.reindex_pg_search_index(database, schema, index, concurrently=concurrently)
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_redis_fdw_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_redis_foreign_servers",
        description=_with_example(
            "List the foreign servers backed by ``redis_fdw`` — the FDW that "
            "exposes a Redis instance as SQL-queryable foreign tables. "
            "Reports each server's connection address, port, database, TLS "
            "posture, and whether a user mapping (credential) is configured. "
            "Returns an empty list when the redis_fdw extension is not installed. "
            "Returns a list of objects with `name`, `address`, `port`, "
            "`database`, `tls` (bool), `password_configured` (bool), and "
            "`options` (the full server-options dict).",
            "list_redis_foreign_servers()",
        ),
    )
    async def list_redis_foreign_servers(ctx: _Ctx) -> list[redis_fdw.RedisForeignServer]:
        async def _run() -> list[redis_fdw.RedisForeignServer]:
            servers = await redis_fdw.list_redis_foreign_servers(_driver(ctx))
            return servers

        return await _cached_call(ctx, "list_redis_foreign_servers", _run)

    @server.tool(
        name="describe_redis_cache_table",
        description=_with_example(
            "Describe one foreign table backed by ``redis_fdw``: which server "
            "it's mapped to, the Redis-side key structure (`hash` / `list` / "
            "`string` / `set` / `zset`), key-prefix, TTL, and SQL-side column "
            "shape. Raises an error when the table doesn't exist or isn't "
            "backed by redis_fdw. "
            "Returns an object with `schema`, `name`, `server`, `key_type`, "
            "`key_prefix`, `ttl_seconds`, `columns` (list of `{name, data_type}`), "
            "and `options` (the full foreign-table options dict).",
            "describe_redis_cache_table(schema='public', table='sessions_cache')",
        ),
    )
    async def describe_redis_cache_table(ctx: _Ctx, schema: str, table: str) -> redis_fdw.RedisCacheTableInfo:
        async def _run() -> redis_fdw.RedisCacheTableInfo:
            info = await redis_fdw.describe_redis_cache_table(_driver(ctx), schema, table)
            return info

        return await _cached_call(ctx, "describe_redis_cache_table", _run, schema, table)

    @server.tool(
        name="get_redis_cache_stats",
        description=_with_example(
            "Best-effort cache metrics for a redis_fdw server. redis_fdw does "
            "not ship a uniform stats SQL surface across versions, so the tool "
            "validates that the server exists and otherwise reports "
            "`available=false` with a diagnostic. Operators wanting live "
            "metrics should query Redis directly (INFO / DBSIZE). "
            "Returns an object with `server`, `available` (bool), `key_count`, "
            "`used_memory_bytes`, and `detail` (a human-readable note).",
            "get_redis_cache_stats(server='redis_primary')",
        ),
    )
    async def get_redis_cache_stats(ctx: _Ctx, server: str) -> redis_fdw.RedisCacheStats:
        async def _run() -> redis_fdw.RedisCacheStats:
            stats = await redis_fdw.get_redis_cache_stats(_driver(ctx), server)
            return stats

        return await _cached_call(ctx, "get_redis_cache_stats", _run, server)

    @server.tool(
        name="recommend_redis_cache_targets",
        description=_with_example(
            "Recommend tables that would benefit from a Redis cache layer. "
            "Inspects ``pg_stat_user_tables`` for read-heavy, low-write "
            "relations whose working set fits comfortably in Redis "
            "(default: read/write ratio ≥ 10, ≥ 1000 reads, ≤ 1M rows). "
            "When ``server`` is provided the generated ``ready_to_run_sql`` "
            "stub targets that server name; otherwise the stub uses a "
            "placeholder operators must substitute. Advisor is read-only — "
            "never touches Redis itself. "
            "Returns an object with `server` and `candidates` — a list of "
            "objects with `schema`, `table`, `reads`, `writes`, "
            "`read_write_ratio`, `estimated_row_count`, `reason` "
            "(`read_only_lookup_table` / `small_hot_relation` / "
            "`read_heavy_low_write` / `moderate_read_dominant`), and "
            "`ready_to_run_sql` (a CREATE FOREIGN TABLE stub).",
            "recommend_redis_cache_targets(server='redis_primary', limit=10)",
        ),
    )
    async def recommend_redis_cache_targets(
        ctx: _Ctx,
        server: str | None = None,
        min_read_write_ratio: float = 10.0,
        min_reads_per_day: int = 1000,
        max_rows: int = 1_000_000,
        limit: int = 20,
    ) -> redis_fdw.RecommendRedisCacheTargetsResult:
        async def _run() -> redis_fdw.RecommendRedisCacheTargetsResult:
            result = await redis_fdw.recommend_redis_cache_targets(
                _driver(ctx),
                server=server,
                min_read_write_ratio=min_read_write_ratio,
                min_reads_per_day=min_reads_per_day,
                max_rows=max_rows,
                limit=limit,
            )
            return result

        return await _cached_call(
            ctx,
            "recommend_redis_cache_targets",
            _run,
            server,
            min_read_write_ratio,
            min_reads_per_day,
            max_rows,
            limit,
        )


def _register_redis_fdw_ddl(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="enable_redis_fdw",
        description=_with_example(
            "Install the ``redis_fdw`` extension. Thin wrapper over "
            "``enable_extension('redis_fdw')`` — convenient for agents that "
            "have located the cache-and-foreign-data bucket but haven't found "
            "the generic extension installer. redis_fdw runs in-process inside "
            "Postgres; operators should be aware of that operational "
            "implication before installing. Requires DDL mode. "
            "Returns an object with `name='redis_fdw'` and `enabled=true`.",
            "enable_redis_fdw()",
        ),
    )
    async def enable_redis_fdw(ctx: _Ctx) -> dict[str, Any]:
        result = await extensions.enable_extension(_driver(ctx), "redis_fdw")
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="create_redis_cache_server",
        description=_with_example(
            "Create a foreign server backed by ``redis_fdw`` "
            "(``CREATE SERVER … FOREIGN DATA WRAPPER redis_fdw OPTIONS (…)``). "
            "TLS is enabled by default; the tool refuses ``tls=false`` against "
            "a non-loopback Redis host unless ``allow_insecure_tls=true`` is "
            "passed explicitly. ``name`` must be a valid unquoted SQL "
            "identifier. Idempotent via ``IF NOT EXISTS``. Requires DDL mode. "
            "Returns an object with `name`, `address`, `port`, `database`, "
            "`tls`, and `created=true`.",
            "create_redis_cache_server(name='redis_primary', address='redis.internal', port=6379)",
        ),
    )
    async def create_redis_cache_server(
        ctx: _Ctx,
        name: str,
        address: str,
        port: int = 6379,
        database: int = 0,
        tls: bool = True,
        allow_insecure_tls: bool = False,
    ) -> redis_fdw.CreateRedisServerResult:
        result = await redis_fdw.create_redis_cache_server(
            _driver(ctx),
            name=name,
            address=address,
            port=port,
            database=database,
            tls=tls,
            allow_insecure_tls=allow_insecure_tls,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="create_redis_user_mapping",
        description=_with_example(
            "Create a user mapping for a redis_fdw foreign server. The Redis "
            "password is never accepted as a tool argument — pass "
            "``secret_ref`` (the name of a secret in the configured "
            "``MCPG_SECRETS_BACKEND``) and the tool resolves it before "
            "interpolating into the OPTIONS clause. ``user='public'`` creates "
            "a PUBLIC mapping; otherwise the user name must be a valid "
            "unquoted SQL identifier. Idempotent via ``IF NOT EXISTS``. "
            "Requires DDL mode. "
            "Returns an object with `server`, `user`, `secret_ref` (echoed by "
            "name, not value), and `created=true`.",
            "create_redis_user_mapping(server='redis_primary', user='public', secret_ref='REDIS_PASSWORD')",
        ),
    )
    async def create_redis_user_mapping(
        ctx: _Ctx,
        server: str,
        user: str,
        secret_ref: str,
    ) -> redis_fdw.CreateRedisUserMappingResult:
        import os

        from mcpg.secrets import build_secrets_provider

        secrets_provider, _backend = build_secrets_provider(os.environ)
        result = await redis_fdw.create_redis_user_mapping(
            _driver(ctx),
            server=server,
            user=user,
            secret_ref=secret_ref,
            secrets=secrets_provider,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="create_redis_cache_table",
        description=_with_example(
            "Create a foreign table backed by ``redis_fdw``. ``key_type`` is "
            "the Redis-side structure (one of ``hash`` / ``list`` / ``string`` "
            "/ ``set`` / ``zset``). ``columns`` is a list of "
            "``{name, type}`` dicts — every name must be a valid unquoted SQL "
            "identifier and every type a bare Postgres type. Optional "
            "``key_prefix`` and ``ttl_seconds`` are passed through as "
            "redis_fdw options. Idempotent via ``IF NOT EXISTS``. Requires "
            "DDL mode. "
            "Returns an object with `schema`, `name`, `server`, `key_type`, "
            "`columns` (tuple of column names), and `created=true`.",
            "create_redis_cache_table(schema='public', name='sessions_cache', "
            "server='redis_primary', key_type='hash', "
            "columns=[{'name': 'key', 'type': 'text'}, {'name': 'value', 'type': 'text'}], "
            "key_prefix='session:', ttl_seconds=3600)",
        ),
    )
    async def create_redis_cache_table(
        ctx: _Ctx,
        schema: str,
        name: str,
        server: str,
        key_type: str,
        columns: list[dict[str, str]],
        key_prefix: str | None = None,
        ttl_seconds: int | None = None,
    ) -> redis_fdw.CreateRedisCacheTableResult:
        result = await redis_fdw.create_redis_cache_table(
            _driver(ctx),
            schema=schema,
            name=name,
            server=server,
            key_type=key_type,
            columns=columns,
            key_prefix=key_prefix,
            ttl_seconds=ttl_seconds,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_cron_write(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="schedule_cron_job",
        description=(
            "Register a pg_cron job. ``schedule`` is a cron expression or "
            "pg_cron interval shortcut (e.g. '30 seconds'). Available only "
            "in unrestricted mode; requires pg_cron installed. "
            "Returns an object with `jobid` (the pg_cron job id), `name`, "
            "and `schedule`."
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


def _register_pgq_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_pgq_status",
        description=_with_example(
            "Report whether SQL/PGQ (the SQL standard for property graph "
            "queries, new in PG 19) is usable on this server. SQL/PGQ "
            "coexists with the AGE-style `graph_operations` bucket — "
            "`get_pgq_status` is the agent's hint about which surface to "
            "reach for. Never raises; on PG < 19 reports "
            "`available=false` with a diagnostic pointing at `run_cypher`. "
            "Returns an object with `available` (bool), `server_version_num` "
            "(int), `server_version` (the human-readable string), and "
            "`detail` (a guidance string the agent can surface to the user).",
            "get_pgq_status()",
        ),
    )
    async def get_pgq_status(ctx: _Ctx) -> pgq.PgqStatus:
        async def _run() -> pgq.PgqStatus:
            return await pgq.get_pgq_status(_driver(ctx))

        return await _cached_call(ctx, "get_pgq_status", _run)

    @server.tool(
        name="list_property_graphs",
        description=_with_example(
            "List SQL/PGQ property graphs defined in the database (reads "
            "`information_schema.sql_property_graphs`). Returns an empty "
            "list on PG < 19 or when the catalog view is missing (early "
            "Beta builds may not expose it yet — pair with `get_pgq_status` "
            "to disambiguate). "
            "Returns a list of objects with `schema`, `name`, "
            "`vertex_tables` (list of `schema.table` strings), and "
            "`edge_tables` (same shape).",
            "list_property_graphs()",
        ),
    )
    async def list_property_graphs(ctx: _Ctx) -> list[pgq.PropertyGraphInfo]:
        async def _run() -> list[pgq.PropertyGraphInfo]:
            return await pgq.list_property_graphs(_driver(ctx))

        return await _cached_call(ctx, "list_property_graphs", _run)

    @server.tool(
        name="describe_property_graph",
        description=_with_example(
            "Describe one SQL/PGQ property graph by schema-qualified name. "
            "Requires PG 19+; raises an error otherwise. Useful when an "
            "agent has located a graph via `list_property_graphs` and "
            "needs the full membership before composing a `run_pgq` query. "
            "Returns an object with `schema`, `name`, `vertex_tables` "
            "(list of `schema.table` strings), and `edge_tables` (same "
            "shape).",
            "describe_property_graph(schema='public', name='org_chart')",
        ),
    )
    async def describe_property_graph(ctx: _Ctx, schema: str, name: str) -> pgq.PropertyGraphInfo:
        async def _run() -> pgq.PropertyGraphInfo:
            return await pgq.describe_property_graph(_driver(ctx), schema, name)

        return await _cached_call(ctx, "describe_property_graph", _run, schema, name)

    @server.tool(
        name="run_pgq",
        description=_with_example(
            "Execute a SQL/PGQ `SELECT ... GRAPH_TABLE` query and return "
            "the rows. The query must be a single `SELECT` (or `WITH ... "
            "SELECT`) statement that references `GRAPH_TABLE` — anything "
            "else is refused at the boundary (use `run_select` for "
            "non-graph reads). `max_rows` caps the result set; when the "
            "cap is hit, `truncated=true`. Requires PG 19+. "
            "Returns an object with `columns` (list of column names from "
            "the query's `COLUMNS (...)` clause), `rows` (list of dicts "
            "keyed by those columns), `row_count`, and `truncated` (bool).",
            (
                'run_pgq(query="SELECT * FROM GRAPH_TABLE (org_chart '
                "MATCH (e:Employee)-[:REPORTS_TO]->(m:Manager) "
                'COLUMNS (e.name AS employee, m.name AS manager))", max_rows=50)'
            ),
        ),
    )
    async def run_pgq(ctx: _Ctx, query: str, max_rows: int = 200) -> pgq.PgqRunResult:
        return await pgq.run_pgq(_driver(ctx), query, max_rows=max_rows)


def _register_pgq_ddl(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="create_property_graph",
        description=_with_example(
            "Create a SQL/PGQ property graph. The tool composes the "
            '`CREATE PROPERTY GRAPH "schema"."name"` header itself; '
            "`definition_body` carries the `VERTEX TABLES (...)` (and "
            "optional `EDGE TABLES (...)`) clauses. The body must begin "
            "with `VERTEX TABLES` and must not contain `;` — that boundary "
            "rules out smuggling a different DDL statement via the "
            "parameter. Requires PG 19+ and DDL mode. "
            "Returns an object with `schema`, `name`, and `created=true`.",
            (
                "create_property_graph(schema='public', name='org_chart', "
                'definition_body="VERTEX TABLES (employees KEY (id) LABEL '
                "Employee PROPERTIES (id, name)) EDGE TABLES (reports_to "
                "SOURCE KEY (id) REFERENCES employees (id) DESTINATION KEY "
                '(manager_id) REFERENCES employees (id) LABEL REPORTS_TO)")'
            ),
        ),
    )
    async def create_property_graph(
        ctx: _Ctx,
        schema: str,
        name: str,
        definition_body: str,
    ) -> pgq.CreatePropertyGraphResult:
        result = await pgq.create_property_graph(
            _driver(ctx),
            schema=schema,
            name=name,
            definition_body=definition_body,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="drop_property_graph",
        description=_with_example(
            "Drop a SQL/PGQ property graph. ``if_exists=true`` (the "
            "default) makes the operation idempotent. Requires PG 19+ and "
            "DDL mode. "
            "Returns an object with `schema`, `name`, and `dropped=true`.",
            "drop_property_graph(schema='public', name='org_chart')",
        ),
    )
    async def drop_property_graph(
        ctx: _Ctx,
        schema: str,
        name: str,
        if_exists: bool = True,
    ) -> pgq.DropPropertyGraphResult:
        result = await pgq.drop_property_graph(
            _driver(ctx),
            schema=schema,
            name=name,
            if_exists=if_exists,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_pg_prewarm_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_prewarm_extension_status",
        description=_with_example(
            "Report whether ``pg_prewarm`` (and the supporting "
            "``pg_buffercache``) are installed, and whether ``pg_prewarm`` is "
            "listed in ``shared_preload_libraries`` (the autoprewarm worker "
            "requires it). "
            "Returns an object with `pg_prewarm_installed` (bool), "
            "`pg_buffercache_installed` (bool), `autoprewarm_libraries_present` "
            "(bool), and `shared_preload_libraries` (the raw setting).",
            "get_prewarm_extension_status()",
        ),
    )
    async def get_prewarm_extension_status(ctx: _Ctx) -> pg_prewarm.PrewarmExtensionStatus:
        async def _run() -> pg_prewarm.PrewarmExtensionStatus:
            status = await pg_prewarm.get_prewarm_extension_status(_driver(ctx))
            return status

        return await _cached_call(ctx, "get_prewarm_extension_status", _run)

    @server.tool(
        name="list_prewarmed_relations",
        description=_with_example(
            "Report current shared-buffer residency per relation, ranked by "
            "blocks-cached descending. Requires the ``pg_buffercache`` "
            "extension; returns an empty list when it's missing. "
            "Returns a list of objects with `schema`, `table`, "
            "`blocks_cached` (8 KiB pages currently in shared buffers), "
            "`total_blocks` (the relation's on-disk size in the same unit), "
            "`pct_cached` (the residency ratio rounded to 2 decimals), and "
            "`dirty_blocks` (pages with pending writes).",
            "list_prewarmed_relations(schema='public', limit=50)",
        ),
    )
    async def list_prewarmed_relations(
        ctx: _Ctx,
        schema: str | None = None,
        limit: int = 100,
    ) -> list[pg_prewarm.PrewarmedRelation]:
        async def _run() -> list[pg_prewarm.PrewarmedRelation]:
            relations = await pg_prewarm.list_prewarmed_relations(_driver(ctx), schema=schema, limit=limit)
            return relations

        return await _cached_call(ctx, "list_prewarmed_relations", _run, schema, limit)

    @server.tool(
        name="recommend_prewarm_targets",
        description=_with_example(
            "Recommend relations whose first-query latency would benefit from "
            "``pg_prewarm``. Inspects ``pg_stat_user_tables`` + "
            "``pg_statio_user_tables`` to find high cold-miss-rate / "
            "seq_scan-dominant relations, and caps the cumulative cost at "
            "``shared_buffers_budget_pct`` * ``shared_buffers`` so the "
            "recommendation never silently exceeds shared_buffers. The "
            "advisor is read-only — never invokes pg_prewarm itself. "
            "Returns an object with `shared_buffers_blocks` (configured "
            "shared_buffers in 8 KiB pages), `budget_blocks` (the cap), "
            "`total_cost_blocks` (sum of recommendations actually returned), "
            "and `candidates` — a list of objects with `schema`, `relation`, "
            "`reason` (`seq_scan_dominant` / `high_cold_miss_rate` / "
            "`small_hot_relation_uncached` / `index_in_critical_path`), "
            "`prewarm_mode`, `estimated_buffer_cost`, `heap_blks_read`, "
            "`heap_blks_hit`, `cache_miss_ratio`, and `ready_to_run_sql`.",
            "recommend_prewarm_targets(shared_buffers_budget_pct=60.0, limit=10)",
        ),
    )
    async def recommend_prewarm_targets(
        ctx: _Ctx,
        shared_buffers_budget_pct: float = 60.0,
        min_heap_blks_read: int = 1000,
        limit: int = 20,
        prewarm_mode: str = "buffer",
    ) -> pg_prewarm.RecommendPrewarmTargetsResult:
        async def _run() -> pg_prewarm.RecommendPrewarmTargetsResult:
            result = await pg_prewarm.recommend_prewarm_targets(
                _driver(ctx),
                shared_buffers_budget_pct=shared_buffers_budget_pct,
                min_heap_blks_read=min_heap_blks_read,
                limit=limit,
                prewarm_mode=prewarm_mode,
            )
            return result

        return await _cached_call(
            ctx,
            "recommend_prewarm_targets",
            _run,
            shared_buffers_budget_pct,
            min_heap_blks_read,
            limit,
            prewarm_mode,
        )

    @server.tool(
        name="list_autowarm_jobs",
        description=_with_example(
            "List the pg_cron jobs MCPg registered for autowarm "
            "(``jobname LIKE 'mcpg_autowarm%'``). Returns an empty list when "
            "``pg_cron`` is not installed. "
            "Returns a list of objects with `jobid`, `jobname`, `schedule` "
            "(the cron expression), and `command` (the SELECT the job runs).",
            "list_autowarm_jobs()",
        ),
    )
    async def list_autowarm_jobs(ctx: _Ctx) -> list[pg_prewarm.AutowarmJob]:
        async def _run() -> list[pg_prewarm.AutowarmJob]:
            jobs = await pg_prewarm.list_autowarm_jobs(_driver(ctx))
            return jobs

        return await _cached_call(ctx, "list_autowarm_jobs", _run)


def _register_pg_prewarm_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="prewarm_relation",
        description=_with_example(
            "Run ``SELECT pg_prewarm('schema.relation'::regclass, mode)`` "
            "once. ``mode`` is one of ``buffer`` (load into shared buffers), "
            "``prefetch`` (OS page cache), or ``read`` (blocking read). "
            "Requires pg_prewarm installed. "
            "Returns an object with `schema`, `relation`, `mode`, and "
            "`blocks_prewarmed`.",
            "prewarm_relation(schema='public', relation='orders', mode='buffer')",
        ),
    )
    async def prewarm_relation(
        ctx: _Ctx,
        schema: str,
        relation: str,
        mode: str = "buffer",
    ) -> pg_prewarm.PrewarmResult:
        result = await pg_prewarm.prewarm_relation(_driver(ctx), schema=schema, relation=relation, mode=mode)
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="prewarm_recommended",
        description=_with_example(
            "Invoke ``recommend_prewarm_targets`` and prewarm every "
            "recommendation in order. ``dry_run=true`` reports what would be "
            "prewarmed without invoking pg_prewarm. Per-relation errors are "
            "captured and reported in the ``outcomes`` list so one bad "
            "relation doesn't fail the whole bulk pass. "
            "Returns an object with `dry_run` (bool), `total_blocks` (sum of "
            "blocks_prewarmed across successful outcomes), and `outcomes` — "
            "a list of objects with `schema`, `relation`, `mode`, "
            "`blocks_prewarmed`, and `error` (null on success).",
            "prewarm_recommended(shared_buffers_budget_pct=60.0, dry_run=False)",
        ),
    )
    async def prewarm_recommended(
        ctx: _Ctx,
        shared_buffers_budget_pct: float = 60.0,
        min_heap_blks_read: int = 1000,
        limit: int = 20,
        prewarm_mode: str = "buffer",
        dry_run: bool = False,
    ) -> pg_prewarm.BulkPrewarmResult:
        result = await pg_prewarm.prewarm_recommended(
            _driver(ctx),
            shared_buffers_budget_pct=shared_buffers_budget_pct,
            min_heap_blks_read=min_heap_blks_read,
            limit=limit,
            prewarm_mode=prewarm_mode,
            dry_run=dry_run,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="schedule_autowarm",
        description=_with_example(
            "Register a pg_cron job that calls the bulk prewarm helper at "
            "``schedule``. Default schedule is ``@reboot`` — the canonical "
            "'warm after restart' loop. Requires pg_cron installed. The "
            "cron command embeds the budget / limit / mode arguments and "
            "calls a `mcpg.prewarm_recommended_cron(...)` SQL function the "
            "operator must install separately (template in "
            "``docs/plans/pg-prewarm-advisor.md``). "
            "Returns an object with `jobid`, `name`, and `schedule`.",
            "schedule_autowarm(name='mcpg_autowarm', schedule='@reboot')",
        ),
    )
    async def schedule_autowarm(
        ctx: _Ctx,
        name: str = "mcpg_autowarm",
        schedule: str = "@reboot",
        shared_buffers_budget_pct: float = 60.0,
        min_heap_blks_read: int = 1000,
        limit: int = 20,
        prewarm_mode: str = "buffer",
    ) -> pg_prewarm.ScheduleAutowarmResult:
        result = await pg_prewarm.schedule_autowarm(
            _driver(ctx),
            name=name,
            schedule=schedule,
            shared_buffers_budget_pct=shared_buffers_budget_pct,
            min_heap_blks_read=min_heap_blks_read,
            limit=limit,
            prewarm_mode=prewarm_mode,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="unschedule_autowarm",
        description=_with_example(
            "Remove an autowarm pg_cron job by name. Idempotent — returns "
            "``removed=false`` when the job didn't exist (or pg_cron isn't "
            "installed). "
            "Returns an object with `name` and `removed` (bool).",
            "unschedule_autowarm(name='mcpg_autowarm')",
        ),
    )
    async def unschedule_autowarm(ctx: _Ctx, name: str = "mcpg_autowarm") -> pg_prewarm.UnscheduleAutowarmResult:
        result = await pg_prewarm.unschedule_autowarm(_driver(ctx), name=name)
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_pg19_runtime_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_data_checksums_status",
        description=_with_example(
            "Report whether PG 19's online `data_checksums` toggle is "
            "usable + the current setting. Never raises — driver-level "
            "errors surface as `available=false`. On PG ≤ 18 reports "
            "`available=false` but still surfaces the current state "
            "(set at initdb time) so the agent has context, and points "
            "at the offline `pg_checksums` fallback. "
            "Returns an object with `available` (bool), `server_version_num` "
            "(int), `server_version`, `enabled` (bool / null), and "
            "`detail` (guidance string).",
            "get_data_checksums_status()",
        ),
    )
    async def get_data_checksums_status(ctx: _Ctx) -> pg19_runtime.DataChecksumsStatus:
        # Live cluster setting — never cache (gemini-review pattern from
        # PR #141): an agent toggling checksums needs to see the
        # post-toggle state on the next probe.
        return await pg19_runtime.get_data_checksums_status(_driver(ctx))

    @server.tool(
        name="get_logical_replication_status",
        description=_with_example(
            "Report whether PG 19's on-demand wal_level flip is usable, "
            "plus the configured `wal_level`, the new PG 19 "
            "`effective_wal_level` preset GUC, and `max_replication_slots`. "
            "When configured and effective diverge the agent can tell that "
            "an `ALTER SYSTEM` has been done but a reload is still pending. "
            "Never raises. "
            "Returns an object with `available` (bool), `server_version_num`, "
            "`server_version`, `wal_level`, `effective_wal_level`, "
            "`max_replication_slots`, and `detail`.",
            "get_logical_replication_status()",
        ),
    )
    async def get_logical_replication_status(
        ctx: _Ctx,
    ) -> pg19_runtime.LogicalReplicationStatus:
        # Live setting — never cache; status reflects post-toggle state.
        return await pg19_runtime.get_logical_replication_status(_driver(ctx))


def _register_pg19_runtime_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="enable_data_checksums",
        description=_with_example(
            "Turn `data_checksums` on without restarting the cluster. "
            "Calls PG 19's `pg_enable_data_checksums()` SQL function; the "
            "rewrite of affected pages happens in the background via the "
            "new `data_checksum_worker`. No-op (`changed=false`) when "
            "checksums are already on. Requires PG 19+; raises an error "
            "on older servers (pair with `get_data_checksums_status` to "
            "feature-detect, or fall back to the offline `pg_checksums` "
            "tool on PG ≤ 18). "
            "Returns an object with `was_enabled` (bool / null), "
            "`now_enabled` (bool), `changed` (bool), and `toggle_sql` "
            "(the rendered SQL that actually executed).",
            "enable_data_checksums()",
        ),
    )
    async def enable_data_checksums(ctx: _Ctx) -> pg19_runtime.ToggleDataChecksumsResult:
        database = ctx.request_context.lifespan_context.database
        result = await pg19_runtime.enable_data_checksums(database)
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="disable_data_checksums",
        description=_with_example(
            "Turn `data_checksums` off without restarting the cluster. "
            "Calls PG 19's `pg_disable_data_checksums()` SQL function. "
            "No-op (`changed=false`) when checksums are already off. "
            "Requires PG 19+. "
            "Returns an object with `was_enabled`, `now_enabled` "
            "(`false`), `changed`, and `toggle_sql`.",
            "disable_data_checksums()",
        ),
    )
    async def disable_data_checksums(ctx: _Ctx) -> pg19_runtime.ToggleDataChecksumsResult:
        database = ctx.request_context.lifespan_context.database
        result = await pg19_runtime.disable_data_checksums(database)
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="enable_logical_replication_on_demand",
        description=_with_example(
            "Flip `wal_level` to `'logical'` without restarting the "
            "cluster. Issues `ALTER SYSTEM SET wal_level = 'logical'` "
            "followed by `pg_reload_conf()`; on PG 19+ the change takes "
            "effect for new WAL traffic immediately. No-op when "
            "`wal_level` is already `'logical'`. Requires PG 19+; raises "
            "on older servers where the flip still needs a planned restart. "
            "Returns an object with `previous_wal_level` (str / null), "
            "`new_wal_level` (str), `requires_restart` (bool), and `detail`.",
            "enable_logical_replication_on_demand()",
        ),
    )
    async def enable_logical_replication_on_demand(
        ctx: _Ctx,
    ) -> pg19_runtime.EnableLogicalReplicationOnDemandResult:
        database = ctx.request_context.lifespan_context.database
        result = await pg19_runtime.enable_logical_replication_on_demand(database)
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_pg19_ddl_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_pg19_ddl_status",
        description=_with_example(
            "Report whether PG 19's `pg_get_roledef()` / `pg_get_databasedef()` "
            "/ `pg_get_tablespacedef()` DDL-dump functions are usable on this "
            "server. Never raises — driver-level errors surface as "
            "`available=false`. On PG ≤ 18 reports `available=false` and "
            "points the agent at `pg_dumpall --roles-only` / `--globals-only` "
            "/ `--tablespaces-only` as the fallback. "
            "Returns an object with `available` (bool), `server_version_num` "
            "(int), `server_version`, `has_pg_get_roledef` (bool), "
            "`has_pg_get_databasedef` (bool), `has_pg_get_tablespacedef` "
            "(bool), and `detail` (guidance string).",
            "get_pg19_ddl_status()",
        ),
    )
    async def get_pg19_ddl_status(ctx: _Ctx) -> pg19_ddl.Pg19DdlStatus:
        # Catalog probe — never cache: an extension install / build flip
        # mid-session needs to be visible on the next call.
        # Returns the dataclass directly — FastMCP auto-derives the
        # outputSchema from the type annotation (PR-13: structured
        # tool outputs so LangChain / LangGraph clients can validate
        # responses against a Pydantic model).
        return await pg19_ddl.get_pg19_ddl_status(_driver(ctx))

    @server.tool(
        name="get_role_ddl",
        description=_with_example(
            "Return the `CREATE ROLE` DDL for a named role using PG 19's "
            "`pg_get_roledef(oid)` function — no shell-out to `pg_dumpall "
            "--roles-only` required. Returns `found=false` with an empty "
            "`ddl` when the role doesn't exist. Requires PG 19+; raises "
            "on older servers (pair with `get_pg19_ddl_status` to "
            "feature-detect). "
            "Returns an object with `object_type` (`'role'`), `object_name`, "
            "`found` (bool), and `ddl` (the verbatim CREATE statement).",
            "get_role_ddl(role_name='app_user')",
        ),
    )
    async def get_role_ddl(ctx: _Ctx, role_name: str) -> pg19_ddl.ObjectDdlResult:
        return await pg19_ddl.get_role_ddl(_driver(ctx), role_name)

    @server.tool(
        name="get_database_ddl",
        description=_with_example(
            "Return the `CREATE DATABASE` DDL for a named database using "
            "PG 19's `pg_get_databasedef(oid)` function. Returns "
            "`found=false` with an empty `ddl` when the database doesn't "
            "exist. Requires PG 19+. "
            "Returns an object with `object_type` (`'database'`), "
            "`object_name`, `found`, and `ddl`.",
            "get_database_ddl(database_name='analytics')",
        ),
    )
    async def get_database_ddl(ctx: _Ctx, database_name: str) -> pg19_ddl.ObjectDdlResult:
        return await pg19_ddl.get_database_ddl(_driver(ctx), database_name)

    @server.tool(
        name="get_tablespace_ddl",
        description=_with_example(
            "Return the `CREATE TABLESPACE` DDL for a named tablespace using "
            "PG 19's `pg_get_tablespacedef(oid)` function. Returns "
            "`found=false` with an empty `ddl` when the tablespace doesn't "
            "exist. Requires PG 19+. "
            "Returns an object with `object_type` (`'tablespace'`), "
            "`object_name`, `found`, and `ddl`.",
            "get_tablespace_ddl(tablespace_name='fast_ssd')",
        ),
    )
    async def get_tablespace_ddl(ctx: _Ctx, tablespace_name: str) -> pg19_ddl.ObjectDdlResult:
        return await pg19_ddl.get_tablespace_ddl(_driver(ctx), tablespace_name)


def _register_pg19_ddl_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="validate_check_constraint",
        description=_with_example(
            "Run `ALTER TABLE schema.table VALIDATE CONSTRAINT name` to "
            "validate a constraint that was originally added with "
            "`NOT VALID`. Idempotent — when the constraint is already "
            "validated, returns `changed=false` and emits no DDL. Works "
            "on every supported PG version (the SQL has been around "
            "since 9.x); ships in the PG 19 DDL helpers module because "
            "it closes the create-NOT-VALID / validate-later loop on "
            "the agent surface. "
            "Returns an object with `table_schema`, `table`, "
            "`constraint_name`, `was_valid` (bool / null), `now_valid` "
            "(bool), `changed` (bool), and `validate_sql` (the rendered "
            "DDL that actually executed, or a `-- no-op` comment when "
            "nothing was needed). The field is named `table_schema` (not "
            "`schema`) to avoid colliding with Pydantic's reserved name "
            "in the auto-derived outputSchema.",
            "validate_check_constraint(schema='public', table='orders', constraint_name='orders_total_nonneg')",
        ),
    )
    async def validate_check_constraint(
        ctx: _Ctx,
        schema: str,
        table: str,
        constraint_name: str,
    ) -> pg19_ddl.ValidateCheckConstraintResult:
        database = ctx.request_context.lifespan_context.database
        result = await pg19_ddl.validate_check_constraint(
            database,
            schema=schema,
            table=table,
            constraint_name=constraint_name,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_pg19_skip_scan_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_skip_scan_status",
        description=_with_example(
            "Report whether PG 19's B-tree skip-scan optimisation is "
            "the planner default on this server. Never raises — "
            "driver-level errors surface as `available=false`. On "
            "PG ≤ 18 reports `available=false` and points the agent at "
            "the standard 'add a dedicated single-column index' "
            "fallback. "
            "Returns an object with `available` (bool), "
            "`server_version_num` (int), `server_version`, and "
            "`detail` (guidance string).",
            "get_skip_scan_status()",
        ),
    )
    async def get_skip_scan_status(ctx: _Ctx) -> pg19_skip_scan.SkipScanStatus:
        # Live version probe — never cache. A server upgrade mid-session
        # needs to flip availability on the next call.
        return await pg19_skip_scan.get_skip_scan_status(_driver(ctx))

    @server.tool(
        name="recommend_skip_scan_indexes",
        description=_with_example(
            "Find composite B-tree indexes whose leading column has "
            "low NDV — these are the ones PG 19's skip-scan optimisation "
            "unlocks. Each candidate's trailing columns can now be "
            "served by the composite index alone, so any dedicated "
            "single-column indexes on those trailing columns become "
            "review candidates for `recommend_index_drops`. Returns an "
            "empty list on PG ≤ 18 or driver failure — pair with "
            "`get_skip_scan_status` for the diagnostic. "
            "`max_leading_ndv` (default 1000) caps the leading-column "
            "NDV that's considered low enough for skip-scan to be "
            "profitable. "
            "Returns a list of objects with `schema`, `table`, "
            "`index_name`, `leading_column`, `trailing_columns` "
            "(list of strings), `estimated_leading_ndv` (int), and "
            "`rationale` (human-readable explanation).",
            "recommend_skip_scan_indexes()",
        ),
    )
    async def recommend_skip_scan_indexes(
        ctx: _Ctx, max_leading_ndv: int = 1000
    ) -> list[pg19_skip_scan.SkipScanCandidate]:
        # Live catalog + stats walk — never cache. ANALYZE / index DDL
        # changes need to be visible on the next call.
        return await pg19_skip_scan.recommend_skip_scan_indexes(_driver(ctx), max_leading_ndv=max_leading_ndv)


def _register_pg19_partitions_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_pg19_partitions_status",
        description=_with_example(
            "Report whether PG 19's `ALTER TABLE … MERGE PARTITIONS` and "
            "`ALTER TABLE … SPLIT PARTITION` forms are usable on this "
            "server. Never raises — driver-level errors surface as "
            "`available=false`. On PG ≤ 18 reports `available=false` and "
            "points the agent at the detach / create / attach fallback "
            "path. "
            "Returns an object with `available` (bool), `server_version_num` "
            "(int), `server_version`, and `detail` (guidance string).",
            "get_pg19_partitions_status()",
        ),
    )
    async def get_pg19_partitions_status(ctx: _Ctx) -> pg19_partitions.Pg19PartitionsStatus:
        # Cluster version probe — never cache: a server upgrade mid-session
        # needs to flip availability on the next call.
        return await pg19_partitions.get_pg19_partitions_status(_driver(ctx))


def _register_pg19_partitions_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="merge_partitions",
        description=_with_example(
            "Consolidate two or more partitions into a single new "
            "partition using PG 19's `ALTER TABLE … MERGE PARTITIONS (p1, "
            "p2, …) INTO new`. Reuses the existing partition data "
            "files — no row-by-row copy. PG validates that the source "
            "partition bounds are contiguous; non-adjacent ranges are "
            "rejected by the server. Requires PG 19+; raises on older "
            "versions with the detach / create / attach fallback in the "
            "message. "
            "Returns an object with `parent_schema`, `parent_table`, "
            "`source_partitions` (list of strings), `target_partition`, "
            "and `merge_sql` (the rendered DDL that actually executed).",
            "merge_partitions(parent_schema='public', parent_table='measurement', "
            "source_partitions=['measurement_y2024_q1', 'measurement_y2024_q2'], "
            "target_partition_name='measurement_y2024_h1')",
        ),
    )
    async def merge_partitions(
        ctx: _Ctx,
        parent_schema: str,
        parent_table: str,
        source_partitions: list[str],
        target_partition_name: str,
    ) -> pg19_partitions.MergePartitionsResult:
        database = ctx.request_context.lifespan_context.database
        result = await pg19_partitions.merge_partitions(
            database,
            parent_schema=parent_schema,
            parent_table=parent_table,
            source_partitions=source_partitions,
            target_partition_name=target_partition_name,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result

    @server.tool(
        name="split_partition",
        description=_with_example(
            "Split one partition into two or more new partitions using "
            "PG 19's `ALTER TABLE … SPLIT PARTITION existing INTO ("
            "PARTITION new1 FOR VALUES …, PARTITION new2 FOR VALUES …)`. "
            "`new_partitions` is a list of `{name, for_values_clause}` "
            "objects — the `name` is quoted as an identifier; the "
            "`for_values_clause` is embedded verbatim (DDL grammar can't "
            "parameter-bind partition bounds — caller composes safe "
            "fragments from validated values). Example RANGE form: "
            "`\"FROM ('2024-01-01') TO ('2024-04-01')\"`. Requires PG 19+. "
            "Returns an object with `parent_schema`, `parent_table`, "
            "`source_partition`, `new_partitions` (the new names), and "
            "`split_sql`.",
            "split_partition(parent_schema='public', parent_table='measurement', "
            "source_partition='measurement_y2024', new_partitions=["
            "{'name': 'measurement_y2024_h1', 'for_values_clause': \"FROM ('2024-01-01') TO ('2024-07-01')\"}, "
            "{'name': 'measurement_y2024_h2', 'for_values_clause': \"FROM ('2024-07-01') TO ('2025-01-01')\"}])",
        ),
    )
    async def split_partition(
        ctx: _Ctx,
        parent_schema: str,
        parent_table: str,
        source_partition: str,
        new_partitions: list[dict[str, str]],
    ) -> pg19_partitions.SplitPartitionResult:
        # Wire-format adapter: MCP passes list-of-dict; helper expects
        # a typed dataclass. Validate keys here so a missing field
        # surfaces a sane error message instead of an AttributeError.
        try:
            specs = [
                pg19_partitions.SplitPartitionSpec(
                    name=spec["name"],
                    for_values_clause=spec["for_values_clause"],
                )
                for spec in new_partitions
            ]
        except KeyError as exc:
            raise pg19_partitions.Pg19PartitionsError(
                f"new_partitions entry missing required key {exc.args[0]!r}; "
                "expected {'name': str, 'for_values_clause': str}."
            ) from exc
        database = ctx.request_context.lifespan_context.database
        result = await pg19_partitions.split_partition(
            database,
            parent_schema=parent_schema,
            parent_table=parent_table,
            source_partition=source_partition,
            new_partitions=specs,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_wait_for_lsn_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_wait_for_lsn_status",
        description=_with_example(
            "Report whether PG 19's `WAIT FOR LSN` is usable on this server. "
            "Also reports whether the current backend is a standby (the only "
            "context where the wait does meaningful work). Never raises. "
            "On PG ≤ 18 returns `available=false` and points at the poll-loop "
            "fallback. "
            "Returns an object with `available` (bool), `server_version_num` "
            "(int), `server_version`, `is_in_recovery` (bool), and `detail`.",
            "get_wait_for_lsn_status()",
        ),
    )
    async def get_wait_for_lsn_status(ctx: _Ctx) -> wait_for_lsn.WaitForLsnStatus:
        # Live cluster state — never cache. is_in_recovery flips on
        # promotion / demotion and the agent needs to see the new
        # state on the next call.
        return await wait_for_lsn.get_wait_for_lsn_status(_driver(ctx))

    @server.tool(
        name="get_current_wal_lsn",
        description=_with_example(
            "Return the current WAL LSN — write-side on a primary "
            "(`pg_current_wal_lsn()`) or replay-side on a standby "
            "(`pg_last_wal_replay_lsn()`). Natural pairing for the "
            "read-your-writes workflow: capture on the primary right "
            "after a write, then pass to `wait_for_lsn` on the standby "
            "session before the follow-up read. Works on every "
            "supported PG version. "
            "Returns an object with `role` (`'primary'` or `'standby'`) "
            "and `lsn` (PostgreSQL LSN literal, e.g. `'0/1234ABCD'`).",
            "get_current_wal_lsn()",
        ),
    )
    async def get_current_wal_lsn(ctx: _Ctx) -> wait_for_lsn.CurrentWalLsnResult:
        # Snapshot — never cache; the LSN advances monotonically with
        # every WAL-emitting transaction.
        return await wait_for_lsn.get_current_wal_lsn(_driver(ctx))

    @server.tool(
        name="recommend_read_your_writes",
        description=_with_example(
            "Advise whether the caller should use `WAIT FOR LSN` for "
            "read-your-writes consistency. Combines server role "
            "(primary vs standby), replay lag, and PG version into a "
            "structured recommendation. Never raises. `reason` is one "
            "of `primary_no_wait_needed`, `standby_no_lag`, "
            "`standby_lag_unknown`, `standby_with_lag`, "
            "`standby_pg18_or_older`, `unavailable`. "
            "Returns an object with `recommend_use` (bool), `reason`, "
            "`is_in_recovery`, `server_version_num`, `current_lag_bytes` "
            "(int / null), and `detail`.",
            "recommend_read_your_writes()",
        ),
    )
    async def recommend_read_your_writes(
        ctx: _Ctx,
    ) -> wait_for_lsn.ReadYourWritesRecommendation:
        # Live advisor — never cache. Lag and role change in real time.
        return await wait_for_lsn.recommend_read_your_writes(_driver(ctx))


def _register_wait_for_lsn_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="wait_for_lsn",
        description=_with_example(
            "Issue `WAIT FOR LSN '<lsn>' TIMEOUT <ms>` and block until "
            "WAL replay catches up on the connected backend. `timeout_ms` "
            "defaults to 0 (wait indefinitely); a positive value bounds "
            "the wait — on timeout the helper returns `timed_out=true` "
            "rather than raising so the caller can decide whether to "
            "retry or fall through. LSN format is strictly validated "
            "(`hex/hex`) before any SQL is composed. Requires PG 19+; "
            "raises on older servers with the poll-loop fallback in "
            "the message. "
            "Returns an object with `lsn`, `timeout_ms` (int), `timed_out` "
            "(bool), and `wait_sql` (the rendered statement).",
            "wait_for_lsn(lsn='0/1234ABCD', timeout_ms=5000)",
        ),
    )
    async def wait_for_lsn_tool(ctx: _Ctx, lsn: str, timeout_ms: int = 0) -> wait_for_lsn.WaitForLsnResult:
        return await wait_for_lsn.wait_for_lsn(_driver(ctx), lsn=lsn, timeout_ms=timeout_ms)


def _register_pg19_stats_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_pg19_stats_status",
        description=_with_example(
            "Report whether PG 19's `pg_stat_lock` and `pg_stat_recovery` "
            "views are usable on this server. Never raises — driver-level "
            "errors surface as `available=false`. On PG < 19 returns "
            "`available=false` with a diagnostic pointing the agent at "
            "`find_blocking_chains` / `pg_stat_replication`. "
            "Returns an object with `available` (bool), `server_version_num` "
            "(int), `server_version`, `has_pg_stat_lock` (bool), "
            "`has_pg_stat_recovery` (bool), and `detail` (guidance string).",
            "get_pg19_stats_status()",
        ),
    )
    async def get_pg19_stats_status(ctx: _Ctx) -> pg19_stats.Pg19StatsStatus:
        async def _run() -> pg19_stats.Pg19StatsStatus:
            return await pg19_stats.get_pg19_stats_status(_driver(ctx))

        return await _cached_call(ctx, "get_pg19_stats_status", _run)

    @server.tool(
        name="read_pg_stat_lock",
        description=_with_example(
            "Return every row from PG 19's `pg_stat_lock` view "
            "(per-lock-type acquire / wait / wait-time counters since the "
            "most recent `pg_stat_reset`). Empty list on PG < 19 or when "
            "the view isn't present. "
            "Returns a list of objects with `lock_type` (relation / page / "
            "tuple / xid / virtualxid / advisory / ...), `acquires`, "
            "`waits`, and `wait_time_us`.",
            "read_pg_stat_lock()",
        ),
    )
    async def read_pg_stat_lock(ctx: _Ctx) -> list[pg19_stats.LockStatRow]:
        # Live counters — never cache (matches find_blocking_chains /
        # read_pg_stat_io pattern). Caching would return stale waits
        # during real-time incident response (gemini review on PR #141).
        return await pg19_stats.read_pg_stat_lock(_driver(ctx))

    @server.tool(
        name="read_pg_stat_recovery",
        description=_with_example(
            "Return rows from PG 19's `pg_stat_recovery` view — replay "
            "progress, lag, and startup state for a standby. Empty list "
            "on PG < 19, when the view isn't present, or when the server "
            "isn't in recovery (a primary running standalone returns no "
            "rows). "
            "Returns a list of objects with `replay_lsn`, "
            "`replay_lag_seconds`, `last_replayed_at`, and `startup_state` "
            "(any of which may be null when not applicable).",
            "read_pg_stat_recovery()",
        ),
    )
    async def read_pg_stat_recovery(ctx: _Ctx) -> list[pg19_stats.RecoveryStatRow]:
        # Live replay state — never cache. Stale lag readings during
        # standby triage are worse than useless (gemini review on PR #141).
        return await pg19_stats.read_pg_stat_recovery(_driver(ctx))

    @server.tool(
        name="analyze_lock_hotspots",
        description=_with_example(
            "Rank PG 19 `pg_stat_lock` rows by wait dominance and surface "
            "stable reason codes. Read-only — never modifies state. Pair "
            "with `find_blocking_chains` for the active culprits on a "
            "specific hot lock_type. "
            "Returns an object with `available` (bool), "
            "`server_version_num`, `detail`, and `hotspots` — a list of "
            "objects with `lock_type`, `waits`, `wait_time_us`, `reason` "
            "(one of `contention_dominant` / `high_wait_time` / "
            "`high_wait_count` / `low_contention`), and "
            "`suggested_followup` (a human-readable next-step string).",
            "analyze_lock_hotspots()",
        ),
    )
    async def analyze_lock_hotspots(ctx: _Ctx) -> pg19_stats.LockHotspotsResult:
        # Live advisor over live counters — never cache. Incident-response
        # callers need the current snapshot, not what we saw 60s ago
        # (gemini review on PR #141).
        return await pg19_stats.analyze_lock_hotspots(_driver(ctx))


def _register_aio_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_aio_status",
        description=_with_example(
            "Report whether PG 19's async-I/O subsystem is usable. On "
            "PG < 19 returns `available=false` with a diagnostic pointing "
            "the agent at the existing `read_pg_stat_io` / `run_maintenance` "
            "tools. Never raises — driver-level errors during the version "
            "or settings probe also surface as `available=false`. "
            "Returns an object with `available` (bool), `server_version_num` "
            "(int), `server_version`, `io_method` ('sync' / 'worker' / "
            "'io_uring' / null), `io_min_workers`, `io_max_workers`, and "
            "`detail` (a guidance string).",
            "get_aio_status()",
        ),
    )
    async def get_aio_status(ctx: _Ctx) -> aio.AioStatus:
        async def _run() -> aio.AioStatus:
            return await aio.get_aio_status(_driver(ctx))

        return await _cached_call(ctx, "get_aio_status", _run)

    @server.tool(
        name="recommend_io_method",
        description=_with_example(
            "Recommend a PG 19 `io_method` ('sync' / 'worker' / 'io_uring') "
            "for the current workload. Reads `pg_stat_database` aggregates "
            "(blks_read / blks_hit / stats_reset) and the current setting, "
            "then maps the workload signals to one of: "
            "`high_concurrent_read_load` (→ io_uring), "
            "`bursty_io_with_cache_pressure` (→ worker), "
            "`low_io_pressure` (→ sync), `current_setting_optimal` "
            "(no change), or `insufficient_stats` (window too short). "
            "Read-only — never invokes ALTER SYSTEM; emits a "
            "ready_to_run_sql snippet the operator can paste when the "
            "recommendation differs from the current setting. "
            "Returns an object with `available` (bool), `server_version_num`, "
            "`detail`, and `recommendations` — a list of objects with "
            "`recommended_method`, `reason`, `current_method`, "
            "`cache_miss_ratio`, `reads_per_second`, `stats_window_seconds`, "
            "and `ready_to_run_sql` (null when no change suggested).",
            "recommend_io_method()",
        ),
    )
    async def recommend_io_method(ctx: _Ctx) -> aio.RecommendIoMethodResult:
        async def _run() -> aio.RecommendIoMethodResult:
            return await aio.recommend_io_method(_driver(ctx))

        return await _cached_call(ctx, "recommend_io_method", _run)


def _register_repack_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_repack_status",
        description=_with_example(
            "Report whether PG 19's in-server `REPACK` command is usable. "
            "On PG < 19 returns `available=false` with a diagnostic pointing "
            "the agent at the long-standing pg_repack extension shell-out "
            "path so the fallback is clear. The in-server `REPACK "
            "CONCURRENTLY` is the headline PG 19 operational win — "
            "online table rebuild with no blocking writers. "
            "Returns an object with `available` (bool), `server_version_num` "
            "(int), `server_version` (the human-readable string), and "
            "`detail` (a guidance string the agent can surface to the user).",
            "get_repack_status()",
        ),
    )
    async def get_repack_status(ctx: _Ctx) -> repack.RepackStatus:
        async def _run() -> repack.RepackStatus:
            return await repack.get_repack_status(_driver(ctx))

        return await _cached_call(ctx, "get_repack_status", _run)


def _register_repack_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="repack_table",
        description=_with_example(
            "Rebuild a table using PG 19's in-server `REPACK` command. "
            "Defaults to `concurrently=true` — the non-blocking variant is "
            "the common ops choice. Cannot run inside a transaction block "
            "(same constraint as `VACUUM`); runs on an autocommit "
            "connection via `Database.run_unmanaged`. Requires PG 19+; "
            "raises an error otherwise (pair with `get_repack_status` to "
            "feature-detect, or fall back to pg_repack on PG ≤ 18). "
            "Returns an object with `schema`, `table`, `concurrently` "
            "(bool), and `repack_sql` (the rendered DDL that actually "
            "executed — same audit-friendly shape as `maintenance_sql` on "
            "`run_maintenance`).",
            "repack_table(schema='public', table='orders', concurrently=True)",
        ),
    )
    async def repack_table(
        ctx: _Ctx,
        schema: str,
        table: str,
        concurrently: bool = True,
    ) -> repack.RepackResult:
        database = ctx.request_context.lifespan_context.database
        result = await repack.repack_table(
            database,
            schema=schema,
            table=table,
            concurrently=concurrently,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_partman(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="partman_create_parent",
        description=(
            "Register a partitioned table with pg_partman. ``partition_type`` "
            "must be 'range', 'list', or 'native'. Performs DDL — requires "
            "unrestricted mode + MCPG_ALLOW_DDL; pg_partman installed. "
            "Returns an object with `parent_table` and `detail` (the partman "
            "registration status string)."
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
            "unrestricted mode + MCPG_ALLOW_DDL. "
            "Returns an object with `parent_table` (the scoped name or '*' for "
            "all) and `detail` (maintenance summary string)."
        ),
    )
    async def partman_run_maintenance(ctx: _Ctx, parent_table: str | None = None) -> partman.PartmanResult:
        result = await partman.partman_run_maintenance(_driver(ctx), parent_table)
        await ctx.request_context.lifespan_context.cache.clear()
        return result

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
            "analyze, or vacuum_analyze). Available only in unrestricted mode. "
            "Returns an object with `operation`, `target` (the qualified table name), "
            "and `maintenance_sql` (the rendered DDL that actually ran)."
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
            "the connection stays open. Available only in unrestricted mode. "
            "Returns an object with `pid`, `action` ('cancel'), and `succeeded` (bool)."
        ),
    )
    async def cancel_query(ctx: _Ctx, pid: int) -> liveops.BackendActionResult:
        return await liveops.cancel_query(_driver(ctx), pid)

    @server.tool(
        name="terminate_backend",
        description=(
            "Terminate a backend PID (pg_terminate_backend), closing its "
            "connection. Available only in unrestricted mode. "
            "Returns an object with `pid`, `action` ('terminate'), and `succeeded` (bool)."
        ),
    )
    async def terminate_backend(ctx: _Ctx, pid: int) -> liveops.BackendActionResult:
        return await liveops.terminate_backend(_driver(ctx), pid)


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
            "only in unrestricted access mode with MCPG_ALLOW_DDL enabled. "
            "Returns an object with `name` and `enabled` (bool)."
        ),
    )
    async def enable_extension(ctx: _Ctx, name: str) -> extensions.EnableExtensionResult:
        result = await extensions.enable_extension(_driver(ctx), name)
        await ctx.request_context.lifespan_context.cache.clear()
        return result


def _register_graphs_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="list_graphs",
        description=(
            "List all active Apache AGE property graphs in the database. "
            "Returns a list of objects with `name` (graph name) and `oid`."
        ),
    )
    async def list_graphs(ctx: _Ctx) -> list[dict[str, Any]]:
        app = ctx.request_context.lifespan_context
        res = await graph.list_graphs(app)
        return [dict(x) for x in res]

    @server.tool(
        name="describe_graph",
        description=(
            "Describe the schema structure, vertex labels, and edge labels of a specific property graph. "
            "Returns an object with `graph_name`, `vertex_labels`, and `edge_labels`."
        ),
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
            "(CREATE, SET, DELETE, MERGE, REMOVE). "
            "Returns an object with `columns` (list of result column names) and "
            "`rows` (list of result rows as dicts keyed by column)."
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
            "relationships in a property graph to visualize its schema and topology. "
            "Returns the Mermaid flowchart as a string."
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
            "Performs DDL — requires DDL permission enabled. "
            "Returns an object with `graph_name` and `created` (bool)."
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
            "edges, and backing tables. Performs DDL — requires DDL permission enabled. "
            "Returns an object with `graph_name` and `dropped` (bool)."
        ),
    )
    async def drop_graph(ctx: _Ctx, graph_name: str, cascade: bool = True) -> dict[str, Any]:
        app = ctx.request_context.lifespan_context
        res = await graph_mgmt.drop_graph(app, graph_name, cascade=cascade)
        await app.cache.clear()
        return dict(res)


def _register_resources(server: FastMCP[AppContext]) -> None:
    """Register the MCP **resources** primitive — `mcpg://…` URIs.

    Resources are MCP's preload-on-connect surface (separate from tools
    and prompts). Agents read by URI rather than calling a tool, which
    skips the tool-call wire overhead and the per-call context-window
    cost. The content payloads come from :mod:`mcpg.resources` — see
    that module's docstring for the surface inventory.
    """
    from mcpg import resources as mcpg_resources

    # ------------------------------------------------------------------
    # mcpg://about/index — full describe_self-equivalent payload.
    # Static (no template variable), no DB access. Reuses the tool
    # surface registered on this server so the bucket counts match
    # the maximal-flag enumeration the same agent would see via
    # `describe_self`.
    # ------------------------------------------------------------------
    @server.resource(
        "mcpg://about/index",
        name="MCPg self-description",
        mime_type="application/json",
        description=(
            "Full capability summary — the same JSON payload `describe_self` returns, "
            "exposed as a resource so an agent can preload it once at session start "
            "without burning a tool call. Includes per-bucket tool lists + headlines. "
            "Drill into a single bucket via `mcpg://capabilities/{bucket_id}`."
        ),
    )
    async def about_index_resource() -> str:
        # ``list_tools`` is async; collect names once and reuse the
        # canonical formatter so the wire shape matches `describe_self`
        # exactly.
        registered_tools = list(await server.list_tools())
        names = [t.name for t in registered_tools]
        return mcpg_resources._build_about_index_payload(names)

    # ------------------------------------------------------------------
    # mcpg://capabilities/index — compact bucket list (id + name + summary).
    # Cheaper than `mcpg://about/index` — meant for "what's the menu"
    # discovery without pulling per-bucket tool lists.
    # ------------------------------------------------------------------
    @server.resource(
        "mcpg://capabilities/index",
        name="MCPg capability buckets (compact)",
        mime_type="application/json",
        description=(
            "Compact list of MCPg's capability buckets — just `id`, `name`, "
            "`summary` per bucket. Cheap enough to pull on every session "
            "start. Drill into a bucket with `mcpg://capabilities/{bucket_id}`."
        ),
    )
    def capabilities_index_resource() -> str:
        return mcpg_resources._build_capabilities_index_payload()

    # ------------------------------------------------------------------
    # mcpg://capabilities/{bucket_id} — per-bucket detail.
    # Same payload shape as the `capabilities[i]` entry from
    # `describe_self`, filtered to one bucket.
    # ------------------------------------------------------------------
    @server.resource(
        "mcpg://capabilities/{bucket_id}",
        name="MCPg capability bucket detail",
        mime_type="application/json",
        description=(
            "Full detail for one capability bucket — `id`, `name`, "
            "`summary`, `detail`, `headline_tools` (top 3-6), "
            "`tool_count`, and `all_tools`. Use after picking a bucket "
            "from `mcpg://capabilities/index`."
        ),
    )
    async def capability_detail_resource(bucket_id: str) -> str:
        registered_tools = list(await server.list_tools())
        names = [t.name for t in registered_tools]
        return mcpg_resources._build_capability_detail_payload(bucket_id, names)

    # ------------------------------------------------------------------
    # mcpg://schema/{schema_name} — compact schema dump as Markdown
    # inside a JSON envelope. Read-only DB access; schema name is
    # parameter-bound inside `get_compact_schema`.
    # ------------------------------------------------------------------
    @server.resource(
        "mcpg://schema/{schema_name}",
        name="PostgreSQL schema (compact)",
        mime_type="application/json",
        description=(
            "Compact Markdown dump of one PostgreSQL schema — tables, "
            "columns, foreign keys. Same content as the `get_compact_schema` "
            "tool, exposed as a resource so an agent can preload schema "
            "context without burning a tool call. JSON envelope "
            "`{schema, format: 'markdown', body}`."
        ),
    )
    async def schema_resource(ctx: _Ctx, schema_name: str) -> str:
        database = ctx.request_context.lifespan_context.database
        return await mcpg_resources._build_schema_payload(database.driver(), schema_name)


def _register_prompts(server: FastMCP[AppContext]) -> None:
    """Register the MCP **prompts** primitive — pre-built investigation playbooks.

    Companion to :func:`_register_resources` (preload context) and
    :func:`register_tools` (operations). Prompts surface as the
    standard ``prompts/list`` + ``prompts/get`` MCP protocol; clients
    render them with arguments the user supplies and inject the
    result into the conversation. Bodies come from :mod:`mcpg.prompts`
    so tests can exercise the templating directly.
    """
    from mcpg import prompts as mcpg_prompts

    # ------------------------------------------------------------------
    # diagnose_slow_query — single-statement investigation flow.
    # Routes the agent through explain_query, analyze_query_plan,
    # recommend_indexes, analyze_workload in order.
    # ------------------------------------------------------------------
    @server.prompt(
        name="diagnose_slow_query",
        title="Diagnose a slow query",
        description=(
            "Deterministic investigation plan for a single slow SQL statement. "
            "Walks the agent through `explain_query`, `analyze_query_plan`, "
            "`recommend_indexes`, and `analyze_workload` in order, with a "
            "structured reporting checklist at the end."
        ),
    )
    def diagnose_slow_query_prompt(sql: str) -> str:
        return mcpg_prompts._build_diagnose_slow_query(sql)

    # ------------------------------------------------------------------
    # bisect_slow_migration — narrows the cause of a migration regression
    # via `compare_schemas` + `list_applied_migrations` + per-query analysis.
    # ------------------------------------------------------------------
    @server.prompt(
        name="bisect_slow_migration",
        title="Bisect a slow migration",
        description=(
            "Investigation plan for a performance regression introduced by a "
            "migration. Confirms the migration ran, scopes what changed via "
            "`compare_schemas`, validates the suspects with per-query "
            "`analyze_query_plan`, and ends with a remediation decision tree."
        ),
    )
    def bisect_slow_migration_prompt(
        migration_id: str,
        baseline_schema: str,
        current_schema: str,
    ) -> str:
        return mcpg_prompts._build_bisect_slow_migration(migration_id, baseline_schema, current_schema)

    # ------------------------------------------------------------------
    # review_rls_policy — RLS coverage audit for one table.
    # Uses `describe_table` + `list_policies` + `audit_database`.
    # ------------------------------------------------------------------
    @server.prompt(
        name="review_rls_policy",
        title="Review row-level security for a table",
        description=(
            "RLS coverage audit for a single table — inventories columns, "
            "reads current policies, identifies gaps against identity-bearing "
            "columns, and cross-checks the cluster security posture. "
            "Diagnosis only — proposes `CREATE POLICY` statements but does "
            "not apply them."
        ),
    )
    def review_rls_policy_prompt(schema: str, table: str) -> str:
        return mcpg_prompts._build_review_rls_policy(schema, table)


def _register_warehousepg_reads(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="get_warehousepg_status",
        description=_with_example(
            "Probe the connected server for the WarehousePG (Greenplum-derived "
            "MPP) signature. Reports `available=true` only when BOTH the "
            "version string mentions WarehousePG / Greenplum AND the "
            "`gp_segment_configuration` catalog view exists. Surfaces "
            "`coordinator_role`, `segment_count` (primary segments only), and "
            "`mirroring` (bool). On vanilla PostgreSQL clusters returns "
            "`available=false` with a clean diagnostic — the rest of the "
            "`mcpg.warehousepg.*` family advertise themselves inert via this "
            "probe. Read-only; never raises.",
            "get_warehousepg_status()",
        ),
    )
    async def get_warehousepg_status(ctx: _Ctx) -> warehousepg.WarehousePGStatus:
        async def _run() -> warehousepg.WarehousePGStatus:
            return await warehousepg.get_warehousepg_status(_driver(ctx))

        return await _cached_call(ctx, "get_warehousepg_status", _run)

    @server.tool(
        name="list_distribution_policies",
        description=_with_example(
            "List the data-distribution policy for each table in a WarehousePG "
            "schema (`HASH(col,...)`, `RANDOM`, or `REPLICATED`). Joins "
            "`gp_distribution_policy` to `pg_attribute` for the distribution-"
            "key column names in catalog order. `schema=None` returns every "
            "non-system schema. Read-only. On vanilla PG returns "
            "`available=false` with a diagnostic.",
            "list_distribution_policies(schema='public')",
        ),
    )
    async def list_distribution_policies(ctx: _Ctx, schema: str | None = None) -> warehousepg.DistributionPolicyReport:
        async def _run() -> warehousepg.DistributionPolicyReport:
            return await warehousepg.list_distribution_policies(_driver(ctx), schema=schema)

        return await _cached_call(ctx, "list_distribution_policies", _run, schema)

    @server.tool(
        name="check_segment_health",
        description=_with_example(
            "Walk `gp_segment_configuration` and surface MPP segment posture. "
            "Returns per-segment `status` ('u' = up), `mode` ('s' = sync / "
            "'n' = not-in-sync / 'c' = changetracking), and `role` vs "
            "`preferred_role` to detect post-failover state. Top-level "
            "`unhealthy_count` + `out_of_sync_count` let agents branch "
            "without walking the segments array. Read-only. On vanilla PG "
            "returns `available=false`.",
            "check_segment_health()",
        ),
    )
    async def check_segment_health(ctx: _Ctx) -> warehousepg.SegmentHealthReport:
        async def _run() -> warehousepg.SegmentHealthReport:
            return await warehousepg.check_segment_health(_driver(ctx))

        return await _cached_call(ctx, "check_segment_health", _run)

    @server.tool(
        name="describe_ao_table",
        description=_with_example(
            "Describe append-optimized (AO) / append-optimized columnar "
            "(AO/CO) storage metadata for one table: row vs column orientation, "
            "`compression_type`, `compression_level`, `block_size`, `checksum`. "
            "Reads `pg_appendonly`. Returns `is_ao=false` cleanly when the "
            "table is a regular heap. Read-only. On vanilla PG returns "
            "`available=false`.",
            "describe_ao_table(schema='public', table='events_ao')",
        ),
    )
    async def describe_ao_table(ctx: _Ctx, schema: str, table: str) -> warehousepg.AppendOptimizedTableInfo:
        async def _run() -> warehousepg.AppendOptimizedTableInfo:
            return await warehousepg.describe_ao_table(_driver(ctx), schema, table)

        return await _cached_call(ctx, "describe_ao_table", _run, schema, table)

    @server.tool(
        name="list_resource_groups",
        description=_with_example(
            "List configured WarehousePG resource groups + their utilisation. "
            "Reads `gp_toolkit.gp_resgroup_status` for `concurrency`, "
            "`cpu_max_percent`, `cpu_weight`, `memory_limit`, "
            "`memory_shared_quota`, plus live `num_running` / `num_queueing`. "
            "Pairs with `analyze_workload` for 'where's my workload time "
            "going' diagnosis. Read-only. On vanilla PG returns "
            "`available=false`.",
            "list_resource_groups()",
        ),
    )
    async def list_resource_groups(ctx: _Ctx) -> warehousepg.ResourceGroupReport:
        async def _run() -> warehousepg.ResourceGroupReport:
            return await warehousepg.list_resource_groups(_driver(ctx))

        return await _cached_call(ctx, "list_resource_groups", _run)

    @server.tool(
        name="analyze_mpp_query_plan",
        description=_with_example(
            "Run `EXPLAIN (ANALYZE, FORMAT JSON)` on `sql` and roll up "
            "MPP-specific facts: slice count, motion nodes (`Redistribute "
            "Motion` / `Broadcast Motion` / `Gather Motion`), and per-motion "
            "metadata (senders, receivers, estimated rows). Uses the same "
            "safety pre-flight as `analyze_query_plan(io=True)` — writes / "
            "DDL stay rejected. `redistribute_count` flags 'data is not "
            "co-located with the join key'. On vanilla PG returns "
            "`available=false`.",
            "analyze_mpp_query_plan(sql='SELECT * FROM big JOIN small ON big.k = small.k')",
        ),
    )
    async def analyze_mpp_query_plan(ctx: _Ctx, sql: str) -> warehousepg.MppQueryPlanAnalysis:
        # Deliberately NOT cached — matches the convention for
        # analyze_query_plan / explain_query: each EXPLAIN ANALYZE
        # call reflects the planner state + actual buffer cache at
        # invocation time. Caching by SQL text would hide regressions
        # the agent is calling this tool to detect.
        return await warehousepg.analyze_mpp_query_plan(_driver(ctx), sql)

    @server.tool(
        name="recommend_redistribute",
        description=_with_example(
            "Distribution-skew advisor for a hash-distributed table. Reads "
            "`pg_stats.n_distinct` for every column on the table and suggests "
            "a better hash key when the current one is low-cardinality. Pure "
            "catalog read — no per-segment scans. Returns ranked "
            "`candidates`, optional `recommendation`, and ready-to-review "
            "`suggested_ddl` (`ALTER TABLE … SET WITH (REORGANIZE=TRUE) "
            "DISTRIBUTED BY (col)`). Diagnosis-only — never executes. On "
            "vanilla PG returns `available=false`.",
            "recommend_redistribute(schema='public', table='fact_sales')",
        ),
    )
    async def recommend_redistribute(ctx: _Ctx, schema: str, table: str) -> warehousepg.RedistributeRecommendation:
        async def _run() -> warehousepg.RedistributeRecommendation:
            return await warehousepg.recommend_redistribute(_driver(ctx), schema, table)

        return await _cached_call(ctx, "recommend_redistribute", _run, schema, table)


def _register_logical_replication_writes(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="create_publication",
        description=_with_example(
            "Create a logical-replication publication. Exactly one of "
            "`all_tables=True` or a non-empty `tables` tuple must be supplied. "
            "Each table in `tables` must be a `schema.table`-qualified name; "
            "both pieces are identifier-validated and quoted. Returns the "
            "rendered SQL for audit. Requires `MCPG_ALLOW_DDL`.",
            "create_publication(name='sales_pub', tables=('public.orders', 'public.line_items'))",
        ),
    )
    async def create_publication(
        ctx: _Ctx,
        name: str,
        all_tables: bool = False,
        tables: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        result = await logical_replication.create_publication(
            ctx.request_context.lifespan_context.database,
            name=name,
            all_tables=all_tables,
            tables=tables,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="drop_publication",
        description=_with_example(
            "Drop a logical-replication publication. `if_exists=True` "
            "suppresses 'does not exist' errors; `cascade=True` lets the "
            "drop cascade through dependent objects. Identifier-validated. "
            "Requires `MCPG_ALLOW_DDL`.",
            "drop_publication(name='sales_pub', if_exists=True)",
        ),
    )
    async def drop_publication(
        ctx: _Ctx,
        name: str,
        if_exists: bool = False,
        cascade: bool = False,
    ) -> dict[str, Any]:
        result = await logical_replication.drop_publication(
            ctx.request_context.lifespan_context.database,
            name=name,
            if_exists=if_exists,
            cascade=cascade,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="create_subscription",
        description=_with_example(
            "Create a logical-replication subscription to one or more "
            "publications on a publisher cluster. `connection_string` is a "
            "libpq DSN; it's NOT echoed back in the result (may contain "
            "credentials). Options: `enabled` (start immediately), "
            "`copy_data` (initial sync), `create_slot` (let subscriber "
            "create the upstream slot), `slot_name` (override default), "
            "`synchronous_commit` (one of 'on'/'off'/'local'/'remote_write'/"
            "'remote_apply'). Requires `MCPG_ALLOW_DDL`.",
            "create_subscription(name='sales_sub', "
            "connection_string='host=publisher dbname=app user=replicator', "
            "publications=('sales_pub',))",
        ),
    )
    async def create_subscription(
        ctx: _Ctx,
        name: str,
        connection_string: str,
        publications: tuple[str, ...],
        enabled: bool = True,
        copy_data: bool = True,
        create_slot: bool = True,
        slot_name: str | None = None,
        synchronous_commit: str | None = None,
    ) -> dict[str, Any]:
        result = await logical_replication.create_subscription(
            ctx.request_context.lifespan_context.database,
            name=name,
            connection_string=connection_string,
            publications=publications,
            enabled=enabled,
            copy_data=copy_data,
            create_slot=create_slot,
            slot_name=slot_name,
            synchronous_commit=synchronous_commit,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)

    @server.tool(
        name="drop_subscription",
        description=_with_example(
            "Drop a logical-replication subscription. PostgreSQL requires "
            "the subscription be disabled first (or its slot dropped on the "
            "publisher); this tool does NOT auto-disable — pair with "
            "`run_ddl` for `ALTER SUBSCRIPTION ... DISABLE` when needed. "
            "Identifier-validated. Requires `MCPG_ALLOW_DDL`.",
            "drop_subscription(name='sales_sub', if_exists=True)",
        ),
    )
    async def drop_subscription(
        ctx: _Ctx,
        name: str,
        if_exists: bool = False,
    ) -> dict[str, Any]:
        result = await logical_replication.drop_subscription(
            ctx.request_context.lifespan_context.database,
            name=name,
            if_exists=if_exists,
        )
        await ctx.request_context.lifespan_context.cache.clear()
        return asdict(result)


def register_tools(server: FastMCP[AppContext], settings: Settings) -> None:
    """Register the MCP tools permitted by the configured access mode.

    ``get_server_info`` is always available. Read tools (introspection,
    queries) are exposed whenever the READ capability is permitted, which is
    every mode. Write tools require the WRITE capability — unrestricted mode.
    The DDL tool additionally requires the ``MCPG_ALLOW_DDL`` opt-in.

    MCP resources (the ``mcpg://…`` preload surface) are registered
    alongside READ tools — they're conceptually a read-shape operation
    and they share the same gate.
    """
    _register_server_info(server)
    if is_permitted(settings.access_mode, Capability.READ):
        _register_resources(server)
        _register_prompts(server)
        _register_introspection(server)
        _register_diagrams(server)
        _register_schema_diff(server)
        _register_vector_tuning(server)
        _register_rag_efficiency(server)
        _register_rag_analytics(server)
        _register_rag_telemetry_efficiency_read(server)
        _register_prisma(server)
        _register_advisors(server)
        _register_composite(server)
        _register_data_movement(server)
        _register_audit_trail(server)
        _register_query(server)
        _register_health(server)
        _register_liveops(server)
        _register_turboquant_reads(server)
        _register_pg_search_reads(server)
        _register_timescaledb_reads(server)
        _register_graphs_reads(server)
        _register_redis_fdw_reads(server)
        _register_pg_prewarm_reads(server)
        _register_pgq_reads(server)
        _register_repack_reads(server)
        _register_aio_reads(server)
        _register_pg19_stats_reads(server)
        _register_pg19_runtime_reads(server)
        _register_pg19_ddl_reads(server)
        _register_pg19_partitions_reads(server)
        _register_pg19_skip_scan_reads(server)
        _register_wait_for_lsn_reads(server)
        _register_wait_for_lsn_writes(server)
        _register_warehousepg_reads(server)
    if is_permitted(settings.access_mode, Capability.WRITE):
        _register_write(server)
        _register_maintenance(server)
        _register_backend_control(server)
        _register_cron_write(server)
        _register_turboquant_writes(server)
        _register_rag_telemetry_write(server)
        _register_data_movement_writes(server)
        _register_pg_prewarm_writes(server)
        _register_repack_writes(server)
        _register_pg19_runtime_writes(server)
    if is_permitted(settings.access_mode, Capability.DDL) and settings.allow_ddl:
        _register_ddl(server)
        _register_logical_replication_writes(server)
        _register_partman(server)
        _register_pg19_ddl_writes(server)
        _register_pg19_partitions_writes(server)
        _register_turboquant_ddl(server)
        _register_pg_search_ddl(server)
        _register_rag_telemetry_ddl(server)
        _register_timescaledb_writes(server)
        _register_graphs_writes(server)
        _register_redis_fdw_ddl(server)
        _register_pgq_ddl(server)
    if is_permitted(settings.access_mode, Capability.MIGRATE) and settings.allow_ddl:
        _register_migrations(server)
    if is_permitted(settings.access_mode, Capability.SHELL) and settings.allow_shell:
        _register_data_movement_shell(server)
    if is_permitted(settings.access_mode, Capability.LISTEN) and settings.allow_listen:
        _register_listen(server)

    # Session-intent surface filter (roadmap 8.8). Runs LAST so every
    # tool that would otherwise be registered has already been added —
    # we then remove whatever the configured intent doesn't allow.
    # describe_self / describe_tool are always kept (see session_intent).
    if settings.session_intent:
        from mcpg.session_intent import filter_server_tools, resolve_intent_to_buckets

        allowed = resolve_intent_to_buckets(settings.session_intent)
        if allowed is not None:
            filter_server_tools(server, allowed)
