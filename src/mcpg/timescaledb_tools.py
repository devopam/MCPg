"""MCP tool registration for the extended TimescaleDB tool family.

Kept out of :mod:`mcpg.tools` (like :mod:`mcpg.guc_tools`) so the large tools
module only needs three call sites. Three tiers, three gates:

* **reads** — READ gate: ``get_hypertable_stats``, ``list_timescale_jobs``,
  ``list_continuous_aggregates``, ``get_timescaledb_telemetry_report``.
* **additive DDL** — unrestricted mode + ``MCPG_ALLOW_DDL`` (same as
  ``create_hypertable``): dimensions, chunk interval, policies.
* **admin** — additionally ``MCPG_ALLOW_TIMESCALEDB_ADMIN``: destructive or
  heavy operations behind a ``dry_run`` / ``confirm`` + ``confirmation_token``
  flow. Design: ``docs/plans/timescaledb-tools.md``.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from mcpg import timescaledb, timescaledb_admin
from mcpg.context import AppContext
from mcpg.sql import SqlDriver

_Ctx = Context[AppContext, Any]

_DB_DESC = (
    "Optional: target a configured secondary (read-only) database by name; "
    "omit for the primary. Call list_databases to see the configured ids."
)
_DatabaseArg = Annotated[str | None, Field(description=_DB_DESC)]

_CONFIRM_FLOW = (
    "Destructive/heavy: call with dry_run=true first (returns the plan, affected objects and a "
    "confirmation_token); show the warning to the user; then call with confirm=true and that exact "
    "confirmation_token to execute. Requires unrestricted mode + MCPG_ALLOW_DDL + MCPG_ALLOW_TIMESCALEDB_ADMIN."
)


def _driver(ctx: _Ctx, database: str | None = None) -> SqlDriver:
    return ctx.request_context.lifespan_context.database.driver(database)


def register_timescaledb_reads(server: MCPServer[AppContext]) -> None:
    """Read-only introspection tools (READ gate)."""

    @server.tool(
        name="get_hypertable_stats",
        description=(
            "Size and compression statistics for one TimescaleDB hypertable: total/compressed chunk "
            "counts, approximate row count, table/index/toast/total bytes, and bytes before/after "
            "compression. Errors if the table is not a hypertable; available=false when the "
            "timescaledb extension is not installed."
        ),
    )
    async def get_hypertable_stats(
        ctx: _Ctx, schema: str, table: str, database: _DatabaseArg = None
    ) -> timescaledb.HypertableStats:
        return await timescaledb.get_hypertable_stats(_driver(ctx, database), schema, table)

    @server.tool(
        name="list_timescale_jobs",
        description=(
            "List TimescaleDB background jobs (retention / compression / refresh / reorder policies "
            "and user jobs) with schedule, next start, last run status and run/failure counts. "
            "Job ids below 1000 are TimescaleDB system jobs."
        ),
    )
    async def list_timescale_jobs(ctx: _Ctx, database: _DatabaseArg = None) -> timescaledb.JobListResult:
        return await timescaledb.list_jobs(_driver(ctx, database))

    @server.tool(
        name="list_continuous_aggregates",
        description=(
            "List TimescaleDB continuous aggregates with their source hypertable, "
            "materialized_only flag and compression flag."
        ),
    )
    async def list_continuous_aggregates(
        ctx: _Ctx, database: _DatabaseArg = None
    ) -> timescaledb.ContinuousAggregateListResult:
        return await timescaledb.list_continuous_aggregates(_driver(ctx, database))

    @server.tool(
        name="get_timescaledb_telemetry_report",
        description=(
            "Return the usage report TimescaleDB builds about this database (get_telemetry_report()): "
            "versions, relation counts and sizes, feature usage. It is generated locally from catalog "
            "data and nothing is sent by this call (the periodic upload is a separate background job "
            "governed by timescaledb.telemetry_level). Contains instance metadata such as the database "
            "UUID, so treat it as sensitive."
        ),
    )
    async def get_timescaledb_telemetry_report(ctx: _Ctx, database: _DatabaseArg = None) -> timescaledb.TelemetryReport:
        return await timescaledb.get_telemetry_report(_driver(ctx, database))


def register_timescaledb_ddl(server: MCPServer[AppContext]) -> None:
    """Additive / reversible tools (DDL gate)."""

    async def _clear(ctx: _Ctx) -> None:
        await ctx.request_context.lifespan_context.cache.clear()

    @server.tool(
        name="add_dimension",
        description=(
            "Add a dimension to a TimescaleDB hypertable: pass number_partitions for a hash (space) "
            "dimension, or chunk_time_interval (e.g. '1 day') for an additional range dimension - exactly "
            "one. Requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def add_dimension(
        ctx: _Ctx,
        schema: str,
        table: str,
        column: str,
        number_partitions: int | None = None,
        chunk_time_interval: str | None = None,
        *,
        if_not_exists: bool = True,
    ) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.add_dimension(
            _driver(ctx),
            schema,
            table,
            column,
            number_partitions=number_partitions,
            chunk_time_interval=chunk_time_interval,
            if_not_exists=if_not_exists,
        )
        await _clear(ctx)
        return result

    @server.tool(
        name="set_chunk_time_interval",
        description=(
            "Change a hypertable's chunk interval (e.g. '1 day'). Applies only to chunks created from "
            "now on; existing chunks are unchanged. Requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def set_chunk_time_interval(
        ctx: _Ctx, schema: str, table: str, chunk_time_interval: str
    ) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.set_chunk_time_interval(_driver(ctx), schema, table, chunk_time_interval)
        await _clear(ctx)
        return result

    @server.tool(
        name="add_continuous_aggregate_policy",
        description=(
            "Schedule automatic refresh of a continuous aggregate. start_offset / end_offset are intervals "
            "back from now (e.g. '3 days', '1 hour'; null = unbounded on that side); schedule_interval is how "
            "often it runs. Requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def add_continuous_aggregate_policy(
        ctx: _Ctx,
        schema: str,
        view: str,
        schedule_interval: str,
        start_offset: str | None = None,
        end_offset: str | None = None,
        *,
        if_not_exists: bool = True,
    ) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.add_continuous_aggregate_policy(
            _driver(ctx),
            schema,
            view,
            start_offset=start_offset,
            end_offset=end_offset,
            schedule_interval=schedule_interval,
            if_not_exists=if_not_exists,
        )
        await _clear(ctx)
        return result

    @server.tool(
        name="add_reorder_policy",
        description=(
            "Schedule background reordering of a hypertable's chunks along an existing index. "
            "Requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def add_reorder_policy(
        ctx: _Ctx, schema: str, table: str, index_name: str, *, if_not_exists: bool = True
    ) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.add_reorder_policy(
            _driver(ctx), schema, table, index_name, if_not_exists=if_not_exists
        )
        await _clear(ctx)
        return result

    @server.tool(
        name="remove_retention_policy",
        description=(
            "Remove a hypertable's retention policy (no-op if none). Requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def remove_retention_policy(ctx: _Ctx, schema: str, table: str) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.remove_retention_policy(_driver(ctx), schema, table)
        await _clear(ctx)
        return result

    @server.tool(
        name="remove_compression_policy",
        description=(
            "Remove a hypertable's compression policy (no-op if none). Already-compressed chunks stay "
            "compressed. Requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def remove_compression_policy(ctx: _Ctx, schema: str, table: str) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.remove_compression_policy(_driver(ctx), schema, table)
        await _clear(ctx)
        return result

    @server.tool(
        name="remove_continuous_aggregate_policy",
        description=(
            "Remove a continuous aggregate's refresh policy (no-op if none). Requires unrestricted mode "
            "+ MCPG_ALLOW_DDL."
        ),
    )
    async def remove_continuous_aggregate_policy(ctx: _Ctx, schema: str, view: str) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.remove_continuous_aggregate_policy(_driver(ctx), schema, view)
        await _clear(ctx)
        return result

    @server.tool(
        name="remove_reorder_policy",
        description=(
            "Remove a hypertable's reorder policy (no-op if none). Requires unrestricted mode + MCPG_ALLOW_DDL."
        ),
    )
    async def remove_reorder_policy(ctx: _Ctx, schema: str, table: str) -> timescaledb.TimescaleWriteResult:
        result = await timescaledb.remove_reorder_policy(_driver(ctx), schema, table)
        await _clear(ctx)
        return result


def register_timescaledb_admin(server: MCPServer[AppContext]) -> None:  # noqa: C901
    """Destructive / heavy tools (DDL gate + MCPG_ALLOW_TIMESCALEDB_ADMIN, confirm flow)."""

    def _untenanted(ctx: _Ctx) -> None:
        s = ctx.request_context.lifespan_context.settings
        timescaledb_admin.require_untenanted(s.default_role, s.allowed_roles)

    def _timeout(ctx: _Ctx) -> float:
        return ctx.request_context.lifespan_context.settings.statement_timeout_ms / 1000

    async def _clear(ctx: _Ctx, result: timescaledb_admin.TimescaleAdminResult) -> None:
        if result.status == "applied":
            await ctx.request_context.lifespan_context.cache.clear()

    @server.tool(
        name="drop_chunks",
        description=(
            "Permanently delete whole chunks of a hypertable by age: older_than / newer_than (intervals "
            "such as '30 days', relative to now) or created_before / created_after (chunk creation time; "
            "cannot be combined with the others). At least one bound is required. dry_run lists the chunks "
            "that would be dropped. DATA LOSS. " + _CONFIRM_FLOW
        ),
    )
    async def drop_chunks(
        ctx: _Ctx,
        schema: str,
        table: str,
        older_than: str | None = None,
        newer_than: str | None = None,
        created_before: str | None = None,
        created_after: str | None = None,
        confirmation_token: str | None = None,
        *,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        result = await timescaledb_admin.drop_chunks(
            _driver(ctx),
            schema,
            table,
            older_than=older_than,
            newer_than=newer_than,
            created_before=created_before,
            created_after=created_after,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
        )
        await _clear(ctx, result)
        return result

    @server.tool(
        name="compress_chunk",
        description=(
            "Compress one chunk now (chunk_schema is usually _timescaledb_internal; find names with "
            "list_chunks). Rewrites the chunk and locks it. " + _CONFIRM_FLOW
        ),
    )
    async def compress_chunk(
        ctx: _Ctx,
        chunk_schema: str,
        chunk_name: str,
        confirmation_token: str | None = None,
        *,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        result = await timescaledb_admin.compress_chunk(
            _driver(ctx),
            chunk_schema,
            chunk_name,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
        )
        await _clear(ctx, result)
        return result

    @server.tool(
        name="decompress_chunk",
        description=(
            "Decompress one chunk (needs free disk for the uncompressed data; locks the chunk). " + _CONFIRM_FLOW
        ),
    )
    async def decompress_chunk(
        ctx: _Ctx,
        chunk_schema: str,
        chunk_name: str,
        confirmation_token: str | None = None,
        *,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        result = await timescaledb_admin.decompress_chunk(
            _driver(ctx),
            chunk_schema,
            chunk_name,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
        )
        await _clear(ctx, result)
        return result

    @server.tool(
        name="reorder_chunk",
        description=(
            "Physically reorder one chunk along an index. Cannot run in a transaction, so it runs on an "
            "autocommit connection (not rolled back on failure) and is unavailable when "
            "MCPG_DEFAULT_ROLE / MCPG_ALLOWED_ROLES is set. " + _CONFIRM_FLOW
        ),
    )
    async def reorder_chunk(
        ctx: _Ctx,
        chunk_schema: str,
        chunk_name: str,
        index_schema: str,
        index_name: str,
        confirmation_token: str | None = None,
        *,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        _untenanted(ctx)
        app = ctx.request_context.lifespan_context
        result = await timescaledb_admin.reorder_chunk(
            app.database,
            app.database.driver(),
            chunk_schema,
            chunk_name,
            index_schema,
            index_name,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
            timeout_s=_timeout(ctx),
        )
        await _clear(ctx, result)
        return result

    @server.tool(
        name="merge_chunks",
        description=(
            "Merge two or more adjacent, uncompressed chunks (all in chunk_schema) into one. "
            "concurrently=true uses merge_chunks_concurrently (lighter locks; runs on an autocommit "
            "connection and is unavailable when MCPG_DEFAULT_ROLE / MCPG_ALLOWED_ROLES is set). " + _CONFIRM_FLOW
        ),
    )
    async def merge_chunks(
        ctx: _Ctx,
        chunk_schema: str,
        chunk_names: list[str],
        confirmation_token: str | None = None,
        *,
        concurrently: bool = False,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        if concurrently:
            _untenanted(ctx)
        app = ctx.request_context.lifespan_context
        result = await timescaledb_admin.merge_chunks(
            app.database,
            app.database.driver(),
            chunk_schema,
            chunk_names,
            concurrently=concurrently,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
            timeout_s=_timeout(ctx),
        )
        await _clear(ctx, result)
        return result

    @server.tool(
        name="refresh_continuous_aggregate",
        description=(
            "Refresh a continuous aggregate now over [window_start, window_end) (ISO timestamps; null = "
            "unbounded). force=true recomputes buckets even if already refreshed. Cannot run in a "
            "transaction, so it runs on an autocommit connection in batches (a failure can leave the window "
            "partly refreshed) and is unavailable when MCPG_DEFAULT_ROLE / MCPG_ALLOWED_ROLES is set. " + _CONFIRM_FLOW
        ),
    )
    async def refresh_continuous_aggregate(
        ctx: _Ctx,
        schema: str,
        view: str,
        window_start: str | None = None,
        window_end: str | None = None,
        confirmation_token: str | None = None,
        *,
        force: bool = False,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        _untenanted(ctx)
        app = ctx.request_context.lifespan_context
        result = await timescaledb_admin.refresh_continuous_aggregate(
            app.database,
            app.database.driver(),
            schema,
            view,
            window_start=window_start,
            window_end=window_end,
            force=force,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
            timeout_s=_timeout(ctx),
        )
        await _clear(ctx, result)
        return result

    @server.tool(
        name="run_job",
        description=(
            "Run a TimescaleDB user/policy job immediately (job ids >= 1000; system jobs are refused). "
            "A retention policy job drops chunks right away. " + _CONFIRM_FLOW
        ),
    )
    async def run_job(
        ctx: _Ctx,
        job_id: int,
        confirmation_token: str | None = None,
        *,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        result = await timescaledb_admin.run_job(
            _driver(ctx), job_id, dry_run=dry_run, confirm=confirm, confirmation_token=confirmation_token
        )
        await _clear(ctx, result)
        return result

    @server.tool(
        name="alter_job",
        description=(
            "Change a TimescaleDB user/policy job's schedule_interval, max_runtime, max_retries, "
            "retry_period or scheduled flag (at least one). The job's config and procedure cannot be "
            "changed. " + _CONFIRM_FLOW
        ),
    )
    async def alter_job(
        ctx: _Ctx,
        job_id: int,
        schedule_interval: str | None = None,
        max_runtime: str | None = None,
        max_retries: int | None = None,
        retry_period: str | None = None,
        confirmation_token: str | None = None,
        *,
        scheduled: bool | None = None,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        result = await timescaledb_admin.alter_job(
            _driver(ctx),
            job_id,
            schedule_interval=schedule_interval,
            max_runtime=max_runtime,
            max_retries=max_retries,
            retry_period=retry_period,
            scheduled=scheduled,
            dry_run=dry_run,
            confirm=confirm,
            confirmation_token=confirmation_token,
        )
        await _clear(ctx, result)
        return result

    @server.tool(
        name="delete_job",
        description=(
            "Delete a TimescaleDB user/policy job (job ids >= 1000). For policy jobs prefer the matching "
            "remove_*_policy tool. " + _CONFIRM_FLOW
        ),
    )
    async def delete_job(
        ctx: _Ctx,
        job_id: int,
        confirmation_token: str | None = None,
        *,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> timescaledb_admin.TimescaleAdminResult:
        result = await timescaledb_admin.delete_job(
            _driver(ctx), job_id, dry_run=dry_run, confirm=confirm, confirmation_token=confirmation_token
        )
        await _clear(ctx, result)
        return result
