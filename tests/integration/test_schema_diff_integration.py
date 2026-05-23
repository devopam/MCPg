"""Integration test for compare_schemas against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.schema_diff import compare_schemas

_LEFT = "mcpg_diff_left_it"
_RIGHT = "mcpg_diff_right_it"


@pytest.fixture
async def two_schemas(connected_database: Database) -> AsyncIterator[tuple[str, str]]:
    """Build two real schemas that differ in tables, columns, indexes, and FKs."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_LEFT} CASCADE")
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_RIGHT} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_LEFT}")
    await driver.execute_query(f"CREATE SCHEMA {_RIGHT}")

    # Left: widget + legacy_thing
    await driver.execute_query(f"CREATE TABLE {_LEFT}.widget (id integer PRIMARY KEY, name text)")
    await driver.execute_query(f"CREATE INDEX widget_legacy_idx ON {_LEFT}.widget (name)")
    await driver.execute_query(f"CREATE TABLE {_LEFT}.legacy_thing (id integer PRIMARY KEY)")

    # Right: widget (renamed-shape: name nullable→NOT NULL, plus created_at)
    # + brand_new, and the FK target shape changes (now a unique key).
    await driver.execute_query(
        f"CREATE TABLE {_RIGHT}.widget ("
        f"  id integer PRIMARY KEY, "
        f"  name text NOT NULL, "
        f"  created_at timestamp NOT NULL"
        f")"
    )
    await driver.execute_query(f"CREATE INDEX widget_legacy_idx ON {_RIGHT}.widget (created_at)")  # same name, new def
    await driver.execute_query(f"CREATE TABLE {_RIGHT}.brand_new (id integer PRIMARY KEY)")

    try:
        yield _LEFT, _RIGHT
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_LEFT} CASCADE")
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_RIGHT} CASCADE")


async def test_compare_schemas_reports_real_structural_differences(
    connected_database: Database, two_schemas: tuple[str, str]
) -> None:
    left, right = two_schemas

    diff = await compare_schemas(connected_database.driver(), left, right)

    added_names = {table.name for table in diff.tables_added}
    removed_names = {table.name for table in diff.tables_removed}
    assert "brand_new" in added_names
    assert "legacy_thing" in removed_names

    # widget is the only common table; assert it surfaced as changed
    changed_tables = {td.table: td for td in diff.tables_changed}
    assert "widget" in changed_tables
    widget = changed_tables["widget"]

    # The new column appears in columns_added; "name" is changed (nullable).
    assert "created_at" in {column.name for column in widget.columns_added}
    changed_names = {change.name for change in widget.columns_changed}
    assert "name" in changed_names
    name_change = next(c for c in widget.columns_changed if c.name == "name")
    assert "nullable" in name_change.fields_changed

    # The reused index name resolves to indexes_changed (different definition).
    changed_index_names = {change.name for change in widget.indexes_changed}
    assert "widget_legacy_idx" in changed_index_names
