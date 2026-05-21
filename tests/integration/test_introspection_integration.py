"""Integration tests for schema introspection against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.introspection import (
    SchemaInfo,
    describe_table,
    list_extensions,
    list_indexes,
    list_schemas,
    list_tables,
)

_SCHEMA = "mcpg_introspection_it"


@pytest.fixture
async def sample_schema(connected_database: Database) -> AsyncIterator[str]:
    """Create a throwaway schema with one table and index; drop it afterwards."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.widget (id integer PRIMARY KEY, name text NOT NULL, note text)")
    await driver.execute_query(f"CREATE INDEX widget_name_idx ON {_SCHEMA}.widget (name)")
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_list_schemas_includes_a_user_schema(connected_database: Database, sample_schema: str) -> None:
    schemas = await list_schemas(connected_database.driver())

    assert SchemaInfo(sample_schema) in schemas


async def test_list_tables_finds_the_table(connected_database: Database, sample_schema: str) -> None:
    tables = await list_tables(connected_database.driver(), sample_schema)

    assert ("widget", "BASE TABLE") in {(table.name, table.type) for table in tables}


async def test_describe_table_returns_typed_columns(connected_database: Database, sample_schema: str) -> None:
    columns = {col.name: col for col in await describe_table(connected_database.driver(), sample_schema, "widget")}

    assert columns["id"].data_type == "integer"
    assert columns["name"].nullable is False
    assert columns["note"].nullable is True


async def test_list_indexes_finds_primary_key_and_secondary_index(
    connected_database: Database, sample_schema: str
) -> None:
    names = {index.name for index in await list_indexes(connected_database.driver(), sample_schema, "widget")}

    assert {"widget_pkey", "widget_name_idx"} <= names


async def test_list_extensions_includes_plpgsql(connected_database: Database) -> None:
    names = {extension.name for extension in await list_extensions(connected_database.driver())}

    assert "plpgsql" in names
