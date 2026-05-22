"""Integration tests for schema introspection against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.extensions import enable_extension
from mcpg.introspection import (
    SchemaInfo,
    describe_table,
    list_available_extensions,
    list_constraints,
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
    indexes = await list_indexes(connected_database.driver(), sample_schema, "widget")
    by_name = {index.name: index for index in indexes}

    assert {"widget_pkey", "widget_name_idx"} <= by_name.keys()
    # Both sample indexes are plain B-tree; the access method is reported.
    assert by_name["widget_pkey"].method == "btree"
    assert by_name["widget_name_idx"].method == "btree"


async def test_list_constraints_finds_the_primary_key(connected_database: Database, sample_schema: str) -> None:
    constraints = await list_constraints(connected_database.driver(), sample_schema, "widget")

    by_type = {constraint.type for constraint in constraints}
    assert "primary_key" in by_type


async def test_describe_table_reports_pgvector_dimension(connected_database: Database) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "vector" not in available:
        pytest.skip("pgvector is not available on this PostgreSQL server")
    await enable_extension(driver, "vector")
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_vector_it", force_readonly=False)
    await driver.execute_query("CREATE TABLE mcpg_vector_it (id integer, embedding vector(3))", force_readonly=False)
    try:
        columns = {col.name: col for col in await describe_table(driver, "public", "mcpg_vector_it")}
        assert columns["embedding"].vector_dimension == 3
        assert columns["id"].vector_dimension is None
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_vector_it", force_readonly=False)


async def test_list_indexes_reports_pgvector_hnsw_method(connected_database: Database) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "vector" not in available:
        pytest.skip("pgvector is not available on this PostgreSQL server")
    await enable_extension(driver, "vector")
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_hnsw_it", force_readonly=False)
    await driver.execute_query("CREATE TABLE mcpg_hnsw_it (id integer, embedding vector(3))", force_readonly=False)
    await driver.execute_query(
        "CREATE INDEX mcpg_hnsw_idx ON mcpg_hnsw_it USING hnsw (embedding vector_l2_ops)",
        force_readonly=False,
    )
    try:
        indexes = {idx.name: idx for idx in await list_indexes(driver, "public", "mcpg_hnsw_it")}
        assert indexes["mcpg_hnsw_idx"].method == "hnsw"
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_hnsw_it", force_readonly=False)


async def test_list_extensions_includes_plpgsql(connected_database: Database) -> None:
    names = {extension.name for extension in await list_extensions(connected_database.driver())}

    assert "plpgsql" in names


async def test_list_available_extensions_marks_plpgsql_installed(
    connected_database: Database,
) -> None:
    available = await list_available_extensions(connected_database.driver())
    by_name = {extension.name: extension for extension in available}

    assert by_name  # the catalog always lists some available extensions
    assert by_name["plpgsql"].installed is True
