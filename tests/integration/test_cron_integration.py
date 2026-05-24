"""Integration tests for pg_cron wrappers — gated on the extension."""

import pytest

from mcpg.cron import list_cron_jobs, schedule_cron_job, unschedule_cron_job
from mcpg.database import Database
from mcpg.extensions import enable_extension
from mcpg.introspection import list_available_extensions


async def test_list_cron_jobs_returns_empty_when_extension_absent(
    connected_database: Database,
) -> None:
    # pg_cron typically requires shared_preload_libraries and isn't on the
    # CI image; the read function must still answer without erroring.
    available = {extension.name for extension in await list_available_extensions(connected_database.driver())}
    if "pg_cron" in available:
        pytest.skip("pg_cron is available — covered by the schedule-roundtrip test")

    assert await list_cron_jobs(connected_database.driver()) == []


async def test_schedule_then_unschedule_roundtrip(connected_database: Database) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "pg_cron" not in available:
        pytest.skip("pg_cron is not available on this PostgreSQL server")
    await enable_extension(driver, "pg_cron")

    job_name = "mcpg_cron_it_heartbeat"
    # Best-effort cleanup if a prior run left the job behind.
    try:
        await unschedule_cron_job(driver, job_name)
    except Exception:
        pass

    scheduled = await schedule_cron_job(driver, job_name, "*/5 * * * *", "SELECT 1")
    try:
        names = {job.jobname for job in await list_cron_jobs(driver)}
        assert job_name in names
        assert scheduled.jobid > 0
    finally:
        removed = await unschedule_cron_job(driver, job_name)
        assert removed is True
