"""TimescaleDB hypertable + compression + retention helpers.

Thin wrappers around the most-used TimescaleDB management functions:
``create_hypertable``, ``add_dimension``, ``set_chunk_time_interval``,
the compression/retention/continuous-aggregate policies, and the
read tools that introspect what already exists. Every tool degrades
gracefully when the ``timescaledb`` extension is not installed —
returning ``available=False`` rather than raising — so an MCPg
deployment that doesn't have Timescale on the target database still
reports the tools as callable.

Write tools are gated under ``Capability.DDL`` + ``MCPG_ALLOW_DDL``;
the underlying TimescaleDB functions issue DDL.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from mcpg.errors import MCPgError
from mcpg.extensions import extension_installed
from mcpg.identifiers import IdentifierError, ensure_identifier
from mcpg.identifiers import quote_identifier as _qi
from mcpg.sql import SqlDriver

# A whitelist of intervals we'll inline into SQL. Validated against this
# pattern then passed as a SQL literal (TimescaleDB takes interval
# expressions as positional args, not bound params).
_INTERVAL = re.compile(
    r"\A\d+\s+(microseconds?|milliseconds?|seconds?|minutes?|hours?|days?|weeks?|months?|years?)\Z", re.IGNORECASE
)


class TimescaleError(MCPgError):
    """Raised when a TimescaleDB tool call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    # Accept any addressable identifier; it is quoted (and, where it lands
    # inside a string literal, single-quote-escaped) at each splice site.
    # Only empty / NUL / overlong names are rejected.
    try:
        ensure_identifier(name, kind)
    except IdentifierError as exc:
        raise TimescaleError(str(exc)) from exc


def _sql_str_literal(text: str) -> str:
    """Return ``text`` as a single-quoted SQL string literal (quotes doubled)."""
    return "'" + text.replace("'", "''") + "'"


def _regclass_literal(schema: str, table: str) -> str:
    """Return ``'"schema"."table"'`` — a quoted relation inside a string literal.

    TimescaleDB's ``create_hypertable`` / ``add_*_policy`` take the relation
    as a ``regclass`` *text* argument, so the double-quoted identifier is
    itself wrapped in a single-quoted literal; both layers are escaped
    (``"`` doubled by :func:`quote_identifier`, ``'`` doubled here).
    """
    return _sql_str_literal(f"{_qi(schema)}.{_qi(table)}")


def _check_interval(value: str) -> None:
    if not _INTERVAL.match(value.strip()):
        raise TimescaleError(f"invalid interval {value!r}; expected e.g. '7 days', '1 hour', '30 minutes'")


@dataclass(frozen=True)
class HypertableInfo:
    """Summary of one TimescaleDB hypertable."""

    schema: str
    name: str
    num_dimensions: int
    num_chunks: int
    compression_enabled: bool
    total_size_bytes: int


@dataclass(frozen=True)
class HypertableListResult:
    """The result of :func:`list_hypertables`.

    ``available`` is ``False`` when the ``timescaledb`` extension is
    not installed on the target database.
    """

    available: bool
    hypertables: list[HypertableInfo]


@dataclass(frozen=True)
class ChunkInfo:
    """One chunk under a hypertable."""

    hypertable_schema: str
    hypertable_name: str
    chunk_schema: str
    chunk_name: str
    range_start: str | None
    range_end: str | None
    is_compressed: bool


@dataclass(frozen=True)
class ChunkListResult:
    """The result of :func:`list_chunks`."""

    available: bool
    chunks: list[ChunkInfo]


@dataclass(frozen=True)
class TimescaleWriteResult:
    """The outcome of a TimescaleDB write tool call."""

    available: bool
    function: str
    details: str


