"""Integration tests for the Diesel ORM exporter against real PG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.diesel import generate_diesel_schema

_SCHEMA = "mcpg_diesel_it"


@pytest.fixture
async def diesel_schema(connected_database: Database) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.status AS ENUM ('active','inactive')")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.owner (id serial PRIMARY KEY, name varchar(120) NOT NULL, created_at timestamptz)"
    )
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget ("
        "id serial PRIMARY KEY, "
        f"owner_id integer NOT NULL REFERENCES {_SCHEMA}.owner(id), "
        "name text NOT NULL, "
        f"state {_SCHEMA}.status NOT NULL DEFAULT 'active', "
        "extras jsonb)"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_diesel_schema_emits_valid_rust_for_a_real_schema(
    connected_database: Database, diesel_schema: str
) -> None:
    rs = await generate_diesel_schema(connected_database.driver(), diesel_schema)

    # Enum module emitted.
    assert "pub mod pg_enum {" in rs
    assert "pub enum Status {" in rs
    assert "Active," in rs and "Inactive," in rs

    # Both tables emitted as table! macros with the right PK clause.
    assert "table! {\n    owner (id) {" in rs
    assert "table! {\n    widget (id) {" in rs

    # Column → Diesel SQL type mappings.
    assert "id -> Integer," in rs
    assert "name -> Varchar," in rs  # varchar(120) → Varchar
    assert "created_at -> Nullable<Timestamptz>," in rs  # nullable timestamptz
    assert "extras -> Nullable<Jsonb>," in rs
    # Enum column maps to Text (wrapper-over-Text strategy).
    assert "state -> Text," in rs

    # joinable! for the intra-schema FK.
    assert "joinable!(widget -> owner (owner_id));" in rs

    # allow_tables_to_appear_in_same_query! with sorted names.
    assert "allow_tables_to_appear_in_same_query!(owner, widget);" in rs


async def test_generate_diesel_schema_for_an_empty_schema_emits_no_blocks(
    connected_database: Database,
) -> None:
    driver = connected_database.driver()
    schema = "mcpg_diesel_empty_it"
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {schema}")
    try:
        rs = await generate_diesel_schema(driver, schema)
        # No tables → no table! macros, no joinable, no allow_join.
        assert "table!" not in rs
        assert "joinable!" not in rs
        assert "allow_tables_to_appear_in_same_query!" not in rs
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
