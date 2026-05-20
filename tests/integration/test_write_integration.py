"""Integration tests for write execution against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.write import WriteError, run_ddl, run_write

_TABLE = "mcpg_write_it"


@pytest.fixture
async def write_table(connected_database: Database) -> AsyncIterator[str]:
    """Create a throwaway table for write tests; drop it afterwards."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}", force_readonly=False)
    await driver.execute_query(f"CREATE TABLE {_TABLE} (id integer PRIMARY KEY, label text)", force_readonly=False)
    try:
        yield _TABLE
    finally:
        await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}", force_readonly=False)


async def test_run_write_inserts_and_returns_rows(connected_database: Database, write_table: str) -> None:
    result = await run_write(
        connected_database.driver(),
        f"INSERT INTO {write_table} (id, label) VALUES (1, 'a') RETURNING id, label",
    )

    assert result.rows == [{"id": 1, "label": "a"}]


async def test_run_write_commits_so_a_later_read_sees_the_row(connected_database: Database, write_table: str) -> None:
    await run_write(connected_database.driver(), f"INSERT INTO {write_table} (id) VALUES (2)")

    rows = await connected_database.driver().execute_query(f"SELECT count(*) AS n FROM {write_table}")
    assert rows is not None
    assert rows[0].cells["n"] == 1


async def test_run_write_rejects_ddl_against_a_real_database(connected_database: Database) -> None:
    with pytest.raises(WriteError):
        await run_write(connected_database.driver(), "CREATE TABLE mcpg_nope (id int)")


async def test_run_ddl_creates_a_real_table(connected_database: Database) -> None:
    driver = connected_database.driver()
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_ddl_it", force_readonly=False)
    try:
        await run_ddl(driver, "CREATE TABLE mcpg_ddl_it (id integer PRIMARY KEY)")

        rows = await driver.execute_query("SELECT 1 FROM information_schema.tables WHERE table_name = 'mcpg_ddl_it'")
        assert rows
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_ddl_it", force_readonly=False)
