"""Integration tests for index recommendations against a live PostgreSQL."""

from mcpg.database import Database
from mcpg.indexing import recommend_indexes


async def test_recommend_indexes_against_real_postgres(connected_database: Database) -> None:
    # min_live_tuples=0 makes any sequentially-scanned user table eligible, so
    # the catalog query is genuinely exercised regardless of test-db contents.
    recommendations = await recommend_indexes(connected_database.driver(), min_live_tuples=0)

    assert isinstance(recommendations, list)
