"""Tests for maintenance operations and the run_maintenance tool."""

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.maintenance import MaintenanceError, MaintenanceResult, run_maintenance
from mcpg.server import create_server


def _database() -> FakeDatabase:
    return FakeDatabase(FakeDriver())


async def test_run_maintenance_issues_vacuum_for_a_table() -> None:
    database = _database()

    result = await run_maintenance(database, "vacuum", "app", "widget")

    assert result == MaintenanceResult(
        operation="vacuum",
        target="app.widget",
        maintenance_sql='VACUUM "app"."widget"',
    )
    assert database.unmanaged == ['VACUUM "app"."widget"']
    # The recorded SQL matches what actually ran — same record-the-SQL
    # invariant create_pg_search_index / create_turboquant_index already
    # hold for their CreateIndexResult / ReindexResult dataclasses.
    assert result.maintenance_sql == database.unmanaged[0]


async def test_run_maintenance_issues_analyze() -> None:
    database = _database()

    await run_maintenance(database, "analyze", "app", "widget")

    assert database.unmanaged == ['ANALYZE "app"."widget"']


async def test_run_maintenance_issues_vacuum_analyze() -> None:
    database = _database()

    await run_maintenance(database, "vacuum_analyze", "app", "widget")

    assert database.unmanaged == ['VACUUM (ANALYZE) "app"."widget"']


async def test_run_maintenance_rejects_an_unknown_operation() -> None:
    with pytest.raises(MaintenanceError, match="unknown operation"):
        await run_maintenance(_database(), "reindex", "app", "widget")


async def test_run_maintenance_quotes_identifiers_to_block_injection() -> None:
    database = _database()

    await run_maintenance(database, "analyze", "app", 'widget"; DROP TABLE x; --')

    # The embedded quote is doubled, so the payload stays inside one identifier.
    assert database.unmanaged == ['ANALYZE "app"."widget""; DROP TABLE x; --"']


async def test_run_maintenance_rejects_an_empty_identifier() -> None:
    with pytest.raises(MaintenanceError, match="invalid identifier"):
        await run_maintenance(_database(), "vacuum", "app", "")


async def test_run_maintenance_wraps_a_database_failure() -> None:
    database = FakeDatabase(FakeDriver(), unmanaged_fail=True)

    with pytest.raises(MaintenanceError, match="maintenance failed"):
        await run_maintenance(database, "vacuum", "app", "widget")


_UNRESTRICTED = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)


async def test_run_maintenance_tool_is_callable_in_unrestricted_mode() -> None:
    server = create_server(_UNRESTRICTED, database=_database())  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("run_maintenance", {"operation": "analyze", "schema": "app", "table": "w"})

    assert result.isError is False
