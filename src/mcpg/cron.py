"""pg_cron job-scheduling wrappers.

pg_cron is a background-worker extension that runs scheduled SQL inside
the database. MCPg exposes a thin read tool plus two write-gated tools
that drive the extension's ``cron.schedule()`` / ``cron.unschedule()``
functions. All write tools raise :class:`CronError` when the extension
is not installed; the read tool returns an empty list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed


class CronError(Exception):
    """Raised when a pg_cron operation cannot complete."""


@dataclass(frozen=True, slots=True)
class CronJob:
    """A scheduled pg_cron job.

    ``jobname`` is the user-supplied label, available from pg_cron 1.4
    onwards (the column was added in that release). MCPg targets the
    1.4+ schema; older pg_cron installations will surface as a query
    error rather than silently filling ``None``. ``active`` is ``True``
    when the job will run.
    """

    jobid: int
    schedule: str
    command: str
    database: str
    username: str
    active: bool
    jobname: str | None


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """The outcome of scheduling a new cron job."""

    jobid: int
    name: str


async def list_cron_jobs(driver: SqlDriver) -> list[CronJob]:
    """List the pg_cron jobs registered in the database.

    Returns an empty list when pg_cron is not installed — callers can
    treat absence as "no jobs" rather than a hard error.
    """
    if not await extension_installed(driver, "pg_cron"):
        return []
    rows = await driver.execute_query(
        "SELECT jobid, schedule, command, database, username, active, jobname FROM cron.job ORDER BY jobid",
        force_readonly=True,
    )
    return [
        CronJob(
            jobid=row.cells["jobid"],
            schedule=row.cells["schedule"],
            command=row.cells["command"],
            database=row.cells["database"],
            username=row.cells["username"],
            active=row.cells["active"],
            jobname=row.cells["jobname"],
        )
        for row in rows or []
    ]


async def schedule_cron_job(driver: SqlDriver, name: str, schedule: str, command: str) -> ScheduleResult:
    """Register a new pg_cron job and return the assigned jobid.

    ``schedule`` is a cron expression (e.g. ``"*/5 * * * *"``) or one of
    pg_cron's interval shortcuts (e.g. ``"30 seconds"``). ``command``
    is the SQL to execute, run with the privileges of the connected
    role.

    Raises:
        CronError: pg_cron is not installed, or the scheduling call
            failed (invalid schedule expression, name conflict, etc.).
    """
    if not await extension_installed(driver, "pg_cron"):
        raise CronError("pg_cron extension is not installed in this database")
    rows = await driver.execute_query(
        "SELECT cron.schedule(%s, %s, %s) AS jobid",
        params=[name, schedule, command],
    )
    if not rows:
        raise CronError(f"cron.schedule did not return a jobid for {name!r}")
    return ScheduleResult(jobid=rows[0].cells["jobid"], name=name)


# Tight allowlists for everything that lands inside the COPY TO PROGRAM
# shell string — pg_cron passes the SQL body verbatim, so anything we
# splice in becomes part of a shell command on the database host.
_SAFE_PATH = re.compile(r"\A[A-Za-z0-9_./\-]+\Z")
_ABS_SAFE_PATH = re.compile(r"\A/[A-Za-z0-9_./\-]+\Z")
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
# Single source of truth: the keys are the allowed ``format`` values
# and the values are the matching ``pg_dump`` short flag.
_BACKUP_FORMAT_FLAGS = {"plain": "-Fp", "custom": "-Fc", "tar": "-Ft"}


async def schedule_logical_backup(
    driver: SqlDriver,
    name: str,
    schedule: str,
    destination: str,
    *,
    format: str = "plain",
    schema_only: bool = False,
    compress: bool = False,
    pg_dump_path: str = "pg_dump",
    database: str | None = None,
) -> ScheduleResult:
    """Schedule a recurring ``pg_dump`` via pg_cron + ``COPY TO PROGRAM``.

    Composes :func:`schedule_cron_job` with a ``COPY (SELECT 1) TO
    PROGRAM`` body that invokes ``pg_dump`` on the database host's
    filesystem and writes the dump to ``destination`` on that host.

    ``destination`` must be an absolute POSIX path containing only
    ``[A-Za-z0-9_./-]`` — shell metacharacters are rejected so the
    value cannot escape the ``COPY TO PROGRAM`` single-quoted string.
    ``pg_dump_path`` is constrained the same way (with no absolute-
    path requirement so a bare ``pg_dump`` from ``$PATH`` is allowed).
    ``database`` must be a plain identifier when set.

    ``COPY TO PROGRAM`` is **PostgreSQL-superuser-only**; the connected
    role must hold superuser to run the scheduled job. Otherwise the
    backup will be scheduled successfully but every invocation will
    fail at execution time.

    Raises:
        CronError: pg_cron is not installed, an argument fails
            validation, or the underlying ``cron.schedule`` call fails.
    """
    if not _ABS_SAFE_PATH.match(destination):
        raise CronError(f"destination must be an absolute path containing only [A-Za-z0-9_./-]; got {destination!r}")
    if not _SAFE_PATH.match(pg_dump_path):
        raise CronError(f"pg_dump_path must contain only [A-Za-z0-9_./-]; got {pg_dump_path!r}")
    if database is not None and not _IDENTIFIER.match(database):
        raise CronError(f"database must be a plain identifier; got {database!r}")
    if format not in _BACKUP_FORMAT_FLAGS:
        raise CronError(f"unsupported backup format {format!r}; expected one of {sorted(_BACKUP_FORMAT_FLAGS)}")

    parts = [pg_dump_path, _BACKUP_FORMAT_FLAGS[format]]
    if schema_only:
        parts.append("--schema-only")
    if database is not None:
        parts.extend(["-d", database])
    pipeline = " ".join(parts)
    if compress:
        pipeline += " | gzip"
    pipeline += f" > {destination}"

    sql_command = f"COPY (SELECT 1) TO PROGRAM '{pipeline}'"
    return await schedule_cron_job(driver, name, schedule, sql_command)


async def unschedule_cron_job(driver: SqlDriver, name: str) -> bool:
    """Unschedule a pg_cron job by name.

    Returns the boolean pg_cron returns from ``cron.unschedule(name)``
    — ``True`` when the job existed and was removed.

    Raises:
        CronError: pg_cron is not installed.
    """
    if not await extension_installed(driver, "pg_cron"):
        raise CronError("pg_cron extension is not installed in this database")
    rows = await driver.execute_query(
        "SELECT cron.unschedule(%s) AS removed",
        params=[name],
    )
    return bool(rows and rows[0].cells["removed"])
