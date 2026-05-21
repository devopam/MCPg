"""Fixtures for integration tests that require a live PostgreSQL.

Integration tests run only when ``MCPG_TEST_DATABASE_URL`` points at a
reachable database; otherwise they are skipped. CI provides this via a
PostgreSQL service container; locally, set it to any test database.
"""

import os
from collections.abc import AsyncIterator

import pytest

from mcpg.config import load_settings
from mcpg.database import Database


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