async def list_hypertables(driver: SqlDriver) -> HypertableListResult:
    """List every hypertable visible to the current role.

    Reads from ``timescaledb_information.hypertables`` (Timescale's
    public catalog view); pre-2.x columns are not supported.
    """
    if not await extension_installed(driver, "timescaledb"):
        return HypertableListResult(available=False, hypertables=[])
    rows = await driver.execute_query(
        "SELECT hypertable_schema, hypertable_name, num_dimensions, "
        "       num_chunks, compression_enabled, "
        "       COALESCE("
        "         hypertable_size(format('%I.%I', hypertable_schema, hypertable_name)::regclass), "
        "         0"
        "       ) AS total_size_bytes "
        "FROM timescaledb_information.hypertables "
        "ORDER BY hypertable_schema, hypertable_name",
        force_readonly=True,
    )
    hypertables = [
        HypertableInfo(
            schema=str(row.cells["hypertable_schema"]),
            name=str(row.cells["hypertable_name"]),
            num_dimensions=int(row.cells["num_dimensions"]),
            num_chunks=int(row.cells["num_chunks"]),
            compression_enabled=bool(row.cells["compression_enabled"]),
            total_size_bytes=int(row.cells["total_size_bytes"]),
        )
        for row in rows or []
    ]
    return HypertableListResult(available=True, hypertables=hypertables)


async def list_chunks(driver: SqlDriver, schema: str, table: str) -> ChunkListResult:
    """List the chunks of ``schema.table``.

    Empty list when the table is not a hypertable; the caller can
    cross-check with :func:`list_hypertables` first.
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    if not await extension_installed(driver, "timescaledb"):
        return ChunkListResult(available=False, chunks=[])
    rows = await driver.execute_query(
        "SELECT hypertable_schema, hypertable_name, chunk_schema, chunk_name, "
        "       range_start::text AS range_start, range_end::text AS range_end, "
        "       is_compressed "
        "FROM timescaledb_information.chunks "
        "WHERE hypertable_schema = %s AND hypertable_name = %s "
        "ORDER BY range_start NULLS LAST",
        params=[schema, table],
        force_readonly=True,
    )
    chunks = [
        ChunkInfo(
            hypertable_schema=str(row.cells["hypertable_schema"]),
            hypertable_name=str(row.cells["hypertable_name"]),
            chunk_schema=str(row.cells["chunk_schema"]),
            chunk_name=str(row.cells["chunk_name"]),
            range_start=str(row.cells["range_start"]) if row.cells["range_start"] is not None else None,
            range_end=str(row.cells["range_end"]) if row.cells["range_end"] is not None else None,
            is_compressed=bool(row.cells["is_compressed"]),
        )
        for row in rows or []
    ]
    return ChunkListResult(available=True, chunks=chunks)


async def create_hypertable(
    driver: SqlDriver,
    schema: str,
    table: str,
    time_column: str,
    *,
    chunk_time_interval: str = "7 days",
    if_not_exists: bool = True,
) -> TimescaleWriteResult:
    """Convert an existing table into a hypertable on ``time_column``.

    Args:
        chunk_time_interval: TimescaleDB interval expression (e.g.
            ``'7 days'``, ``'1 hour'``). Validated against an allowlist
            pattern before being inlined into SQL.
        if_not_exists: When ``True``, calling on a table that's
            already a hypertable is a no-op rather than an error.
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    _check_identifier(time_column, "time_column")
    _check_interval(chunk_time_interval)
    if not await extension_installed(driver, "timescaledb"):
        return TimescaleWriteResult(
            available=False, function="create_hypertable", details="timescaledb extension is not installed"
        )
    inex = "TRUE" if if_not_exists else "FALSE"
    # Double-quote inside the string literal so a mixed-case relation
    # name reaches regclass with its case preserved — unquoted identifiers
    # passed to functions that cast to regclass are folded to lowercase.
    sql = (
        f"SELECT create_hypertable({_regclass_literal(schema, table)}, {_sql_str_literal(time_column)}, "
        f"chunk_time_interval => INTERVAL '{chunk_time_interval}', "
        f"if_not_exists => {inex}) AS result"
    )
    rows = await driver.execute_query(sql)
    detail = str(rows[0].cells["result"]) if rows else "OK"
    return TimescaleWriteResult(available=True, function="create_hypertable", details=detail)


