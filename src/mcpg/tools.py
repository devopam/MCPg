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
    composite,
    cron,
    data_movement,
    diagrams,
    diesel,
    drizzle,
    ecto,
    ent,
    extensions,
    health,
    indexing,
    introspection,
    jooq,
    liveops,
    maintenance,
    migrations,
    partman,
    prisma,
    query,
    schema_diff,
    sqlalchemy_export,
    sqlc,
    textsearch,
    timescaledb,
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
        report = await advisors.run_advisors(_driver(ctx), schema)
        return asdict(report)

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
        report = await advisors.find_unused_objects(_driver(ctx), schema)
        return asdict(report)


def _register_composite(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="summarize_table",
        description=(
            "Return a one-stop snapshot of a table: columns, primary key, "
            "foreign keys, every other constraint, indexes, storage + "
            "row-count + last-vacuum/analyze stats, and (optionally) a "
            "short sample of rows. Replaces what would otherwise be 4-5 "
            "individual tool calls. Set sample_rows=0 on wide / jsonb-"
            "heavy tables where the sample isn't useful."
        ),
    )
    async def summarize_table(ctx: _Ctx, schema: str, table: str, sample_rows: int = 5) -> dict[str, Any]:
        result = await composite.summarize_table(_driver(ctx), schema, table, sample_rows=sample_rows)
        return asdict(result)

    @server.tool(
        name="why_is_this_slow",
        description=(
            "Diagnose why a SQL query might be slow, in one call. Runs "
            "EXPLAIN (FORMAT JSON) — does NOT execute the query — walks the "
            "plan tree, snapshots concurrent active queries + blocking "
            "lock pairs, reads the cluster-wide cache hit ratio, and "
            "produces categorised suggestions (plan / contention / cache / "
            "maintenance). Read-only; safe to run on a statement the agent "
            "doesn't want to materialise yet."
        ),
    )
    async def why_is_this_slow(ctx: _Ctx, sql: str) -> dict[str, Any]:
        result = await composite.why_is_this_slow(_driver(ctx), sql)
        return asdict(result)


def _register_data_movement(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="export_query",
        description=(
            "Run a read-only SQL query and serialise its rows to CSV or JSON. "
            "Reuses the SQL-safety checks of run_select. Truncates at `limit` "
            "rows and flags it in the result so callers can paginate."
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
        return asdict(result)


def _register_migrations(server: FastMCP[AppContext]) -> None:
    @server.tool(
        name="prepare_migration",
        description=(
            "Stage a candidate migration against a shadow clone of `target_schema`. "
            "Replicates the target schema's structure into mcpg_shadow_<id>, applies "
            "`candidate_sql` there, then runs compare_schemas(target, shadow) so the "
            "agent can review the structural diff before completing. Returns the "
            "migration id, shadow schema name, TTL, and the diff. Performs DDL — "
            "requires unrestricted mode + MCPG_ALLOW_DDL."
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
        )
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
        )
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
        name="hybrid_search",
        description=(
            "Combine vector and full-text ranking via reciprocal-rank fusion "
            "(RRF) — pulls candidates from each source, then fuses them so "
            "rows ranked highly in EITHER source surface. Closes the gap "
            "between pure vector (misses keyword/identifier matches) and "
            "pure full-text (misses semantic synonyms). Parameters: "
            "vector_column, text_column, query_vector, text_query, plus "
            "metric / text_config / limit / candidate_pool / rrf_k tunables. "
            "Each match carries vector_rank, fts_rank, the fused rrf_score, "
            "and (when present) the original distance + ts_rank values."
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
        _register_composite(server)
        _register_data_movement(server)
        _register_audit_trail(server)
        _register_query(server)
        _register_health(server)
        _register_liveops(server)
        _register_timescaledb_reads(server)
    if is_permitted(settings.access_mode, Capability.WRITE):
        _register_write(server)
        _register_maintenance(server)
        _register_backend_control(server)
        _register_cron_write(server)
        _register_data_movement_writes(server)
    if is_permitted(settings.access_mode, Capability.DDL) and settings.allow_ddl:
        _register_ddl(server)
        _register_partman(server)
        _register_timescaledb_writes(server)
    if is_permitted(settings.access_mode, Capability.MIGRATE) and settings.allow_ddl:
        _register_migrations(server)
    if is_permitted(settings.access_mode, Capability.SHELL) and settings.allow_shell:
        _register_data_movement_shell(server)
    if is_permitted(settings.access_mode, Capability.LISTEN) and settings.allow_listen:
        _register_listen(server)
