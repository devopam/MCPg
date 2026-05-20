"""Integration tests for the database lifecycle against a live PostgreSQL.

The connection-failure path is covered by a fast unit test in
``tests/unit/test_database.py``; these tests exercise real-database success
behaviour only.
"""

from mcpg.database import Database


async def test_connects_to_a_real_postgres(connected_database: Database) -> None:
    assert connected_database.is_connected is True


async def test_driver_executes_a_query(connected_database: Database) -> None:
    rows = await connected_database.driver().execute_query("SELECT 1 AS one")

    assert rows is not None
    assert rows[0].cells["one"] == 1


async def test_close_then_reports_disconnected(connected_database: Database) -> None:
    await connected_database.close()
    assert connected_database.is_connected is False