async def add_compression_policy(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    compress_after: str = "7 days",
) -> TimescaleWriteResult:
    """Enable compression on ``schema.table`` and schedule the policy.

    Two calls under one tool: ``ALTER TABLE ... SET (timescaledb.compress)``
    and ``add_compression_policy``. Idempotent on the ALTER (TimescaleDB
    no-ops when compression is already enabled) and on the policy
    (TimescaleDB raises a friendly error which we surface).
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    _check_interval(compress_after)
    if not await extension_installed(driver, "timescaledb"):
        return TimescaleWriteResult(
            available=False, function="add_compression_policy", details="timescaledb extension is not installed"
        )
    await driver.execute_query(f"ALTER TABLE {_qi(schema)}.{_qi(table)} SET (timescaledb.compress = TRUE)")
    rows = await driver.execute_query(
        f"SELECT add_compression_policy({_regclass_literal(schema, table)}, INTERVAL '{compress_after}') AS job_id"
    )
    detail = f"job_id={rows[0].cells['job_id']}" if rows else "policy added"
    return TimescaleWriteResult(available=True, function="add_compression_policy", details=detail)


async def add_retention_policy(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    drop_after: str = "30 days",
) -> TimescaleWriteResult:
    """Schedule a retention policy that drops chunks older than ``drop_after``."""
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    _check_interval(drop_after)
    if not await extension_installed(driver, "timescaledb"):
        return TimescaleWriteResult(
            available=False, function="add_retention_policy", details="timescaledb extension is not installed"
        )
    rows = await driver.execute_query(
        f"SELECT add_retention_policy({_regclass_literal(schema, table)}, INTERVAL '{drop_after}') AS job_id"
    )
    detail = f"job_id={rows[0].cells['job_id']}" if rows else "policy added"
    return TimescaleWriteResult(available=True, function="add_retention_policy", details=detail)


# ---------------------------------------------------------------------------
# Tier R: reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HypertableStats:
    """Size, chunk and compression statistics for one hypertable."""

    available: bool
    schema: str
    table: str
    total_chunks: int | None
    compressed_chunks: int | None
    approximate_row_count: int | None
    table_bytes: int | None
    index_bytes: int | None
    toast_bytes: int | None
    total_bytes: int | None
    before_compression_bytes: int | None
    after_compression_bytes: int | None


def _opt_int(value: object) -> int | None:
    return None if value is None else int(str(value))


async def get_hypertable_stats(driver: SqlDriver, schema: str, table: str) -> HypertableStats:
    """Sizes, chunk counts, an approximate row count and compression savings.

    Uses ``hypertable_columnstore_stats`` when the server has it (newer
    TimescaleDB) and ``hypertable_compression_stats`` otherwise. The
    before/after compression byte counts are ``None`` while compression is not
    enabled on the hypertable (those functions then return no row).
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    empty = HypertableStats(
        available=False,
        schema=schema,
        table=table,
        total_chunks=None,
        compressed_chunks=None,
        approximate_row_count=None,
        table_bytes=None,
        index_bytes=None,
        toast_bytes=None,
        total_bytes=None,
        before_compression_bytes=None,
        after_compression_bytes=None,
    )
    if not await extension_installed(driver, "timescaledb"):
        return empty
    exists = await driver.execute_query(
        "SELECT h.num_chunks, "
        "       (SELECT count(*) FROM timescaledb_information.chunks c "
        "         WHERE c.hypertable_schema = h.hypertable_schema AND c.hypertable_name = h.hypertable_name "
        "           AND c.is_compressed) AS compressed_chunks "
        "FROM timescaledb_information.hypertables h WHERE h.hypertable_schema = %s AND h.hypertable_name = %s",
        params=[schema, table],
        force_readonly=True,
    )
    if not exists:
        raise TimescaleError(f"{schema}.{table} is not a hypertable")
    rel = f"{_qi(schema)}.{_qi(table)}"
    sizes = await driver.execute_query(
        "SELECT table_bytes, index_bytes, toast_bytes, total_bytes FROM hypertable_detailed_size(%s::regclass)",
        params=[rel],
        force_readonly=True,
    )
    approx = await driver.execute_query(
        "SELECT approximate_row_count(%s::regclass) AS n", params=[rel], force_readonly=True
    )
    fn_rows = await driver.execute_query(
        "SELECT CASE WHEN to_regproc('hypertable_columnstore_stats') IS NOT NULL "
        "THEN 'hypertable_columnstore_stats' ELSE 'hypertable_compression_stats' END AS fn",
        force_readonly=True,
    )
    stats_fn = str(fn_rows[0].cells["fn"]) if fn_rows else "hypertable_compression_stats"
    comp = await driver.execute_query(
        f"SELECT before_compression_total_bytes, after_compression_total_bytes FROM {stats_fn}(%s::regclass)",
        params=[rel],
        force_readonly=True,
    )
    s = sizes[0].cells if sizes else {}
    c = comp[0].cells if comp else {}
    return HypertableStats(
        available=True,
        schema=schema,
        table=table,
        total_chunks=_opt_int(exists[0].cells["num_chunks"]),
        compressed_chunks=_opt_int(exists[0].cells["compressed_chunks"]),
        approximate_row_count=_opt_int(approx[0].cells["n"]) if approx else None,
        table_bytes=_opt_int(s.get("table_bytes")),
        index_bytes=_opt_int(s.get("index_bytes")),
        toast_bytes=_opt_int(s.get("toast_bytes")),
        total_bytes=_opt_int(s.get("total_bytes")),
        before_compression_bytes=_opt_int(c.get("before_compression_total_bytes")),
        after_compression_bytes=_opt_int(c.get("after_compression_total_bytes")),
    )


