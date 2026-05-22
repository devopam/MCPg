"""Integration tests for live-operations introspection against PostgreSQL."""

from mcpg.database import Database
from mcpg.liveops import ActiveQuery, cancel_query, list_active_queries, terminate_backend

# A PID well outside the range any real backend would use.
_ABSENT_PID = 2147483647


async def test_list_active_queries_against_real_postgres(connected_database: Database) -> None:
    # The querying backend excludes itself, so the result is whatever other
    # clients are doing — possibly nothing. The catalog query is still
    # exercised end to end.
    queries = await list_active_queries(connected_database.driver())

    assert isinstance(queries, list)
    assert all(isinstance(active, ActiveQuery) for active in queries)


async def test_cancel_query_reports_failure_for_an_absent_backend(connected_database: Database) -> None:
    # Cancelling a non-existent PID is a safe no-op that still exercises the
    # pg_cancel_backend round trip.
    result = await cancel_query(connected_database.driver(), _ABSENT_PID)

    assert result.action == "cancel_query"
    assert result.succeeded is False


async def test_terminate_backend_reports_failure_for_an_absent_backend(connected_database: Database) -> None:
    result = await terminate_backend(connected_database.driver(), _ABSENT_PID)

    assert result.action == "terminate_backend"
    assert result.succeeded is False
