"""Tests for pg_cron job-scheduling wrappers."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.cron import CronError, CronJob, ScheduleResult, list_cron_jobs, schedule_cron_job, unschedule_cron_job
from mcpg.server import create_server

_UNRESTRICTED = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)
_READ_ONLY = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- list_cron_jobs --------------------------------------------------------


async def test_list_cron_jobs_returns_empty_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    assert await list_cron_jobs(driver) == []  # type: ignore[arg-type]


async def test_list_cron_jobs_maps_rows_when_extension_present() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "FROM cron.job": [
                {
                    "jobid": 7,
                    "schedule": "*/5 * * * *",
                    "command": "SELECT 1",
                    "database": "app",
                    "username": "app_owner",
                    "active": True,
                    "jobname": "heartbeat",
                }
            ],
        }
    )

    assert await list_cron_jobs(driver) == [  # type: ignore[arg-type]
        CronJob(7, "*/5 * * * *", "SELECT 1", "app", "app_owner", True, "heartbeat")
    ]


# --- schedule_cron_job -----------------------------------------------------


async def test_schedule_cron_job_returns_assigned_jobid() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "cron.schedule": [{"jobid": 42}],
        }
    )

    result = await schedule_cron_job(driver, "heartbeat", "*/5 * * * *", "SELECT 1")  # type: ignore[arg-type]

    assert result == ScheduleResult(jobid=42, name="heartbeat")


async def test_schedule_cron_job_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(CronError, match="not installed"):
        await schedule_cron_job(driver, "h", "* * * * *", "SELECT 1")  # type: ignore[arg-type]


async def test_schedule_cron_job_raises_when_extension_returns_no_jobid() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "cron.schedule": []})

    with pytest.raises(CronError, match="did not return a jobid"):
        await schedule_cron_job(driver, "h", "* * * * *", "SELECT 1")  # type: ignore[arg-type]


# --- unschedule_cron_job --------------------------------------------------


async def test_unschedule_cron_job_returns_true_when_job_removed() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "cron.unschedule": [{"removed": True}],
        }
    )

    assert await unschedule_cron_job(driver, "heartbeat") is True  # type: ignore[arg-type]


async def test_unschedule_cron_job_returns_false_when_no_row() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "cron.unschedule": [],
        }
    )

    assert await unschedule_cron_job(driver, "heartbeat") is False  # type: ignore[arg-type]


async def test_unschedule_cron_job_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(CronError, match="not installed"):
        await unschedule_cron_job(driver, "h")  # type: ignore[arg-type]


# --- tool wiring -----------------------------------------------------------


async def test_list_cron_jobs_tool_is_registered_in_read_mode() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "list_cron_jobs" in listed


async def test_schedule_tools_require_unrestricted_mode() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "schedule_cron_job" not in listed
    assert "unschedule_cron_job" not in listed


async def test_schedule_tools_are_registered_in_unrestricted_mode() -> None:
    server = create_server(_UNRESTRICTED, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {"schedule_cron_job", "unschedule_cron_job"} <= listed
