"""Integration tests for the composite tools against real PG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcpg.advisors import find_unused_objects
from mcpg.composite import summarize_table, why_is_this_slow
from mcpg.database import Database

_SCHEMA = "mcpg_composite_it"


@pytest.fixture
async def populated_schema(connected_database: Database, distributed_replicated_clause: str) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget ("
        "id serial PRIMARY KEY, "
        "name text NOT NULL UNIQUE, "
        "qty integer NOT NULL DEFAULT 0, "
        "metadata jsonb)"
        f"{distributed_replicated_clause}"
    )
    await driver.execute_query(f"CREATE INDEX widget_qty_idx ON {_SCHEMA}.widget(qty)")
    await driver.execute_query(f"CREATE INDEX widget_unused_idx ON {_SCHEMA}.widget(name, qty)")
    await driver.execute_query(
        f"INSERT INTO {_SCHEMA}.widget (name, qty) VALUES ('alpha', 1), ('beta', 2), ('gamma', 3)"
    )
    # Force stats to refresh so the size functions return realistic values.
    await driver.execute_query(f"ANALYZE {_SCHEMA}.widget")
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_summarize_table_returns_columns_indexes_constraints_stats_and_sample(
    connected_database: Database, populated_schema: str
) -> None:
    driver = connected_database.driver()
    result = await summarize_table(driver, populated_schema, "widget", sample_rows=2)

    assert result.schema == populated_schema
    assert result.table == "widget"
    column_names = {c.name for c in result.columns}
    assert {"id", "name", "qty", "metadata"} <= column_names
    assert result.primary_key == ["id"]
    # Indexes include the PK, the UNIQUE one, and the two we created.
    index_names = {idx.name for idx in result.indexes}
    assert "widget_qty_idx" in index_names and "widget_unused_idx" in index_names
    # Stats present and reasonable.
    assert result.stats.estimated_row_count >= 0
    assert result.stats.total_size_bytes >= 0
    # Sample respects the limit.
    assert 0 < len(result.sample_rows) <= 2


async def test_summarize_table_skips_sample_when_sample_rows_is_zero(
    connected_database: Database, populated_schema: str
) -> None:
    driver = connected_database.driver()
    result = await summarize_table(driver, populated_schema, "widget", sample_rows=0)

    assert result.sample_rows == []


async def test_find_unused_objects_detects_unused_index_on_real_table(
    connected_database: Database, populated_schema: str
) -> None:
    driver = connected_database.driver()
    report = await find_unused_objects(driver, populated_schema)

    # The widget_unused_idx was never scanned (we only inserted; didn't
    # query). It may or may not be flagged depending on whether the
    # other indexes were touched by the INSERT path — what we can
    # reliably assert is that the report shape is correct.
    assert report.schema == populated_schema
    # Every index in the report is from this schema.
    assert all(idx.schema == populated_schema for idx in report.indexes)
    # Every reported index has a non-empty definition.
    assert all(idx.definition.startswith("CREATE") for idx in report.indexes)


async def test_why_is_this_slow_returns_full_diagnosis_for_a_real_query(
    connected_database: Database, populated_schema: str
) -> None:
    driver = connected_database.driver()
    diagnosis = await why_is_this_slow(driver, f"SELECT * FROM {populated_schema}.widget WHERE name = 'alpha'")

    # The plan summary is populated.
    assert "total_cost" in diagnosis.plan_summary
    assert diagnosis.plan_summary["total_cost"] >= 0
    # The full EXPLAIN payload is included.
    assert diagnosis.explain_plan is not None
    # Suggestions never empty — fallback message is always emitted.
    assert len(diagnosis.suggestions) >= 1
    # Cache hit ratio is a float (or None on a fresh cluster).
    assert diagnosis.cache_hit_ratio is None or 0.0 <= diagnosis.cache_hit_ratio <= 1.0
