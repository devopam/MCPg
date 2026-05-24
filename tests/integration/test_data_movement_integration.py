"""Integration tests for in-process CSV/JSON export against real PG."""

import json
from collections.abc import AsyncIterator

import pytest

from mcpg.data_movement import export_query, export_table
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
