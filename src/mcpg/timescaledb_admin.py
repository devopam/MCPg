"""TimescaleDB destructive / heavy administration tools ("tier B").

Chunk drop / compress / decompress / reorder / merge, continuous-aggregate
refresh and background-job control. These are separate from the additive
helpers in :mod:`mcpg.timescaledb` because they can delete data, take heavy
locks, or run work immediately, so they sit behind
``MCPG_ALLOW_TIMESCALEDB_ADMIN`` (on top of unrestricted mode and
``MCPG_ALLOW_DDL``) and follow the ALTER SYSTEM confirmation flow:

1. ``dry_run=true`` -> the plan, the affected chunks and a
   ``confirmation_token``.
2. ``confirm=true`` + that exact ``confirmation_token`` -> execute.

The token is a stateless digest of the planned statement (and, for
``drop_chunks``, of the chunk set it would drop), so a changed plan asks for
confirmation again.

Execution paths were verified against TimescaleDB 2.30.2: most calls run in
a normal transaction, but ``refresh_continuous_aggregate``,
``merge_chunks_concurrently`` and ``reorder_chunk`` refuse to run inside a
transaction block, so those use :meth:`Database.run_unmanaged` (autocommit).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field

from mcpg.database import Database
from mcpg.extensions import extension_installed
from mcpg.sql import SqlDriver
from mcpg.timescaledb import (
    TimescaleError,
    _check_identifier,
    _check_interval,
    _regclass_literal,
)

_DEFAULT_TIMEOUT_S = 30.0
# Job ids below this are TimescaleDB system jobs (telemetry reporter, job
# history retention). User / policy jobs start at 1000.
_MIN_USER_JOB_ID = 1000
_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?)?\s*(Z|[+-]\d{2}(:?\d{2})?)?\Z")


@dataclass(frozen=True)
class TimescaleAdminResult:
    """Outcome of a tier-B TimescaleDB tool call."""

    available: bool
    status: str  # applied | dry_run | confirmation_required | no_op | unavailable
    function: str
    planned_action: str
    affected: list[str] = field(default_factory=list)
    warning: str | None = None
    confirmation_token: str | None = None
    details: str = ""


def require_untenanted(default_role: str | None, allowed_roles: tuple[str, ...]) -> None:
    """Autocommit paths use the raw pool connection and bypass tenant roles."""
    if default_role is not None or allowed_roles:
        raise TimescaleError(
            "this TimescaleDB admin tool is disabled when MCPG_DEFAULT_ROLE / MCPG_ALLOWED_ROLES is set: "
            "it runs as the connection login and would bypass per-role privilege separation"
        )


def _token(planned: str, affected: list[str]) -> str:
    digest = hashlib.sha256((planned + "|" + ",".join(sorted(affected))).encode("utf-8"))
    return digest.hexdigest()[:16]


def _interval_literal(value: str, kind: str) -> str:
    _check_interval(value)
    return f"INTERVAL '{value.strip()}'"


def _timestamp_literal(value: str | None, kind: str) -> str:
    if value is None:
        return "NULL"
    if not _TIMESTAMP.match(value.strip()):
        raise TimescaleError(
            f"invalid {kind} {value!r}; expected an ISO-8601 timestamp such as '2026-01-31 00:00:00+00'"
        )
    return f"'{value.strip()}'::timestamptz"


def _unavailable(function: str) -> TimescaleAdminResult:
    return TimescaleAdminResult(
        available=False,
        status="unavailable",
        function=function,
        planned_action="",
        details="timescaledb extension is not installed",
    )


def _gate(
    *,
    function: str,
    planned: str,
    affected: list[str],
    warning: str,
    dry_run: bool,
    confirm: bool,
    confirmation_token: str | None,
) -> TimescaleAdminResult | None:
    """Return a dry-run / confirmation-required result, or ``None`` to proceed."""
    token = _token(planned, affected)
    if dry_run:
        return TimescaleAdminResult(
            available=True,
            status="dry_run",
            function=function,
            planned_action=planned,
            affected=affected,
            warning=warning,
            confirmation_token=token,
            details="No changes applied (dry_run=true). Call again with dry_run=false, confirm=true and "
            "confirmation_token to apply.",
        )
    if not confirm or confirmation_token != token:
        mismatch = "confirmation_token does not match this action. " if confirm and confirmation_token else ""
        return TimescaleAdminResult(
            available=True,
            status="confirmation_required",
            function=function,
            planned_action=planned,
            affected=affected,
            warning=warning,
            confirmation_token=token,
            details=f"No changes applied. {mismatch}Show the warning to the user, then call again with "
            "confirm=true and this confirmation_token to execute.",
        )
    return None


async def _run_autocommit(database: Database, sql: str, timeout_s: float) -> None:
    try:
        await asyncio.wait_for(database.run_unmanaged(sql), timeout=timeout_s)
    except TimeoutError as exc:
        raise TimescaleError(f"timed out after {timeout_s:g}s running: {sql}") from exc


async def _require_hypertable(driver: SqlDriver, schema: str, table: str) -> None:
    rows = await driver.execute_query(
        "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_schema = %s AND hypertable_name = %s",
        params=[schema, table],
        force_readonly=False,
    )
    if not rows:
        raise TimescaleError(f"{schema}.{table} is not a hypertable")


async def _require_chunk(driver: SqlDriver, chunk_schema: str, chunk_name: str) -> bool:
    """Confirm the relation is a Timescale chunk; return whether it is compressed."""
    _check_identifier(chunk_schema, "chunk_schema")
    _check_identifier(chunk_name, "chunk_name")
    rows = await driver.execute_query(
        "SELECT is_compressed FROM timescaledb_information.chunks WHERE chunk_schema = %s AND chunk_name = %s",
        params=[chunk_schema, chunk_name],
        force_readonly=False,
    )
    if not rows:
        raise TimescaleError(f"{chunk_schema}.{chunk_name} is not a TimescaleDB chunk")
    return bool(rows[0].cells["is_compressed"])


async def _require_cagg(driver: SqlDriver, schema: str, view: str) -> None:
    rows = await driver.execute_query(
        "SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_schema = %s AND view_name = %s",
        params=[schema, view],
        force_readonly=False,
    )
    if not rows:
        raise TimescaleError(f"{schema}.{view} is not a continuous aggregate")


async def _require_user_job(driver: SqlDriver, job_id: int) -> str:
    """Confirm ``job_id`` is a user/policy job; return its application name."""
    if job_id < _MIN_USER_JOB_ID:
        raise TimescaleError(
            f"job {job_id} is a TimescaleDB system job (ids below {_MIN_USER_JOB_ID}); refusing to touch it"
        )
    rows = await driver.execute_query(
        "SELECT application_name FROM timescaledb_information.jobs WHERE job_id = %s",
        params=[job_id],
        force_readonly=False,
    )
    if not rows:
        raise TimescaleError(f"job {job_id} not found")
    return str(rows[0].cells["application_name"])


def _rel(schema: str, name: str, kind: str) -> str:
    _check_identifier(schema, f"{kind}_schema" if kind != "table" else "schema")
    _check_identifier(name, kind)
    return _regclass_literal(schema, name)


# ---------------------------------------------------------------------------
# drop_chunks
# ---------------------------------------------------------------------------


async def drop_chunks(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    older_than: str | None = None,
    newer_than: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> TimescaleAdminResult:
    """Drop whole chunks of a hypertable that fall outside the given bounds.

    Bounds are intervals relative to ``now()`` (``older_than='30 days'``
    drops chunks entirely older than 30 days). ``created_*`` bounds filter on
    chunk creation time and cannot be combined with ``older_than`` /
    ``newer_than`` (TimescaleDB rejects that).
    """
    _rel(schema, table, "table")
    bounds = {
        "older_than": older_than,
        "newer_than": newer_than,
        "created_before": created_before,
        "created_after": created_after,
    }
    given = {k: v for k, v in bounds.items() if v is not None}
    if not given:
        raise TimescaleError("drop_chunks needs at least one of older_than, newer_than, created_before, created_after")
    if ("created_before" in given or "created_after" in given) and ("older_than" in given or "newer_than" in given):
        raise TimescaleError("created_before/created_after cannot be combined with older_than/newer_than")
    args = ", ".join(f"{k} => {_interval_literal(v, k)}" for k, v in given.items())
    rel = _regclass_literal(schema, table)

    if not await extension_installed(driver, "timescaledb"):
        return _unavailable("drop_chunks")
    await _require_hypertable(driver, schema, table)

    rows = await driver.execute_query(
        f"SELECT c::text AS chunk FROM show_chunks({rel}, {args}) AS c", force_readonly=False
    )
    affected = [str(r.cells["chunk"]) for r in rows or []]
    planned = f"SELECT drop_chunks({rel}, {args})"
    if not affected:
        return TimescaleAdminResult(
            available=True,
            status="no_op",
            function="drop_chunks",
            planned_action=planned,
            details="No chunks match the given bounds; nothing to drop.",
        )
    warning = (
        f"drop_chunks permanently deletes {len(affected)} whole chunk(s) of {schema}.{table} and all rows in them. "
        "It cannot be undone. Continuous aggregates built on this table keep their materialised data only if "
        "they were refreshed before the drop (and have no refresh policy that would re-materialise the gap)."
    )
    gated = _gate(
        function="drop_chunks",
        planned=planned,
        affected=affected,
        warning=warning,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )
    if gated is not None:
        return gated
    dropped = await driver.execute_query(planned, force_readonly=False)
    names = [str(next(iter(r.cells.values()))) for r in dropped or []]
    return TimescaleAdminResult(
        available=True,
        status="applied",
        function="drop_chunks",
        planned_action=planned,
        affected=names or affected,
        details=f"Dropped {len(names or affected)} chunk(s).",
    )


# ---------------------------------------------------------------------------
# compress_chunk / decompress_chunk
# ---------------------------------------------------------------------------


async def _chunk_op(
    driver: SqlDriver,
    *,
    function: str,
    chunk_schema: str,
    chunk_name: str,
    want_compressed: bool,
    warning: str,
    dry_run: bool,
    confirm: bool,
    confirmation_token: str | None,
) -> TimescaleAdminResult:
    if not await extension_installed(driver, "timescaledb"):
        return _unavailable(function)
    is_compressed = await _require_chunk(driver, chunk_schema, chunk_name)
    rel = _regclass_literal(chunk_schema, chunk_name)
    planned = f"SELECT {function}({rel})"
    if is_compressed == want_compressed:
        state = "already compressed" if want_compressed else "not compressed"
        return TimescaleAdminResult(
            available=True,
            status="no_op",
            function=function,
            planned_action=planned,
            affected=[f"{chunk_schema}.{chunk_name}"],
            details=f"Chunk is {state}; nothing to do.",
        )
    gated = _gate(
        function=function,
        planned=planned,
        affected=[f"{chunk_schema}.{chunk_name}"],
        warning=warning,
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )
    if gated is not None:
        return gated
    await driver.execute_query(planned, force_readonly=False)
    return TimescaleAdminResult(
        available=True,
        status="applied",
        function=function,
        planned_action=planned,
        affected=[f"{chunk_schema}.{chunk_name}"],
        details=f"{function} completed.",
    )


async def compress_chunk(
    driver: SqlDriver,
    chunk_schema: str,
    chunk_name: str,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> TimescaleAdminResult:
    """Compress one chunk now (the compression policy does this on a schedule)."""
    return await _chunk_op(
        driver,
        function="compress_chunk",
        chunk_schema=chunk_schema,
        chunk_name=chunk_name,
        want_compressed=True,
        warning="compress_chunk rewrites the chunk and takes a lock that blocks writes to it while it runs. "
        "Compressed chunks are read-optimised; use decompress_chunk to reverse it.",
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )


async def decompress_chunk(
    driver: SqlDriver,
    chunk_schema: str,
    chunk_name: str,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> TimescaleAdminResult:
    """Decompress one chunk (uses extra disk and takes a heavy lock)."""
    return await _chunk_op(
        driver,
        function="decompress_chunk",
        chunk_schema=chunk_schema,
        chunk_name=chunk_name,
        want_compressed=False,
        warning="decompress_chunk rewrites the chunk into row storage, needs free disk for the uncompressed data, "
        "and takes a lock that blocks access to the chunk. A compression policy may recompress it later.",
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )


# ---------------------------------------------------------------------------
# reorder_chunk / merge_chunks (autocommit for reorder and concurrent merge)
# ---------------------------------------------------------------------------


async def reorder_chunk(
    database: Database,
    driver: SqlDriver,
    chunk_schema: str,
    chunk_name: str,
    index_schema: str,
    index_name: str,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> TimescaleAdminResult:
    """Physically reorder a chunk along an index (cannot run in a transaction block)."""
    if not await extension_installed(driver, "timescaledb"):
        return _unavailable("reorder_chunk")
    await _require_chunk(driver, chunk_schema, chunk_name)
    _check_identifier(index_schema, "index_schema")
    _check_identifier(index_name, "index_name")
    planned = (
        f"SELECT reorder_chunk({_regclass_literal(chunk_schema, chunk_name)}, "
        f"{_regclass_literal(index_schema, index_name)})"
    )
    gated = _gate(
        function="reorder_chunk",
        planned=planned,
        affected=[f"{chunk_schema}.{chunk_name}"],
        warning="reorder_chunk rewrites the chunk in index order and takes an exclusive lock on it for the duration. "
        "It runs outside a transaction (autocommit) and cannot be rolled back.",
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )
    if gated is not None:
        return gated
    await _run_autocommit(database, planned, timeout_s)
    return TimescaleAdminResult(
        available=True,
        status="applied",
        function="reorder_chunk",
        planned_action=planned,
        affected=[f"{chunk_schema}.{chunk_name}"],
        details="reorder_chunk completed.",
    )


async def merge_chunks(
    database: Database,
    driver: SqlDriver,
    chunk_schema: str,
    chunk_names: list[str],
    *,
    concurrently: bool = False,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> TimescaleAdminResult:
    """Merge two or more adjacent chunks into one.

    ``concurrently=true`` uses ``merge_chunks_concurrently`` (lighter locking,
    autocommit); the default ``merge_chunks`` runs in a transaction.
    """
    function = "merge_chunks_concurrently" if concurrently else "merge_chunks"
    if len(chunk_names) < 2:
        raise TimescaleError("merge_chunks needs at least two chunk names")
    if len(set(chunk_names)) != len(chunk_names):
        raise TimescaleError("merge_chunks chunk names must be distinct")
    if not await extension_installed(driver, "timescaledb"):
        return _unavailable(function)
    for name in chunk_names:
        await _require_chunk(driver, chunk_schema, name)
    array = "ARRAY[" + ", ".join(_regclass_literal(chunk_schema, n) for n in chunk_names) + "]::regclass[]"
    planned = f"CALL {function}({array})"
    affected = [f"{chunk_schema}.{n}" for n in chunk_names]
    gated = _gate(
        function=function,
        planned=planned,
        affected=affected,
        warning=f"{function} rewrites the data of {len(chunk_names)} chunks into a single chunk; the chunks must be "
        "adjacent and uncompressed. Locking is heavy"
        + (
            " (reduced with concurrently=true, which runs in autocommit and cannot be rolled back)."
            if concurrently
            else "."
        ),
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )
    if gated is not None:
        return gated
    if concurrently:
        await _run_autocommit(database, planned, timeout_s)
    else:
        await driver.execute_query(planned, force_readonly=False)
    return TimescaleAdminResult(
        available=True,
        status="applied",
        function=function,
        planned_action=planned,
        affected=affected,
        details=f"{function} completed.",
    )


# ---------------------------------------------------------------------------
# continuous aggregates
# ---------------------------------------------------------------------------


async def refresh_continuous_aggregate(
    database: Database,
    driver: SqlDriver,
    schema: str,
    view: str,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> TimescaleAdminResult:
    """Refresh a continuous aggregate over a window (NULL bounds = the whole range).

    ``refresh_continuous_aggregate`` refuses to run inside a transaction
    block, so this runs on an autocommit connection.
    """
    _check_identifier(schema, "schema")
    _check_identifier(view, "view")
    start = _timestamp_literal(window_start, "window_start")
    end = _timestamp_literal(window_end, "window_end")
    if not await extension_installed(driver, "timescaledb"):
        return _unavailable("refresh_continuous_aggregate")
    await _require_cagg(driver, schema, view)
    force_sql = ", force => TRUE" if force else ""
    planned = f"CALL refresh_continuous_aggregate({_regclass_literal(schema, view)}, {start}, {end}{force_sql})"
    gated = _gate(
        function="refresh_continuous_aggregate",
        planned=planned,
        affected=[f"{schema}.{view}"],
        warning="refresh_continuous_aggregate recomputes the materialised buckets in the window from the source "
        "hypertable now. A full-range or forced refresh can be expensive and I/O heavy. It runs outside a "
        "transaction (autocommit), in batches, so a failure can leave the window partly refreshed.",
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )
    if gated is not None:
        return gated
    await _run_autocommit(database, planned, timeout_s)
    return TimescaleAdminResult(
        available=True,
        status="applied",
        function="refresh_continuous_aggregate",
        planned_action=planned,
        affected=[f"{schema}.{view}"],
        details="refresh_continuous_aggregate completed.",
    )


# ---------------------------------------------------------------------------
# background jobs
# ---------------------------------------------------------------------------


async def run_job(
    driver: SqlDriver,
    job_id: int,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> TimescaleAdminResult:
    """Run a user / policy job immediately, in the calling session."""
    if not await extension_installed(driver, "timescaledb"):
        return _unavailable("run_job")
    name = await _require_user_job(driver, int(job_id))
    planned = f"CALL run_job({int(job_id)})"
    gated = _gate(
        function="run_job",
        planned=planned,
        affected=[f"job {int(job_id)}: {name}"],
        warning="run_job executes the job's procedure right now with this connection's privileges. For a retention "
        "policy job that means chunks are dropped immediately; for a compression job, chunks are compressed.",
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )
    if gated is not None:
        return gated
    await driver.execute_query(planned, force_readonly=False)
    return TimescaleAdminResult(
        available=True,
        status="applied",
        function="run_job",
        planned_action=planned,
        affected=[f"job {int(job_id)}: {name}"],
        details="run_job completed.",
    )


async def alter_job(
    driver: SqlDriver,
    job_id: int,
    *,
    schedule_interval: str | None = None,
    max_runtime: str | None = None,
    max_retries: int | None = None,
    retry_period: str | None = None,
    scheduled: bool | None = None,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> TimescaleAdminResult:
    """Change a user / policy job's schedule, retries or enabled flag.

    The job's ``config`` and procedure cannot be changed through this tool.
    """
    parts: list[str] = []
    if schedule_interval is not None:
        parts.append(f"schedule_interval => {_interval_literal(schedule_interval, 'schedule_interval')}")
    if max_runtime is not None:
        parts.append(f"max_runtime => {_interval_literal(max_runtime, 'max_runtime')}")
    if max_retries is not None:
        if max_retries < -1:
            raise TimescaleError("max_retries must be >= -1 (-1 = unlimited)")
        parts.append(f"max_retries => {int(max_retries)}")
    if retry_period is not None:
        parts.append(f"retry_period => {_interval_literal(retry_period, 'retry_period')}")
    if scheduled is not None:
        parts.append(f"scheduled => {'TRUE' if scheduled else 'FALSE'}")
    if not parts:
        raise TimescaleError("alter_job needs at least one field to change")
    if not await extension_installed(driver, "timescaledb"):
        return _unavailable("alter_job")
    name = await _require_user_job(driver, int(job_id))
    planned = f"SELECT alter_job({int(job_id)}, {', '.join(parts)})"
    gated = _gate(
        function="alter_job",
        planned=planned,
        affected=[f"job {int(job_id)}: {name}"],
        warning="alter_job changes how and when a background job runs (for example disabling a retention or "
        "compression policy, or running it far more often).",
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )
    if gated is not None:
        return gated
    await driver.execute_query(planned, force_readonly=False)
    return TimescaleAdminResult(
        available=True,
        status="applied",
        function="alter_job",
        planned_action=planned,
        affected=[f"job {int(job_id)}: {name}"],
        details="alter_job completed.",
    )


async def delete_job(
    driver: SqlDriver,
    job_id: int,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> TimescaleAdminResult:
    """Delete a user / policy job (prefer the ``remove_*_policy`` tools for policies)."""
    if not await extension_installed(driver, "timescaledb"):
        return _unavailable("delete_job")
    name = await _require_user_job(driver, int(job_id))
    planned = f"SELECT delete_job({int(job_id)})"
    gated = _gate(
        function="delete_job",
        planned=planned,
        affected=[f"job {int(job_id)}: {name}"],
        warning="delete_job permanently removes the job and its schedule. For a policy job, prefer the matching "
        "remove_*_policy tool so the policy bookkeeping stays consistent.",
        dry_run=dry_run,
        confirm=confirm,
        confirmation_token=confirmation_token,
    )
    if gated is not None:
        return gated
    await driver.execute_query(planned, force_readonly=False)
    return TimescaleAdminResult(
        available=True,
        status="applied",
        function="delete_job",
        planned_action=planned,
        affected=[f"job {int(job_id)}: {name}"],
        details="delete_job completed.",
    )


__all__ = [
    "TimescaleAdminResult",
    "alter_job",
    "compress_chunk",
    "decompress_chunk",
    "delete_job",
    "drop_chunks",
    "merge_chunks",
    "refresh_continuous_aggregate",
    "reorder_chunk",
    "require_untenanted",
    "run_job",
]
