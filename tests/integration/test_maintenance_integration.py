"""Integration tests for maintenance operations against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.maintenance import MaintenanceResult, run_maintenance

_TABLE = "mcpg_maintenance_it"


@pytest.fixture
async def maintenance_table(connected_database: Database) -> AsyncIterator[str]:
    """Create a throwaway table to run maintenance against; drop it afterwards."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}", force_readonly=False)
    await driver.execute_query(f"CREATE TABLE {_TABLE} (id integer)", force_readonly=False)
    try:
        yield _TABLE
    finally:
        await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}", force_readonly=False)


async def test_run_maintenance_vacuum_against_real_postgres(
    connected_database: Database, maintenance_table: str
) -> None:
    # VACUUM cannot run inside a transaction block; this exercises the
    # autocommit path end to end.
    result = await run_maintenance(connected_database, "vacuum", "public", maintenance_table)

    assert result == MaintenanceResult(operation="vacuum", target=f"public.{maintenance_table}")


async def test_run_maintenance_analyze_against_real_postgres(
    connected_database: Database, maintenance_table: str
) -> None:
    result = await run_maintenance(connected_database, "analyze", "public", maintenance_table)

    assert result.operation == "analyze"
