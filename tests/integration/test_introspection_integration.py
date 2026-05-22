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
    list_functions,
    list_indexes,
    list_partitions,
    list_schemas,
    list_sequences,
    list_tables,
    list_triggers,
    list_views,
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
    await driver.execute_query(f"CREATE SEQUENCE {_SCHEMA}.widget_seq")
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.event (id integer, created date) PARTITION BY RANGE (created)")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.event_2026 PARTITION OF {_SCHEMA}.event "
        f"FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
    )
    await driver.execute_query(f"CREATE VIEW {_SCHEMA}.widget_names AS SELECT name FROM {_SCHEMA}.widget")
    await driver.execute_query(
        f"CREATE FUNCTION {_SCHEMA}.widget_count() RETURNS bigint LANGUAGE sql "
        f"AS 'SELECT count(*) FROM {_SCHEMA}.widget'"
    )
    await driver.execute_query(
        f"CREATE FUNCTION {_SCHEMA}.widget_touch() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'"
    )
    await driver.execute_query(
        f"CREATE TRIGGER widget_bi BEFORE INSERT ON {_SCHEMA}.widget "
        f"FOR EACH ROW EXECUTE FUNCTION {_SCHEMA}.widget_touch()"
    )
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


async def test_list_views_finds_the_view(connected_database: Database, sample_schema: str) -> None:
    views = await list_views(connected_database.driver(), sample_schema)
    by_name = {view.name: view for view in views}

    assert "widget_names" in by_name
    assert by_name["widget_names"].materialized is False


async def test_list_functions_finds_the_function(connected_database: Database, sample_schema: str) -> None:
    functions = await list_functions(connected_database.driver(), sample_schema)
    by_name = {function.name: function for function in functions}

    assert "widget_count" in by_name
    assert by_name["widget_count"].kind == "function"
    assert by_name["widget_count"].returns == "bigint"


async def test_list_triggers_finds_the_trigger(connected_database: Database, sample_schema: str) -> None:
    triggers = await list_triggers(connected_database.driver(), sample_schema, "widget")
    by_name = {trigger.name: trigger for trigger in triggers}

    assert "widget_bi" in by_name
    assert by_name["widget_bi"].function == "widget_touch"


async def test_list_sequences_finds_the_sequence(connected_database: Database, sample_schema: str) -> None:
    sequences = await list_sequences(connected_database.driver(), sample_schema)
    by_name = {sequence.name: sequence for sequence in sequences}

    assert "widget_seq" in by_name
    assert by_name["widget_seq"].increment == 1


async def test_list_partitions_describes_a_partitioned_table(connected_database: Database, sample_schema: str) -> None:
    result = await list_partitions(connected_database.driver(), sample_schema, "event")

    assert result.partitioned is True
    assert result.strategy == "range"
    assert "event_2026" in {partition.name for partition in result.partitions}


async def test_list_partitions_reports_a_plain_table_as_not_partitioned(
    connected_database: Database, sample_schema: str
) -> None:
    result = await list_partitions(connected_database.driver(), sample_schema, "widget")

    assert result.partitioned is False


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
