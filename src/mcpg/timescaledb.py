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

import re
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# A whitelist of intervals we'll inline into SQL. Validated against this
# pattern then passed as a SQL literal (TimescaleDB takes interval
# expressions as positional args, not bound params).
_INTERVAL = re.compile(
    r"\A\d+\s+(microseconds?|milliseconds?|seconds?|minutes?|hours?|days?|weeks?|months?|years?)\Z", re.IGNORECASE
)


class TimescaleError(Exception):
    """Raised when a TimescaleDB tool call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise TimescaleError(f"invalid {kind} name: {name!r}")


def _check_interval(value: str) -> None:
    if not _INTERVAL.match(value.strip()):
        raise TimescaleError(f"invalid interval {value!r}; expected e.g. '7 days', '1 hour', '30 minutes'")


def _quoted(name: str) -> str:
    return f'"{name}"'


@dataclass(frozen=True, slots=True)
class HypertableInfo:
    """Summary of one TimescaleDB hypertable."""

    schema: str
    name: str
    num_dimensions: int
    num_chunks: int
    compression_enabled: bool
    total_size_bytes: int


@dataclass(frozen=True, slots=True)
class HypertableListResult:
    """The result of :func:`list_hypertables`.

    ``available`` is ``False`` when the ``timescaledb`` extension is
    not installed on the target database.
    """

    available: bool
    hypertables: list[HypertableInfo]


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    """One chunk under a hypertable."""

    hypertable_schema: str
    hypertable_name: str
    chunk_schema: str
    chunk_name: str
    range_start: str | None
    range_end: str | None
    is_compressed: bool


@dataclass(frozen=True, slots=True)
class ChunkListResult:
    """The result of :func:`list_chunks`."""

    available: bool
    chunks: list[ChunkInfo]


@dataclass(frozen=True, slots=True)
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
        f"SELECT create_hypertable('\"{schema}\".\"{table}\"', '{time_column}', "
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
    await driver.execute_query(f'ALTER TABLE "{schema}"."{table}" SET (timescaledb.compress = TRUE)')
    rows = await driver.execute_query(
        f"SELECT add_compression_policy('\"{schema}\".\"{table}\"', INTERVAL '{compress_after}') AS job_id"
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
        f"SELECT add_retention_policy('\"{schema}\".\"{table}\"', INTERVAL '{drop_after}') AS job_id"
    )
    detail = f"job_id={rows[0].cells['job_id']}" if rows else "policy added"
    return TimescaleWriteResult(available=True, function="add_retention_policy", details=detail)


def _quoted_unused(_: Any) -> None:
    # Placeholder so an editor's "unused import" warning on _quoted
    # doesn't fire in modules that only validate identifiers but never
    # build qualified relations directly.
    pass
