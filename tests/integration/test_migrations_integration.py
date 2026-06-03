"""Integration tests for the migration shadow workflow (ADR-0006)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.migrations import (
    MigrationError,
    cancel_migration,
    complete_migration,
    list_pending_migrations,
    prepare_migration,
    validate_migration_schema,
)

_SCHEMA = "mcpg_mig_it"


@pytest.fixture
async def target_schema(connected_database: Database) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query("DROP SCHEMA IF EXISTS mcpg_migrations CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget (id integer PRIMARY KEY, name text NOT NULL, created_at timestamptz)"
    )
    await driver.execute_query(f"CREATE INDEX widget_name_idx ON {_SCHEMA}.widget (name)")
    # Reset the per-driver ensure-table cache so each test pays a fresh
    # ensure cost (the schema was just dropped).
    from mcpg import migrations

    migrations._ensure_cache.clear()
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await driver.execute_query("DROP SCHEMA IF EXISTS mcpg_migrations CASCADE")
        # Best-effort drop any leftover shadow schemas.
        rows = await driver.execute_query(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'mcpg_shadow_%'"
        )
        for row in rows or []:
            await driver.execute_query(f'DROP SCHEMA IF EXISTS "{row.cells["schema_name"]}" CASCADE')


async def test_prepare_complete_roundtrip_adds_column_to_target(
    connected_database: Database, target_schema: str
) -> None:
    driver = connected_database.driver()
    result = await prepare_migration(
        driver,
        name="add_qty",
        target_schema=target_schema,
        candidate_sql="ALTER TABLE widget ADD COLUMN quantity integer NOT NULL DEFAULT 0",
    )
    # The structural diff surfaces the added column on the widget table.
    assert len(result.diff.tables_changed) == 1
    table_diff = result.diff.tables_changed[0]
    assert table_diff.table == "widget"
    assert {c.name for c in table_diff.columns_added} == {"quantity"}

    # Shadow schema exists with the migrated structure.
    rows = await driver.execute_query(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema='{result.shadow_schema}' AND table_name='widget' "
        "ORDER BY column_name"
    )
    assert rows is not None
    assert {r.cells["column_name"] for r in rows} == {"id", "name", "created_at", "quantity"}

    # complete_migration applies the candidate to the target and drops the shadow.
    completion = await complete_migration(driver, result.id)
    assert completion.id == result.id

    rows = await driver.execute_query(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema='{target_schema}' AND table_name='widget' "
        "ORDER BY column_name"
    )
    assert rows is not None
    assert {r.cells["column_name"] for r in rows} == {"id", "name", "created_at", "quantity"}

    # Shadow is gone.
    rows = await driver.execute_query(
        f"SELECT schema_name FROM information_schema.schemata WHERE schema_name='{result.shadow_schema}'"
    )
    assert rows == []


async def test_cancel_migration_drops_shadow_and_marks_cancelled(
    connected_database: Database, target_schema: str
) -> None:
    driver = connected_database.driver()
    result = await prepare_migration(
        driver,
        name="bad_idea",
        target_schema=target_schema,
        candidate_sql="ALTER TABLE widget ADD COLUMN extras text",
    )
    outcome = await cancel_migration(driver, result.id)
    assert outcome.shadow_dropped is True

    # Shadow gone, target unchanged.
    rows = await driver.execute_query(
        f"SELECT schema_name FROM information_schema.schemata WHERE schema_name='{result.shadow_schema}'"
    )
    assert rows == []
    rows = await driver.execute_query(
        f"SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema='{target_schema}' AND column_name='extras'"
    )
    assert rows == []


async def test_prepare_migration_rolls_back_shadow_on_bad_candidate_sql(
    connected_database: Database, target_schema: str
) -> None:
    driver = connected_database.driver()
    with pytest.raises(Exception):  # noqa: B017
        await prepare_migration(
            driver,
            name="syntax_err",
            target_schema=target_schema,
            candidate_sql="ALTER TABLE widget THIS IS NOT SQL",
        )
    # No orphan shadow lingers — the failure cleanup dropped it.
    rows = await driver.execute_query(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'mcpg_shadow_syntax_err_%'"
    )
    assert rows == []


async def test_list_pending_returns_prepared_migrations_and_drops_completed_ones(
    connected_database: Database, target_schema: str
) -> None:
    driver = connected_database.driver()
    r1 = await prepare_migration(
        driver,
        name="add_extras",
        target_schema=target_schema,
        candidate_sql="ALTER TABLE widget ADD COLUMN extras text",
    )
    r2 = await prepare_migration(
        driver,
        name="add_flag",
        target_schema=target_schema,
        candidate_sql="ALTER TABLE widget ADD COLUMN flag boolean DEFAULT false",
    )
    pending = await list_pending_migrations(driver)
    ids = {p.id for p in pending}
    assert r1.id in ids and r2.id in ids

    # Cancel one; it should drop out of the prepared list.
    await cancel_migration(driver, r1.id)
    pending = await list_pending_migrations(driver)
    assert r1.id not in {p.id for p in pending}
    assert r2.id in {p.id for p in pending}


async def test_complete_migration_refuses_unknown_or_already_completed_ids(
    connected_database: Database, target_schema: str
) -> None:
    driver = connected_database.driver()
    with pytest.raises(MigrationError, match="not found"):
        await complete_migration(driver, "nonexistent_id_123")

    # Prepare + complete + try to complete again.
    r = await prepare_migration(
        driver,
        name="single_use",
        target_schema=target_schema,
        candidate_sql="ALTER TABLE widget ADD COLUMN extras text",
    )
    await complete_migration(driver, r.id)
    with pytest.raises(MigrationError, match="not in 'prepared'"):
        await complete_migration(driver, r.id)


async def test_validate_migration_schema_compares_applied_candidate_to_reference(
    connected_database: Database, target_schema: str
) -> None:
    driver = connected_database.driver()
    ref_schema = f"{_SCHEMA}_ref"
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {ref_schema} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {ref_schema}")
    try:
        # Create matching structure in reference schema but with the target new column already present
        await driver.execute_query(
            f"CREATE TABLE {ref_schema}.widget ("
            "id integer PRIMARY KEY, name text NOT NULL, "
            "created_at timestamptz, quantity integer NOT NULL DEFAULT 0)"
        )
        await driver.execute_query(f"CREATE INDEX widget_name_idx ON {ref_schema}.widget (name)")

        # Validate target_schema + candidate_sql against ref_schema
        result = await validate_migration_schema(
            driver,
            target_schema=target_schema,
            reference_schema=ref_schema,
            candidate_sql="ALTER TABLE widget ADD COLUMN quantity integer NOT NULL DEFAULT 0",
        )

        assert result.applied is True
        assert result.error is None
        # Since candidate SQL made it match reference schema, the diff should show no changes
        assert len(result.diff.tables_added) == 0
        assert len(result.diff.tables_removed) == 0
        assert len(result.diff.tables_changed) == 0

        # Now test a bad candidate SQL to verify it returns applied=False and error message
        result_fail = await validate_migration_schema(
            driver,
            target_schema=target_schema,
            reference_schema=ref_schema,
            candidate_sql="ALTER TABLE widget THIS IS NOT VALID SQL syntax",
        )
        assert result_fail.applied is False
        assert result_fail.error is not None
        assert result_fail.diff is None
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {ref_schema} CASCADE")