@dataclass(frozen=True)
class JobInfo:
    """One TimescaleDB background job with its latest run statistics."""

    job_id: int
    application_name: str
    procedure: str
    schedule_interval: str
    scheduled: bool
    hypertable_schema: str | None
    hypertable_name: str | None
    next_start: str | None
    last_run_status: str | None
    last_run_started_at: str | None
    total_runs: int | None
    total_failures: int | None


@dataclass(frozen=True)
class JobListResult:
    """The result of :func:`list_jobs`."""

    available: bool
    jobs: list[JobInfo]


async def list_jobs(driver: SqlDriver) -> JobListResult:
    """List every background job (policies and user jobs) with run statistics."""
    if not await extension_installed(driver, "timescaledb"):
        return JobListResult(available=False, jobs=[])
    rows = await driver.execute_query(
        "SELECT j.job_id, j.application_name, j.proc_schema || '.' || j.proc_name AS procedure, "
        "       j.schedule_interval::text AS schedule_interval, j.scheduled, "
        "       j.hypertable_schema, j.hypertable_name, j.next_start::text AS next_start, "
        "       s.last_run_status, s.last_run_started_at::text AS last_run_started_at, "
        "       s.total_runs, s.total_failures "
        "FROM timescaledb_information.jobs j "
        "LEFT JOIN timescaledb_information.job_stats s USING (job_id) "
        "ORDER BY j.job_id",
        force_readonly=True,
    )
    jobs = [
        JobInfo(
            job_id=int(r.cells["job_id"]),
            application_name=str(r.cells["application_name"]),
            procedure=str(r.cells["procedure"]),
            schedule_interval=str(r.cells["schedule_interval"]),
            scheduled=bool(r.cells["scheduled"]),
            hypertable_schema=None if r.cells["hypertable_schema"] is None else str(r.cells["hypertable_schema"]),
            hypertable_name=None if r.cells["hypertable_name"] is None else str(r.cells["hypertable_name"]),
            next_start=None if r.cells["next_start"] is None else str(r.cells["next_start"]),
            last_run_status=None if r.cells["last_run_status"] is None else str(r.cells["last_run_status"]),
            last_run_started_at=None if r.cells["last_run_started_at"] is None else str(r.cells["last_run_started_at"]),
            total_runs=_opt_int(r.cells["total_runs"]),
            total_failures=_opt_int(r.cells["total_failures"]),
        )
        for r in rows or []
    ]
    return JobListResult(available=True, jobs=jobs)


