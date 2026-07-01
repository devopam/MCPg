"""Fixtures for integration tests that require a live PostgreSQL.

Integration tests run only when ``MCPG_TEST_DATABASE_URL`` points at a
reachable database; otherwise they are skipped. CI provides this via a
PostgreSQL service container; locally, set it to any test database.
"""

import asyncio
import os
import sys
from collections.abc import AsyncIterator

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcpg.config import load_settings
from mcpg.database import Database
from mcpg.warehousepg import get_warehousepg_status


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every test under tests/integration/ as ``integration``."""
    for item in items:
        if "tests/integration/" in item.nodeid:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def database_url() -> str:
    """Return the test database URL, or skip if none is configured."""
    url = os.environ.get("MCPG_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MCPG_TEST_DATABASE_URL is not set; skipping integration test")
    return url


@pytest.fixture
async def connected_database(database_url: str) -> AsyncIterator[Database]:
    """Yield a connected Database, closed on teardown."""
    settings = load_settings({"MCPG_DATABASE_URL": database_url})
    database = Database(settings)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def is_warehousepg(connected_database: Database) -> bool:
    """Whether the connected server is WarehousePG/Greenplum."""
    status = await get_warehousepg_status(connected_database.driver())
    return status.available


@pytest.fixture
async def distributed_replicated_clause(is_warehousepg: bool) -> str:
    """DDL suffix for tables needing >1 independent UNIQUE/PK constraint.

    WarehousePG requires every UNIQUE/PRIMARY KEY constraint on a table to
    be a superset of its distribution key, which two disjoint single-column
    keys can never satisfy under ordinary (sharded) distribution.
    ``DISTRIBUTED REPLICATED`` sidesteps the whole constraint (the table is
    copied to every segment) — the right call for these small, 0-3-row test
    fixtures. A no-op on vanilla PostgreSQL, where the syntax is invalid, so
    this must stay conditional rather than always-on.
    """
    return " DISTRIBUTED REPLICATED" if is_warehousepg else ""
