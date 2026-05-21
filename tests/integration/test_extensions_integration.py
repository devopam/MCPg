"""Integration tests for extension management against a live PostgreSQL."""

import pytest

from mcpg.database import Database
from mcpg.extensions import ExtensionError, enable_extension
from mcpg.introspection import list_available_extensions, list_extensions


async def test_enable_extension_installs_pg_trgm(connected_database: Database) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "pg_trgm" not in available:
        pytest.skip("pg_trgm is not available on this PostgreSQL server")

    await enable_extension(driver, "pg_trgm")

    installed = {extension.name for extension in await list_extensions(driver)}
    assert "pg_trgm" in installed


async def test_enable_extension_rejects_an_unknown_extension(connected_database: Database) -> None:
    with pytest.raises(ExtensionError, match="allowlist"):
        await enable_extension(connected_database.driver(), "definitely_not_a_real_extension")
