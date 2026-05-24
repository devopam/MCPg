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
    advisors,
    audit_trail,
    cron,
    diagrams,
    extensions,
    health,
    indexing,
    introspection,
    liveops,
    maintenance,
    partman,
    prisma,
    query,
    schema_diff,
    textsearch,
    vector_tuning,
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

    @server.tool(
        name="list_tables",
        description="List the tables and views in a schema, flagging partitioned tables and partitions.",
    )
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

    @server.tool(
        name="list_constraints",
        description="List a table's constraints — primary/foreign keys, unique, check, exclusion.",
    )
    async def list_constraints(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        constraints = await introspection.list_constraints(_driver(ctx), schema, table)
        return [asdict(constraint) for constraint in constraints]

    @server.tool(
        name="list_foreign_keys",
        description="List foreign keys in a schema, resolved to columns and referenced table.",
    )
    async def list_foreign_keys(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        fks = await introspection.list_foreign_keys(_driver(ctx), schema)
        return [asdict(fk) for fk in fks]

    @server.tool(
        name="list_views",
        description="List the views and materialized views in a schema, with their definitions.",
    )
    async def list_views(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        views = await introspection.list_views(_driver(ctx), schema)
        return [asdict(view) for view in views]

    @server.tool(
        name="list_functions",
        description="List the functions and procedures defined in a schema.",
    )
    async def list_functions(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        functions = await introspection.list_functions(_driver(ctx), schema)
        return [asdict(function) for function in functions]

    @server.tool(
        name="list_triggers",
        description="List the user-defined triggers on a table.",
    )
    async def list_triggers(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        triggers = await introspection.list_triggers(_driver(ctx), schema, table)
        return [asdict(trigger) for trigger in triggers]

    @server.tool(
        name="list_partitions",
        description="Describe how a table is partitioned (strategy and bounds) and list its partitions.",
    )
    async def list_partitions(ctx: _Ctx, schema: str, table: str) -> dict[str, Any]:
        partition_set = await introspection.list_partitions(_driver(ctx), schema, table)
        return asdict(partition_set)

    @server.tool(
        name="list_roles",
        description="List the database roles and their attributes, excluding PostgreSQL's own roles "
        "unless include_system is true.",
    )
    async def list_roles(ctx: _Ctx, include_system: bool = False) -> list[dict[str, Any]]:
        roles = await introspection.list_roles(_driver(ctx), include_system=include_system)
        return [asdict(role) for role in roles]

    @server.tool(
        name="list_grants",
        description="List the privileges granted on a table — who may do what to it.",
    )
    async def list_grants(ctx: _Ctx, schema: str, table: str) -> list[dict[str, Any]]:
        grants = await introspection.list_grants(_driver(ctx), schema, table)
        return [asdict(grant) for grant in grants]

    @server.tool(
        name="list_policies",
        description="List the Row-Level-Security policies on a table, and whether row security is enabled.",
    )
    async def list_policies(ctx: _Ctx, schema: str, table: str) -> dict[str, Any]:
        policy_set = await introspection.list_policies(_driver(ctx), schema, table)
        return asdict(policy_set)

    @server.tool(
        name="list_sequences",
        description="List the sequences defined in a schema, with their range, increment, and last value.",
    )
    async def list_sequences(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        sequences = await introspection.list_sequences(_driver(ctx), schema)
        return [asdict(sequence) for sequence in sequences]

    @server.tool(
        name="list_enums",
        description="List the enum types in a schema, with their labels in sort order.",
    )
    async def list_enums(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        enums = await introspection.list_enums(_driver(ctx), schema)
        return [asdict(enum) for enum in enums]

    @server.tool(
        name="list_domains",
        description="List the domain types in a schema, with base type, default, and check constraints.",
    )
    async def list_domains(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        domains = await introspection.list_domains(_driver(ctx), schema)
        return [asdict(domain) for domain in domains]

    @server.tool(
        name="list_composite_types",
        description="List the standalone composite types in a schema with their attributes.",
    )
    async def list_composite_types(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        types = await introspection.list_composite_types(_driver(ctx), schema)
        return [asdict(t) for t in types]

    @server.tool(
        name="list_foreign_data_wrappers",
        description="List the foreign-data wrappers installed in the database.",
    )
    async def list_foreign_data_wrappers(ctx: _Ctx) -> list[dict[str, Any]]:
        wrappers = await introspection.list_foreign_data_wrappers(_driver(ctx))
        return [asdict(wrapper) for wrapper in wrappers]

    @server.tool(
        name="list_foreign_servers",
        description="List the foreign servers defined in the database, with their FDW and options.",
    )
    async def list_foreign_servers(ctx: _Ctx) -> list[dict[str, Any]]:
        servers = await introspection.list_foreign_servers(_driver(ctx))
        return [asdict(server_info) for server_info in servers]

    @server.tool(
        name="list_foreign_tables",
        description="List the foreign tables in a schema, with their server and options.",
    )
    async def list_foreign_tables(ctx: _Ctx, schema: str) -> list[dict[str, Any]]:
        tables = await introspection.list_foreign_tables(_driver(ctx), schema)
        return [asdict(table) for table in tables]

    @server.tool(
        name="list_user_mappings",
        description="List role-to-foreign-server mappings; the catch-all appears as user='public'.",
    )
    async def list_user_mappings(ctx: _Ctx) -> list[dict[str, Any]]:
        mappings = await introspection.list_user_mappings(_driver(ctx))
        return [asdict(mapping) for mapping in mappings]

    @server.tool(
        name="list_publications",
        description="List logical-replication publications with the tables and operations they include.",
    )
    async def list_publications(ctx: _Ctx) -> list[dict[str, Any]]:
        publications = await introspection.list_publications(_driver(ctx))
        return [asdict(publication) for publication in publications]

    @server.tool(
        name="list_subscriptions",
        description="List logical-replication subscriptions; requires superuser to see any rows.",
    )
    async def list_subscriptions(ctx: _Ctx) -> list[dict[str, Any]]:
        subscriptions = await introspection.list_subscriptions(_driver(ctx))
        return [asdict(subscription) for subscription in subscriptions]

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


def _register_diagrams(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="generate_schema_diagram",
        description=(
            "Render a Mermaid ER diagram for a schema. Views and foreign tables are "
            "excluded; partitions are excluded by default — pass include_partitions=true "
            "to draw each partition as its own entity."
        ),
    )
    async def generate_schema_diagram(ctx: _Ctx, schema: str, include_partitions: bool = False) -> str:
        return await diagrams.generate_schema_diagram(_driver(ctx), schema, include_partitions=include_partitions)


def _register_schema_diff(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="compare_schemas",
        description=(
            "Return the structural diff between two schemas — tables/columns/"
            "indexes/constraints/foreign-keys added, removed, or changed. "
            "Base tables only; views and custom types are not compared. "
            "Renames surface as a paired add + remove."
        ),
    )
    async def compare_schemas(ctx: _Ctx, left_schema: str, right_schema: str) -> dict[str, Any]:
        diff = await schema_diff.compare_schemas(_driver(ctx), left_schema, right_schema)
        return asdict(diff)


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
        report = await advisors.run_advisors(_driver(ctx), schema)
        return asdict(report)


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
        name="list_cron_jobs",
        description=(
            "List the pg_cron jobs registered in the database. Returns an empty list when pg_cron is not installed."
        ),
    )
    async def list_cron_jobs(ctx: _Ctx) -> list[dict[str, Any]]:
        jobs = await cron.list_cron_jobs(_driver(ctx))
        return [asdict(job) for job in jobs]


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
        return {"name": name, "removed": removed}


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
        _register_diagrams(server)
        _register_schema_diff(server)
        _register_vector_tuning(server)
        _register_prisma(server)
        _register_advisors(server)
        _register_audit_trail(server)
        _register_query(server)
        _register_health(server)
        _register_liveops(server)
    if is_permitted(settings.access_mode, Capability.WRITE):
        _register_write(server)
        _register_maintenance(server)
        _register_backend_control(server)
        _register_cron_write(server)
    if is_permitted(settings.access_mode, Capability.DDL) and settings.allow_ddl:
        _register_ddl(server)
        _register_partman(server)
