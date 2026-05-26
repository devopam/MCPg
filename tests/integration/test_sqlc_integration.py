"""Integration tests for the sqlc schema exporter against real PG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.sqlc import generate_sqlc_schema

_SCHEMA = "mcpg_sqlc_it"


@pytest.fixture
async def sqlc_schema(connected_database: Database) -> AsyncIterator[str]:
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
        "extras jsonb)"
    )
    await driver.execute_query(f"CREATE INDEX widget_name_lower_idx ON {_SCHEMA}.widget (lower(name))")
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_sqlc_schema_emits_replayable_ddl_for_a_real_schema(
    connected_database: Database, sqlc_schema: str
) -> None:
    sql = await generate_sqlc_schema(connected_database.driver(), sqlc_schema)

    # Header: schema + enum.
    assert f'CREATE SCHEMA IF NOT EXISTS "{sqlc_schema}";' in sql
    assert "CREATE TYPE \"status\" AS ENUM ('active', 'inactive');" in sql

    # Both tables present as qualified CREATE TABLE.
    assert f'CREATE TABLE "{sqlc_schema}"."owner"' in sql
    assert f'CREATE TABLE "{sqlc_schema}"."widget"' in sql

    # PK / FK / UNIQUE constraints land via ALTER TABLE ADD CONSTRAINT.
    assert 'ADD CONSTRAINT "owner_pkey" PRIMARY KEY' in sql
    assert 'ADD CONSTRAINT "widget_pkey" PRIMARY KEY' in sql
    assert "FOREIGN KEY" in sql
    assert f"REFERENCES {sqlc_schema}.owner(id)" in sql or f'REFERENCES "{sqlc_schema}"."owner"' in sql

    # The non-constraint expression index lands as CREATE INDEX.
    assert "widget_name_lower_idx" in sql

    # PK ALTER must appear BEFORE the FK ALTER for the widget table —
    # otherwise replay against an empty DB would fail.
    pk_pos = sql.index('ADD CONSTRAINT "widget_pkey"')
    fk_pos = sql.index("FOREIGN KEY")
    assert pk_pos < fk_pos


async def test_generate_sqlc_schema_for_empty_schema_emits_just_create_schema(
    connected_database: Database,
) -> None:
    driver = connected_database.driver()
    schema = "mcpg_sqlc_empty_it"
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {schema}")
    try:
        sql = await generate_sqlc_schema(driver, schema)
        assert sql.strip() == f'CREATE SCHEMA IF NOT EXISTS "{schema}";'
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
