"""Tests for pg_cron job-scheduling wrappers."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.cron import (
    CronError,
    CronJob,
    ScheduleResult,
    list_cron_jobs,
    schedule_cron_job,
    schedule_logical_backup,
    unschedule_cron_job,
)
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
    assert {"schedule_cron_job", "unschedule_cron_job", "schedule_logical_backup"} <= listed


# --- schedule_logical_backup ----------------------------------------------


async def test_schedule_logical_backup_composes_copy_to_program() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "cron.schedule": [{"jobid": 101}],
        }
    )

    result = await schedule_logical_backup(
        driver,  # type: ignore[arg-type]
        "nightly",
        "0 3 * * *",
        "/var/backups/db.sql",
        "app",
    )

    assert result == ScheduleResult(jobid=101, name="nightly")
    # The cron.schedule call carries the constructed COPY TO PROGRAM body
    # as its third bound parameter — confirm the shape so we don't
    # regress on shell-string construction.
    schedule_calls = [c for c in driver.calls if "cron.schedule" in c[0]]
    assert len(schedule_calls) == 1
    _, params, _ = schedule_calls[0]
    assert params == [
        "nightly",
        "0 3 * * *",
        "COPY (SELECT 1) TO PROGRAM 'pg_dump -Fp -p 5432 -d app > /var/backups/db.sql'",
    ]


async def test_schedule_logical_backup_honours_format_schema_only_compress_and_port() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "cron.schedule": [{"jobid": 102}],
        }
    )

    await schedule_logical_backup(
        driver,  # type: ignore[arg-type]
        "weekly-schema",
        "0 4 * * 0",
        "/var/backups/schema.dump",
        "app-prod",
        format="custom",
        schema_only=True,
        compress=True,
        pg_dump_path="/usr/local/pgsql/bin/pg_dump",
        port=5544,
    )

    expected_command = (
        "COPY (SELECT 1) TO PROGRAM "
        "'/usr/local/pgsql/bin/pg_dump -Fc --schema-only -p 5544 -d app-prod | gzip > /var/backups/schema.dump'"
    )
    schedule_calls = [c for c in driver.calls if "cron.schedule" in c[0]]
    assert schedule_calls[0][1] == ["weekly-schema", "0 4 * * 0", expected_command]


@pytest.mark.parametrize(
    "destination",
    [
        "relative/path.sql",  # not absolute
        "/var/backups/$(rm -rf /)",  # shell substitution
        "/var/backups/file;ls",  # command chain
        "/var/backups/file'; DROP TABLE x; --",  # quote escape
        "/var/backups/file\nrm",  # newline
        "/var/backups/file with space",  # space
        "/var/backups/file|cat",  # pipe
        "",
    ],
)
async def test_schedule_logical_backup_rejects_unsafe_destination(destination: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(CronError, match="destination"):
        await schedule_logical_backup(driver, "n", "* * * * *", destination, "app")  # type: ignore[arg-type]


async def test_schedule_logical_backup_rejects_unsafe_pg_dump_path() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(CronError, match="pg_dump_path"):
        await schedule_logical_backup(
            driver,  # type: ignore[arg-type]
            "n",
            "* * * * *",
            "/var/backups/x.sql",
            "app",
            pg_dump_path="pg_dump; rm -rf /",
        )


@pytest.mark.parametrize(
    "database",
    [
        "app; DROP",  # injection
        "app.prod",  # dot — not a valid pg identifier and ambiguous in -d
        "app/prod",  # slash
        "app prod",  # space
        "-rf",  # leading hyphen would look like a flag to pg_dump
        "",
    ],
)
async def test_schedule_logical_backup_rejects_unsafe_database_name(database: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(CronError, match="database"):
        await schedule_logical_backup(
            driver,  # type: ignore[arg-type]
            "n",
            "* * * * *",
            "/var/backups/x.sql",
            database,
        )


@pytest.mark.parametrize("database", ["app", "app-prod", "app_prod_v2", "App123"])
async def test_schedule_logical_backup_accepts_hyphenated_database_names(database: str) -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "cron.schedule": [{"jobid": 1}],
        }
    )

    await schedule_logical_backup(
        driver,  # type: ignore[arg-type]
        "n",
        "* * * * *",
        "/var/backups/x.sql",
        database,
    )


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
async def test_schedule_logical_backup_rejects_out_of_range_port(port: int) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(CronError, match="port"):
        await schedule_logical_backup(
            driver,  # type: ignore[arg-type]
            "n",
            "* * * * *",
            "/var/backups/x.sql",
            "app",
            port=port,
        )


async def test_schedule_logical_backup_rejects_unsupported_format() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(CronError, match="unsupported backup format"):
        await schedule_logical_backup(
            driver,  # type: ignore[arg-type]
            "n",
            "* * * * *",
            "/var/backups/x.sql",
            "app",
            format="directory",
        )


async def test_schedule_logical_backup_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(CronError, match="not installed"):
        await schedule_logical_backup(driver, "n", "* * * * *", "/var/backups/x.sql", "app")  # type: ignore[arg-type]
