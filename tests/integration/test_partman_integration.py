"""Integration test for pg_partman wrappers — gated on the extension."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.extensions import enable_extension
from mcpg.introspection import list_available_extensions
from mcpg.partman import partman_create_parent, partman_run_maintenance

_SCHEMA = "mcpg_partman_it"


@pytest.fixture
async def partman_schema(connected_database: Database) -> AsyncIterator[Database]:
    """Build a partitioned table pg_partman can manage; skip if extension absent."""
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "pg_partman" not in available:
        pytest.skip("pg_partman is not available on this PostgreSQL server")
    await enable_extension(driver, "pg_partman")
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.event (  id bigserial,   created timestamp NOT NULL) PARTITION BY RANGE (created)"
    )
    try:
        yield connected_database
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_partman_create_parent_and_run_maintenance(partman_schema: Database) -> None:
    driver = partman_schema.driver()
    parent = f"{_SCHEMA}.event"

    created = await partman_create_parent(driver, parent, "created", "1 day")
    assert created.parent_table == parent

    # Maintenance on a freshly-created parent should succeed without raising.
    swept = await partman_run_maintenance(driver, parent)
    assert swept.parent_table == parent
