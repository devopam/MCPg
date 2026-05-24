"""Integration tests for pgvector tuning advisors — gated on the extension."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.extensions import enable_extension
from mcpg.introspection import list_available_extensions
from mcpg.vector_tuning import tune_vector_index, vector_recall_at_k

_TABLE = "mcpg_vector_tuning_it"


@pytest.fixture
async def vector_table(connected_database: Database) -> AsyncIterator[Database]:
    """Build a small pgvector table with an hnsw index; skip if extension absent."""
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "vector" not in available:
        pytest.skip("pgvector is not available on this PostgreSQL server")
    await enable_extension(driver, "vector")
    await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}")
    await driver.execute_query(f"CREATE TABLE {_TABLE} (id integer PRIMARY KEY, embedding vector(3))")
    # Seed a handful of vectors that cluster — the index should agree
    # with brute force on a small dataset of this shape.
    for i, vec in enumerate(
        [
            "[0.1, 0.1, 0.1]",
            "[0.2, 0.2, 0.2]",
            "[0.3, 0.3, 0.3]",
            "[0.9, 0.9, 0.9]",
            "[1.0, 1.0, 1.0]",
            "[5.0, 5.0, 5.0]",
            "[5.1, 5.1, 5.1]",
            "[5.2, 5.2, 5.2]",
        ],
        start=1,
    ):
        await driver.execute_query(
            f"INSERT INTO {_TABLE} (id, embedding) VALUES (%s, %s::vector)",
            params=[i, vec],
        )
    await driver.execute_query(f"CREATE INDEX {_TABLE}_idx ON {_TABLE} USING hnsw (embedding vector_l2_ops)")
    await driver.execute_query(f"ANALYZE {_TABLE}")
    try:
        yield connected_database
    finally:
        await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}")


async def test_tune_vector_index_reads_real_catalog(vector_table: Database) -> None:
    rec = await tune_vector_index(vector_table.driver(), "public", _TABLE, "embedding", index_type="hnsw")

    assert rec.dimension == 3
    assert rec.row_count >= 0  # pg_class.reltuples after ANALYZE is non-negative
    assert rec.parameters == {"m": 16, "ef_construction": 64}
    assert "CREATE INDEX" in rec.create_index_sql
    assert "vector_l2_ops" in rec.create_index_sql


async def test_tune_vector_index_emits_ivfflat_parameters(vector_table: Database) -> None:
    rec = await tune_vector_index(vector_table.driver(), "public", _TABLE, "embedding", index_type="ivfflat")

    assert rec.index_type == "ivfflat"
    assert "lists" in rec.parameters
    assert rec.parameters["lists"] >= 100  # always floored


async def test_vector_recall_at_k_against_real_index(vector_table: Database) -> None:
    report = await vector_recall_at_k(vector_table.driver(), "public", _TABLE, "embedding", "id", k=3, sample_size=4)

    assert report.metric == "l2"
    assert report.k == 3
    assert report.sample_size == 4
    # For 8 well-separated vectors with k=3, the hnsw index should match
    # brute force exactly — recall should be 1.0 (or extremely close).
    assert report.mean_recall >= 0.9
