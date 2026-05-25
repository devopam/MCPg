"""Integration tests for in-process CSV/JSON export against real PG."""

import json
import shutil
from collections.abc import AsyncIterator

import pytest

from mcpg.data_movement import dump_database, export_query, export_table
from mcpg.database import Database

_SCHEMA = "mcpg_data_movement_it"


@pytest.fixture
async def export_schema(connected_database: Database) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget (id integer PRIMARY KEY, name text NOT NULL, created_at timestamptz)"
    )
    await driver.execute_query(
        f"INSERT INTO {_SCHEMA}.widget (id, name, created_at) "
        "VALUES (1, 'alpha', '2026-05-24T12:00:00Z'), "
        "       (2, 'beta',  '2026-05-24T12:01:00Z'), "
        "       (3, 'gamma, with comma', '2026-05-24T12:02:00Z')"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_export_query_returns_csv_for_a_real_query(connected_database: Database, export_schema: str) -> None:
    result = await export_query(
        connected_database.driver(),
        f"SELECT id, name FROM {export_schema}.widget ORDER BY id",
        format="csv",
    )

    lines = result.content.splitlines()
    assert lines[0] == "id,name"
    assert result.row_count == 3
    assert result.truncated is False
    # The "gamma, with comma" row must be quoted so the CSV is parseable.
    assert '"gamma, with comma"' in result.content


async def test_export_table_returns_json_with_timestamps_stringified(
    connected_database: Database, export_schema: str
) -> None:
    result = await export_table(connected_database.driver(), export_schema, "widget", format="json")

    rows = json.loads(result.content)
    assert {row["name"] for row in rows} == {"alpha", "beta", "gamma, with comma"}
    # The default=str pass means datetime values appear as ISO-ish strings,
    # not objects.
    assert all(isinstance(row["created_at"], str) for row in rows)


async def test_export_query_truncates_at_limit_against_a_large_real_result(
    connected_database: Database, export_schema: str
) -> None:
    result = await export_query(
        connected_database.driver(),
        f"SELECT id FROM {export_schema}.widget ORDER BY id",
        format="csv",
        limit=2,
    )

    assert result.row_count == 2
    assert result.truncated is True
    # The header + 2 data rows == 3 lines.
    assert len(result.content.splitlines()) == 3


# --- dump_database (real pg_dump via the ADR-0004 subprocess gate) -------


async def test_dump_database_runs_real_pg_dump_against_a_live_schema(
    connected_database: Database, export_schema: str
) -> None:
    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump is not on PATH on this runner")

    settings = connected_database._settings
    result = await dump_database(
        settings.database_url,
        timeout_sec=settings.shell_timeout_sec,
        max_output_bytes=settings.shell_max_output_bytes,
        format="plain",
        schema_only=True,
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.output_truncated is False
    # pg_dump's plain SQL preamble always contains this comment; if it
    # made it into our output, the subprocess + env-var path works end-
    # to-end against a real PG.
    assert "PostgreSQL database dump" in result.content
    # The widget table we seeded earlier should appear in the schema dump.
    assert "widget" in result.content
