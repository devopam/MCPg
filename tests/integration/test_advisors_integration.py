"""Integration test for the schema-advisor rules against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.advisors import (
    RULE_DUPLICATE_INDEXES,
    RULE_MISSING_PRIMARY_KEY,
    RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ,
    RULE_UNINDEXED_FOREIGN_KEY,
    run_advisors,
)
from mcpg.database import Database

_SCHEMA = "mcpg_advisors_it"


@pytest.fixture
async def advisors_schema(connected_database: Database) -> AsyncIterator[str]:
    """Build one example violation per advisor rule, plus a clean control table."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    # Clean control — has a PK, no FKs, no duplicates, no nullable
    # timestamps. Should NOT produce any findings.
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.control (id integer PRIMARY KEY, name text NOT NULL)")
    # Violates missing_primary_key.
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.no_pk (note text)")
    # Violates nullable_timestamp_without_tz on created_at.
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.legacy (id integer PRIMARY KEY, created_at timestamp)")
    # Violates unindexed_foreign_key: order_item.control_id references
    # control(id) but has no leading-column index.
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.order_item ("
        f"  id integer PRIMARY KEY, "
        f"  control_id integer NOT NULL REFERENCES {_SCHEMA}.control(id)"
        f")"
    )
    # Violates duplicate_indexes: two btree indexes on widget(name).
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.widget (id integer PRIMARY KEY, name text NOT NULL)")
    await driver.execute_query(f"CREATE INDEX widget_name_a ON {_SCHEMA}.widget (name)")
    await driver.execute_query(f"CREATE INDEX widget_name_b ON {_SCHEMA}.widget (name)")
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_run_advisors_reports_every_seeded_violation(connected_database: Database, advisors_schema: str) -> None:
    report = await run_advisors(connected_database.driver(), advisors_schema)

    by_rule = {rule: [f for f in report.findings if f.rule == rule] for rule in report.rules_run}

    # missing_primary_key — exactly the no_pk table.
    assert {f.object for f in by_rule[RULE_MISSING_PRIMARY_KEY]} == {f"{advisors_schema}.no_pk"}

    # unindexed_foreign_key — the order_item.control_id FK.
    assert any(f.object == f"{advisors_schema}.order_item.control_id" for f in by_rule[RULE_UNINDEXED_FOREIGN_KEY])

    # duplicate_indexes — the widget_name_a / widget_name_b pair.
    duplicate_objects = {f.object for f in by_rule[RULE_DUPLICATE_INDEXES]}
    assert any("widget_name_a" in obj and "widget_name_b" in obj for obj in duplicate_objects)

    # nullable_timestamp_without_tz — legacy.created_at.
    assert any(f.object == f"{advisors_schema}.legacy.created_at" for f in by_rule[RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ])

    # The clean control table must not show up under any rule.
    assert all(advisors_schema + ".control" not in f.object.split(" vs ")[0] for f in report.findings)
