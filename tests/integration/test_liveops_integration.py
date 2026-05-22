"""Integration tests for live-operations introspection against PostgreSQL."""

from mcpg.database import Database
from mcpg.liveops import ActiveQuery, list_active_queries


async def test_list_active_queries_against_real_postgres(connected_database: Database) -> None:
    # The querying backend excludes itself, so the result is whatever other
    # clients are doing — possibly nothing. The catalog query is still
    # exercised end to end.
    queries = await list_active_queries(connected_database.driver())

    assert isinstance(queries, list)
    assert all(isinstance(active, ActiveQuery) for active in queries)
