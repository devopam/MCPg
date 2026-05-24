"""Integration test for the SQL audit-trail persistence against real PG."""

from collections.abc import AsyncIterator

import pytest

from mcpg.audit_trail import AUDIT_SCHEMA, list_audit_events
from mcpg.database import Database
from mcpg.write import run_ddl, run_write


@pytest.fixture
async def clean_audit_schema(connected_database: Database) -> AsyncIterator[None]:
    """Drop the audit schema before AND after, so each run starts clean."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {AUDIT_SCHEMA} CASCADE", force_readonly=False)
    try:
        yield None
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {AUDIT_SCHEMA} CASCADE", force_readonly=False)


async def test_audit_trail_persists_run_write_and_run_ddl_invocations(
    connected_database: Database, clean_audit_schema: None
) -> None:
    driver = connected_database.driver()
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_audit_it_widget", force_readonly=False)

    # DDL with schema/table hints — should produce a schema_diff.
    create_result = await run_ddl(
        driver,
        "CREATE TABLE mcpg_audit_it_widget (id integer, name text)",
        audit_persist=True,
        schema="public",
        table="mcpg_audit_it_widget",
    )
    assert create_result.schema_diff is not None
    assert create_result.schema_diff.columns_before == []
    assert {c["name"] for c in create_result.schema_diff.columns_after} == {"id", "name"}

    # ALTER TABLE — diff captures the added column.
    alter_result = await run_ddl(
        driver,
        "ALTER TABLE mcpg_audit_it_widget ADD COLUMN created timestamptz NOT NULL DEFAULT now()",
        audit_persist=True,
        schema="public",
        table="mcpg_audit_it_widget",
    )
    assert alter_result.schema_diff is not None
    before_names = {c["name"] for c in alter_result.schema_diff.columns_before}
    after_names = {c["name"] for c in alter_result.schema_diff.columns_after}
    assert after_names - before_names == {"created"}

    # DML — also persists.
    await run_write(
        driver,
        "INSERT INTO mcpg_audit_it_widget (id, name) VALUES (1, 'first') RETURNING id",
        audit_persist=True,
    )

    events = await list_audit_events(driver, limit=10)

    # The audit table holds one row per invocation, newest first.
    tools = [event.tool for event in events]
    assert "run_write" in tools
    assert "run_ddl" in tools
    # The most recent event should be the run_write (latest INSERT).
    assert tools[0] == "run_write"

    # Tool-name filter narrows the result set.
    only_ddl = await list_audit_events(driver, tool="run_ddl")
    assert len(only_ddl) == 2  # CREATE + ALTER
    assert all(event.tool == "run_ddl" for event in only_ddl)

    # Clean up our integration table (the audit schema is dropped by the fixture).
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_audit_it_widget", force_readonly=False)


async def test_list_audit_events_returns_empty_list_when_persistence_never_ran(
    connected_database: Database, clean_audit_schema: None
) -> None:
    # Fresh DB, no DDL/DML executed with audit_persist=True — the audit
    # schema simply does not exist, and the read returns [].
    assert await list_audit_events(connected_database.driver()) == []
