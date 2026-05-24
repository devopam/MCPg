"""Integration test for the PG → Prisma schema exporter."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.prisma import generate_prisma_schema

_SCHEMA = "mcpg_prisma_it"


@pytest.fixture
async def prisma_schema(connected_database: Database) -> AsyncIterator[str]:
    """Build a small real schema covering tables, FK, enum, and an index."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.status AS ENUM ('draft', 'live', 'archived')")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget ("
        f"  id serial PRIMARY KEY, "
        f"  name text NOT NULL, "
        f"  state {_SCHEMA}.status NOT NULL DEFAULT 'draft'"
        f")"
    )
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.order_item ("
        f"  id serial PRIMARY KEY, "
        f"  widget_id integer NOT NULL REFERENCES {_SCHEMA}.widget(id), "
        f"  quantity integer NOT NULL DEFAULT 1"
        f")"
    )
    await driver.execute_query(f"CREATE INDEX widget_name_idx ON {_SCHEMA}.widget (name)")
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_prisma_schema_renders_a_real_schema_end_to_end(
    connected_database: Database, prisma_schema: str
) -> None:
    out = await generate_prisma_schema(connected_database.driver(), prisma_schema)

    # Preamble is unconditional.
    assert 'provider = "postgresql"' in out
    assert 'provider = "prisma-client-js"' in out

    # Both models are present.
    assert "model widget {" in out
    assert "model order_item {" in out

    # serial PKs map to Int @id @default(autoincrement()).
    assert "id Int @id @default(autoincrement())" in out

    # The enum block appears and is referenced from the column type.
    assert "enum status {" in out
    assert "state status" in out and '@default("draft")' in out

    # FK renders on both sides with the same named relation.
    assert '@relation("order_item_widget_id_fkey"' in out
    assert "order_item[]" in out

    # The secondary index materialises as @@index.
    assert "@@index([name])" in out
