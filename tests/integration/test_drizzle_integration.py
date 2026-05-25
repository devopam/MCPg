"""Integration tests for the Drizzle ORM schema exporter against real PG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.drizzle import generate_drizzle_schema

_SCHEMA = "mcpg_drizzle_it"


@pytest.fixture
async def drizzle_schema(connected_database: Database) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.status AS ENUM ('active','inactive')")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.owner ("
        "id serial PRIMARY KEY, "
        "name varchar(120) NOT NULL, "
        "created_at timestamptz DEFAULT now())"
    )
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget ("
        "id serial PRIMARY KEY, "
        f"owner_id integer NOT NULL REFERENCES {_SCHEMA}.owner(id), "
        "name text NOT NULL UNIQUE, "
        "quantity integer NOT NULL DEFAULT 0, "
        f"state {_SCHEMA}.status NOT NULL DEFAULT 'active', "
        "extras jsonb)"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_drizzle_schema_emits_valid_ts_for_a_real_schema(
    connected_database: Database, drizzle_schema: str
) -> None:
    ts = await generate_drizzle_schema(connected_database.driver(), drizzle_schema)

    # Import line is present and includes the helpers we actually used.
    assert 'from "drizzle-orm/pg-core"' in ts
    assert "pgTable" in ts
    assert "serial" in ts
    assert "varchar" in ts
    assert "jsonb" in ts
    # Enum const generated and referenced from the column.
    assert 'pgEnum("status"' in ts
    assert "statusEnum" in ts

    # Both tables emitted as pgTable consts with the right shape.
    assert 'export const owner = pgTable("owner"' in ts
    assert 'export const widget = pgTable("widget"' in ts

    # snake_case columns become camelCase on the JS side; the literal
    # column name stays as the call argument.
    assert 'ownerId: integer("owner_id")' in ts
    assert "createdAt: timestamp(" in ts

    # FK chain points at the resolved table.column.
    assert ".references(() => owner.id)" in ts

    # PK + unique + default chains.
    assert ".primaryKey()" in ts
    assert ".unique()" in ts
    assert ".default(0)" in ts
    assert ".defaultNow()" in ts


async def test_generate_drizzle_schema_omits_helpers_that_are_not_referenced(connected_database: Database) -> None:
    """Empty schema → import block contains only pgTable + nothing extra.

    The exporter's helper-collection rule says: only import what we
    actually emit. A schema with no columns emits no type helpers.
    """
    driver = connected_database.driver()
    schema = "mcpg_drizzle_empty_it"
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {schema}")
    try:
        ts = await generate_drizzle_schema(driver, schema)
        # No tables → only the import line + a trailing newline body.
        assert 'from "drizzle-orm/pg-core"' in ts
        # No serial / integer / etc. should be present.
        assert "serial" not in ts
        assert "integer" not in ts
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
