"""Integration tests for the test-data factory operations against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.test_data import seed_table_with_sample_data

_SCHEMA = "mcpg_test_data_it"
_TABLE = "widget"


@pytest.fixture
async def sample_table(connected_database: Database) -> AsyncIterator[str]:
    """Create a throwaway schema and table to seed data against; drop it afterwards."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE", force_readonly=False)
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}", force_readonly=False)
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.{_TABLE} ("
        "id integer PRIMARY KEY, name text NOT NULL, "
        "score numeric, created_at timestamp)",
        force_readonly=False,
    )
    try:
        yield _TABLE
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE", force_readonly=False)


async def test_seed_table_with_sample_data_against_real_postgres(
    connected_database: Database, sample_table: str
) -> None:
    driver = connected_database.driver()
    result = await seed_table_with_sample_data(
        driver,
        _SCHEMA,
        sample_table,
        rows=5,
        seed=123,
    )

    assert result.rows_seeded == 5
    assert len(result.statements_executed) == 5

    # Query the table to verify the rows were written
    rows = await driver.execute_query(f'SELECT * FROM "{_SCHEMA}"."{sample_table}" ORDER BY id')
    assert len(rows) == 5
    for row in rows:
        assert isinstance(row.cells["id"], int)
        assert isinstance(row.cells["name"], str)