@dataclass(frozen=True)
class ContinuousAggregateInfo:
    """One continuous aggregate and its source hypertable."""

    view_schema: str
    view_name: str
    source_schema: str
    source_name: str
    materialized_only: bool
    compression_enabled: bool


@dataclass(frozen=True)
class ContinuousAggregateListResult:
    """The result of :func:`list_continuous_aggregates`."""

    available: bool
    continuous_aggregates: list[ContinuousAggregateInfo]


async def list_continuous_aggregates(driver: SqlDriver) -> ContinuousAggregateListResult:
    """List every continuous aggregate visible to the current role."""
    if not await extension_installed(driver, "timescaledb"):
        return ContinuousAggregateListResult(available=False, continuous_aggregates=[])
    rows = await driver.execute_query(
        "SELECT view_schema, view_name, hypertable_schema, hypertable_name, "
        "       materialized_only, compression_enabled "
        "FROM timescaledb_information.continuous_aggregates ORDER BY view_schema, view_name",
        force_readonly=True,
    )
    aggs = [
        ContinuousAggregateInfo(
            view_schema=str(r.cells["view_schema"]),
            view_name=str(r.cells["view_name"]),
            source_schema=str(r.cells["hypertable_schema"]),
            source_name=str(r.cells["hypertable_name"]),
            materialized_only=bool(r.cells["materialized_only"]),
            compression_enabled=bool(r.cells["compression_enabled"]),
        )
        for r in rows or []
    ]
    return ContinuousAggregateListResult(available=True, continuous_aggregates=aggs)


@dataclass(frozen=True)
class TelemetryReport:
    """The usage report TimescaleDB would send to its vendor (built locally)."""

    available: bool
    report: dict[str, Any]


async def get_telemetry_report(driver: SqlDriver) -> TelemetryReport:
    """Return ``get_telemetry_report()``.

    The function only *builds* the report from local catalog data (verified
    on 2.30.2 with the server's network disconnected); the periodic upload is
    the separate ``policy_telemetry`` background job, controlled by
    ``timescaledb.telemetry_level``. Nothing is sent by this call.
    """
    if not await extension_installed(driver, "timescaledb"):
        return TelemetryReport(available=False, report={})
    rows = await driver.execute_query("SELECT get_telemetry_report() AS report", force_readonly=True)
    raw = rows[0].cells["report"] if rows else {}
    report = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    return TelemetryReport(available=True, report=report)


# ---------------------------------------------------------------------------
# Tier A: additive / reversible DDL
# ---------------------------------------------------------------------------


def _interval_or_null(value: str | None) -> str:
    if value is None:
        return "NULL"
    _check_interval(value)
    return f"INTERVAL '{value.strip()}'"


async def _write(
    driver: SqlDriver, function: str, sql: str, *, describe: str = "OK", id_column: str | None = None
) -> TimescaleWriteResult:
    if not await extension_installed(driver, "timescaledb"):
        return TimescaleWriteResult(
            available=False, function=function, details="timescaledb extension is not installed"
        )
    rows = await driver.execute_query(sql, force_readonly=False)
    detail = f"{id_column}={rows[0].cells[id_column]}" if rows and id_column is not None else describe
    return TimescaleWriteResult(available=True, function=function, details=detail)


async def add_dimension(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    *,
    number_partitions: int | None = None,
    chunk_time_interval: str | None = None,
    if_not_exists: bool = True,
) -> TimescaleWriteResult:
    """Add a dimension to a hypertable: hash (``number_partitions``) or range (``chunk_time_interval``)."""
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    _check_identifier(column, "column")
    if (number_partitions is None) == (chunk_time_interval is None):
        raise TimescaleError(
            "add_dimension needs exactly one of number_partitions (hash) or chunk_time_interval (range)"
        )
    if number_partitions is not None and number_partitions < 1:
        raise TimescaleError("number_partitions must be >= 1")
    spec = (
        f"number_partitions => {int(number_partitions)}"
        if number_partitions is not None
        else f"chunk_time_interval => {_interval_or_null(chunk_time_interval)}"
    )
    inex = "TRUE" if if_not_exists else "FALSE"
    sql = (
        f"SELECT dimension_id, created FROM add_dimension({_regclass_literal(schema, table)}, "
        f"{_sql_str_literal(column)}, {spec}, if_not_exists => {inex})"
    )
    return await _write(driver, "add_dimension", sql, id_column="dimension_id")


