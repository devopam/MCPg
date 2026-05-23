"""pg_cron job-scheduling wrappers.

pg_cron is a background-worker extension that runs scheduled SQL inside
the database. MCPg exposes a thin read tool plus two write-gated tools
that drive the extension's ``cron.schedule()`` / ``cron.unschedule()``
functions. All write tools raise :class:`CronError` when the extension
is not installed; the read tool returns an empty list.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed


class CronError(Exception):
    """Raised when a pg_cron operation cannot complete."""


@dataclass(frozen=True, slots=True)
class CronJob:
    """A scheduled pg_cron job.

    ``jobname`` is the user-supplied label (PG 14+ only; ``None`` on
    older servers). ``active`` is ``True`` when the job will run.
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
