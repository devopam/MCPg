"""Integration tests for fuzzy text search against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.extensions import enable_extension
from mcpg.introspection import list_available_extensions
from mcpg.textsearch import full_text_search, fuzzy_search

_TABLE = "mcpg_trgm_it"


@pytest.fixture
async def trigram_table(connected_database: Database) -> AsyncIterator[str]:
    """Enable pg_trgm and create a small text table; drop it afterwards."""
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "pg_trgm" not in available:
        pytest.skip("pg_trgm is not available on this PostgreSQL server")
    await enable_extension(driver, "pg_trgm")
    await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}", force_readonly=False)
    await driver.execute_query(f"CREATE TABLE {_TABLE} (name text)", force_readonly=False)
    await driver.execute_query(
        f"INSERT INTO {_TABLE} (name) VALUES ('alice'), ('alicia'), ('bob')",
        force_readonly=False,
    )
    try:
        yield _TABLE
    finally:
        await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}", force_readonly=False)


async def test_fuzzy_search_ranks_real_rows_by_similarity(connected_database: Database, trigram_table: str) -> None:
    result = await fuzzy_search(connected_database.driver(), "public", trigram_table, "name", "alice")

    assert result.available is True
    values = [match.value for match in result.matches]
    assert "alice" in values
    assert "bob" not in values
    # Matches are ordered by descending similarity.
    assert result.matches == sorted(result.matches, key=lambda m: m.score, reverse=True)


async def test_full_text_search_against_real_postgres(connected_database: Database) -> None:
    driver = connected_database.driver()
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_fts_it", force_readonly=False)
    await driver.execute_query("CREATE TABLE mcpg_fts_it (body text)", force_readonly=False)
    await driver.execute_query(
        "INSERT INTO mcpg_fts_it (body) VALUES ('the quick brown fox'), ('a lazy dog sleeps'), ('foxes are clever')",
        force_readonly=False,
    )
    try:
        matches = await full_text_search(driver, "public", "mcpg_fts_it", "body", "fox")

        values = [match.value for match in matches]
        assert "the quick brown fox" in values
        assert "a lazy dog sleeps" not in values
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_fts_it", force_readonly=False)