async def set_chunk_time_interval(
    driver: SqlDriver, schema: str, table: str, chunk_time_interval: str
) -> TimescaleWriteResult:
    """Change the chunk interval for chunks created from now on (existing chunks are untouched)."""
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    _check_interval(chunk_time_interval)
    sql = (
        f"SELECT set_chunk_time_interval({_regclass_literal(schema, table)}, INTERVAL '{chunk_time_interval.strip()}')"
    )
    return await _write(driver, "set_chunk_time_interval", sql, describe="chunk_time_interval updated")


async def add_continuous_aggregate_policy(
    driver: SqlDriver,
    schema: str,
    view: str,
    *,
    start_offset: str | None,
    end_offset: str | None,
    schedule_interval: str,
    if_not_exists: bool = True,
) -> TimescaleWriteResult:
    """Schedule automatic refresh of a continuous aggregate (NULL offsets = unbounded on that side)."""
    _check_identifier(schema, "schema")
    _check_identifier(view, "view")
    _check_interval(schedule_interval)
    inex = "TRUE" if if_not_exists else "FALSE"
    sql = (
        f"SELECT add_continuous_aggregate_policy({_regclass_literal(schema, view)}, "
        f"start_offset => {_interval_or_null(start_offset)}, end_offset => {_interval_or_null(end_offset)}, "
        f"schedule_interval => INTERVAL '{schedule_interval.strip()}', if_not_exists => {inex}) AS job_id"
    )
    return await _write(driver, "add_continuous_aggregate_policy", sql, id_column="job_id")


async def add_reorder_policy(
    driver: SqlDriver, schema: str, table: str, index_name: str, *, if_not_exists: bool = True
) -> TimescaleWriteResult:
    """Schedule background reordering of chunks along an index."""
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    _check_identifier(index_name, "index_name")
    inex = "TRUE" if if_not_exists else "FALSE"
    sql = (
        f"SELECT add_reorder_policy({_regclass_literal(schema, table)}, {_sql_str_literal(index_name)}, "
        f"if_not_exists => {inex}) AS job_id"
    )
    return await _write(driver, "add_reorder_policy", sql, id_column="job_id")


async def _remove_policy(driver: SqlDriver, function: str, schema: str, relation: str) -> TimescaleWriteResult:
    _check_identifier(schema, "schema")
    _check_identifier(relation, "relation")
    sql = f"SELECT {function}({_regclass_literal(schema, relation)}, if_exists => TRUE)"
    return await _write(driver, function, sql, describe="policy removed (if it existed)")


async def remove_retention_policy(driver: SqlDriver, schema: str, table: str) -> TimescaleWriteResult:
    """Remove a hypertable's retention policy (no-op if none)."""
    return await _remove_policy(driver, "remove_retention_policy", schema, table)


async def remove_compression_policy(driver: SqlDriver, schema: str, table: str) -> TimescaleWriteResult:
    """Remove a hypertable's compression policy (no-op if none)."""
    return await _remove_policy(driver, "remove_compression_policy", schema, table)


async def remove_continuous_aggregate_policy(driver: SqlDriver, schema: str, view: str) -> TimescaleWriteResult:
    """Remove a continuous aggregate's refresh policy (no-op if none)."""
    return await _remove_policy(driver, "remove_continuous_aggregate_policy", schema, view)


async def remove_reorder_policy(driver: SqlDriver, schema: str, table: str) -> TimescaleWriteResult:
    """Remove a hypertable's reorder policy (no-op if none)."""
    return await _remove_policy(driver, "remove_reorder_policy", schema, table)
