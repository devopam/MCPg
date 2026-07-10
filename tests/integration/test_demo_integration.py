"""Integration tests for the ``mcpg --demo`` dataset lifecycle.

These are the walkthrough's guarantees: every planted teaching moment
in the demo dataset (missing FK index, PII columns, camelCase naming,
searchable review prose) must actually be findable by the pivotal
tools, or docs/demo.md is fiction.
"""

import asyncio

import psycopg
import pytest

from mcpg.advisors import find_sensitive_columns
from mcpg.config import Settings, load_settings
from mcpg.database import Database
from mcpg.demo import DEMO_SCHEMA, DemoError, drop_demo, seed_demo
from mcpg.graph_projection import generate_graph_projection
from mcpg.indexing import recommend_indexes
from mcpg.naming import lint_naming_conventions
from mcpg.query import analyze_query_plan
from mcpg.textsearch import full_text_search

_SLOW_QUERY = f"SELECT * FROM {DEMO_SCHEMA}.orders WHERE customer_id = 42 ORDER BY order_date DESC"


async def _force_drop(database_url: str) -> None:
    """Teardown that never leaves the schema behind, marker or not."""
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {DEMO_SCHEMA} CASCADE")
        await conn.commit()


async def test_demo_lifecycle_and_planted_findings(database_url: str, is_warehousepg: bool) -> None:
    if is_warehousepg:
        pytest.skip("demo dataset targets stock PostgreSQL")
    await _force_drop(database_url)
    try:
        summary = await seed_demo(database_url)
        assert summary.schema == DEMO_SCHEMA
        assert summary.row_counts["customers"] == 400
        assert summary.row_counts["products"] == 120
        assert summary.row_counts["orders"] == 3000
        assert summary.row_counts["reviews"] == 900
        assert summary.row_counts["order_items"] > 3000

        # Re-seeding over an existing demo must refuse, not clobber.
        with pytest.raises(DemoError, match="already exists"):
            await seed_demo(database_url)

        database = Database(_settings(database_url))
        await database.connect()
        driver = database.driver()
        try:
            # Row counts landed and the marker comment proves ownership.
            marker = await driver.execute_query(
                "SELECT obj_description(oid, 'pg_namespace') AS c FROM pg_namespace WHERE nspname = %s",
                params=[DEMO_SCHEMA],
            )
            assert marker and marker[0].cells["c"].startswith("MCPg demo dataset")

            # The vector column tracks whether pgvector is installed.
            has_vector_ext = bool(await driver.execute_query("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            assert summary.vector_column_included == has_vector_ext
            embedding_col = await driver.execute_query(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'products' AND column_name = 'embedding'",
                params=[DEMO_SCHEMA],
            )
            assert bool(embedding_col) == has_vector_ext

            # Planted flaw #1: the un-indexed FK forces a sequential scan.
            plan = await analyze_query_plan(driver, _SLOW_QUERY)
            assert "Seq Scan" in plan.node_types
            assert "orders" in plan.sequential_scans

            # ... and the index advisor catches it once the workload shows
            # up in pg_stat_user_tables (reset first: seeding's FK checks
            # drown the signal with index scans on the PK).
            await driver.execute_query("SELECT pg_stat_reset()")
            for table in ("customers", "products", "orders", "order_items", "reviews"):
                await driver.execute_query(f"ANALYZE {DEMO_SCHEMA}.{table}")  # type: ignore[arg-type]
            for _ in range(8):
                await driver.execute_query(_SLOW_QUERY)  # type: ignore[arg-type]
            orders_rec = None
            for _ in range(30):
                recommendations = await recommend_indexes(driver, min_live_tuples=1000)
                orders_rec = next(
                    (r for r in recommendations if r.schema == DEMO_SCHEMA and r.table == "orders"), None
                )
                if orders_rec is not None:
                    break
                await asyncio.sleep(0.5)
            assert orders_rec is not None, "recommend_indexes never flagged mcpg_demo.orders"
            # The planted flaw is the unindexed FK orders.customer_id; the
            # advisor must actually catch it with a btree recommendation (this
            # is exactly what docs/demo.md promises).
            fk_suggestion = next(
                (s for s in orders_rec.suggestions if s.column == "customer_id"), None
            )
            assert fk_suggestion is not None, "recommend_indexes did not flag the unindexed FK customer_id"
            assert fk_suggestion.index_type == "btree"

            # Planted prose: review text is full-text searchable.
            matches = await full_text_search(driver, DEMO_SCHEMA, "reviews", "review_text", '"battery life"', limit=5)
            assert matches

            # Planted PII + naming violation.
            sensitive = await find_sensitive_columns(driver, DEMO_SCHEMA)
            sensitive_columns = {(c.table, c.column) for c in sensitive.columns}
            assert ("customers", "email") in sensitive_columns
            naming = await lint_naming_conventions(driver, DEMO_SCHEMA)
            assert any("reviewSource" in f.object for f in naming.findings)

            # FK topology projects into a graph (emit-only).
            projection = await generate_graph_projection(driver, DEMO_SCHEMA)
            assert projection.cypher_statements
        finally:
            await database.close()

        drop = await drop_demo(database_url)
        assert drop.dropped
        # A second drop is a no-op, not an error.
        assert not (await drop_demo(database_url)).dropped
    finally:
        await _force_drop(database_url)


async def test_drop_refuses_a_schema_mcpg_did_not_create(database_url: str, is_warehousepg: bool) -> None:
    if is_warehousepg:
        pytest.skip("demo dataset targets stock PostgreSQL")
    await _force_drop(database_url)
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        await conn.execute(f"CREATE SCHEMA {DEMO_SCHEMA}")
        await conn.commit()
    try:
        with pytest.raises(DemoError, match="refusing to drop"):
            await drop_demo(database_url)
    finally:
        await _force_drop(database_url)


def _settings(database_url: str) -> Settings:
    return load_settings({"MCPG_DATABASE_URL": database_url})
