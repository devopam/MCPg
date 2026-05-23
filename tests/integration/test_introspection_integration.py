"""Integration tests for schema introspection against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.extensions import enable_extension
from mcpg.introspection import (
    SchemaInfo,
    describe_table,
    list_available_extensions,
    list_composite_types,
    list_constraints,
    list_domains,
    list_enums,
    list_extensions,
    list_foreign_data_wrappers,
    list_foreign_servers,
    list_foreign_tables,
    list_functions,
    list_grants,
    list_indexes,
    list_partitions,
    list_policies,
    list_publications,
    list_roles,
    list_schemas,
    list_sequences,
    list_subscriptions,
    list_tables,
    list_triggers,
    list_user_mappings,
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
    await driver.execute_query(f"CREATE INDEX event_created_idx ON {_SCHEMA}.event (created)")
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
    await driver.execute_query(f"ALTER TABLE {_SCHEMA}.widget ENABLE ROW LEVEL SECURITY")
    await driver.execute_query(f"CREATE POLICY widget_select ON {_SCHEMA}.widget FOR SELECT USING (true)")
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.status AS ENUM ('draft', 'live', 'archived')")
    await driver.execute_query(f"CREATE DOMAIN {_SCHEMA}.positive_int AS integer NOT NULL DEFAULT 1 CHECK (VALUE > 0)")
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.address AS (street text, city text)")
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_list_schemas_includes_a_user_schema(connected_database: Database, sample_schema: str) -> None:
    schemas = await list_schemas(connected_database.driver())

    assert SchemaInfo(sample_schema) in schemas


async def test_list_tables_finds_the_table(connected_database: Database, sample_schema: str) -> None:
    tables = await list_tables(connected_database.driver(), sample_schema)
    by_name = {table.name: table for table in tables}

    assert by_name["widget"].type == "BASE TABLE"
    assert by_name["widget"].partitioned is False
    assert by_name["event"].partitioned is True
    assert by_name["event_2026"].is_partition is True


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
    assert by_name["widget_pkey"].partitioned is False


async def test_list_indexes_flags_a_partitioned_index(connected_database: Database, sample_schema: str) -> None:
    indexes = await list_indexes(connected_database.driver(), sample_schema, "event")
    by_name = {index.name: index for index in indexes}

    assert by_name["event_created_idx"].partitioned is True


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


async def test_list_policies_finds_the_policy(connected_database: Database, sample_schema: str) -> None:
    result = await list_policies(connected_database.driver(), sample_schema, "widget")

    assert result.rls_enabled is True
    assert "widget_select" in {policy.name for policy in result.policies}


async def test_list_policies_reports_an_unsecured_table(connected_database: Database, sample_schema: str) -> None:
    result = await list_policies(connected_database.driver(), sample_schema, "event")

    assert result.rls_enabled is False
    assert result.policies == []


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


async def test_list_grants_finds_the_owner_privileges(connected_database: Database, sample_schema: str) -> None:
    grants = await list_grants(connected_database.driver(), sample_schema, "widget")

    assert grants  # the table owner always holds privileges on its own table
    assert "SELECT" in {grant.privilege for grant in grants}


async def test_list_roles_includes_a_login_capable_role(connected_database: Database) -> None:
    roles = await list_roles(connected_database.driver())

    assert roles  # every cluster has at least the bootstrap superuser
    assert any(role.can_login for role in roles)


async def test_list_roles_excludes_predefined_roles_by_default(connected_database: Database) -> None:
    roles = await list_roles(connected_database.driver())

    assert not any(role.name.startswith("pg_") for role in roles)


async def test_list_enums_finds_an_enum_with_its_labels(connected_database: Database, sample_schema: str) -> None:
    enums = {enum.name: enum for enum in await list_enums(connected_database.driver(), sample_schema)}

    assert enums["status"].values == ["draft", "live", "archived"]


async def test_list_domains_finds_a_domain_with_its_check(connected_database: Database, sample_schema: str) -> None:
    domains = {domain.name: domain for domain in await list_domains(connected_database.driver(), sample_schema)}

    assert domains["positive_int"].base_type == "integer"
    assert domains["positive_int"].nullable is False
    assert any("VALUE > 0" in constraint for constraint in domains["positive_int"].constraints)


async def test_list_composite_types_excludes_table_row_types(connected_database: Database, sample_schema: str) -> None:
    types = {t.name: t for t in await list_composite_types(connected_database.driver(), sample_schema)}

    assert "address" in types
    assert {attr.name for attr in types["address"].attributes} == {"street", "city"}
    # Tables' implicit row-types must not appear.
    assert "widget" not in types


@pytest.fixture
async def foreign_data_setup(connected_database: Database) -> AsyncIterator[str]:
    """Create a postgres_fdw server, mapping, and foreign table; clean up after."""
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "postgres_fdw" not in available:
        pytest.skip("postgres_fdw is not available on this PostgreSQL server")
    schema = "mcpg_fdw_it"
    await driver.execute_query("DROP SERVER IF EXISTS mcpg_fdw_server CASCADE")
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await enable_extension(driver, "postgres_fdw")
    await driver.execute_query(f"CREATE SCHEMA {schema}")
    await driver.execute_query(
        "CREATE SERVER mcpg_fdw_server FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host 'localhost', dbname 'postgres')"
    )
    await driver.execute_query("CREATE USER MAPPING FOR PUBLIC SERVER mcpg_fdw_server OPTIONS (user 'postgres')")
    await driver.execute_query(
        f"CREATE FOREIGN TABLE {schema}.remote_widget (id integer, name text) "
        "SERVER mcpg_fdw_server OPTIONS (schema_name 'public', table_name 'widget')"
    )
    try:
        yield schema
    finally:
        await driver.execute_query("DROP SERVER IF EXISTS mcpg_fdw_server CASCADE")
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


async def test_list_foreign_data_wrappers_finds_postgres_fdw(
    connected_database: Database, foreign_data_setup: str
) -> None:
    wrappers = {w.name: w for w in await list_foreign_data_wrappers(connected_database.driver())}

    assert "postgres_fdw" in wrappers
    assert wrappers["postgres_fdw"].handler is not None


async def test_list_foreign_servers_finds_the_server_and_options(
    connected_database: Database, foreign_data_setup: str
) -> None:
    servers = {s.name: s for s in await list_foreign_servers(connected_database.driver())}

    assert "mcpg_fdw_server" in servers
    assert servers["mcpg_fdw_server"].wrapper == "postgres_fdw"
    assert servers["mcpg_fdw_server"].options.get("host") == "localhost"


async def test_list_foreign_tables_finds_the_foreign_table(
    connected_database: Database, foreign_data_setup: str
) -> None:
    tables = {t.name: t for t in await list_foreign_tables(connected_database.driver(), foreign_data_setup)}

    assert "remote_widget" in tables
    assert tables["remote_widget"].server == "mcpg_fdw_server"
    assert tables["remote_widget"].options.get("table_name") == "widget"


async def test_list_user_mappings_finds_the_public_mapping(
    connected_database: Database, foreign_data_setup: str
) -> None:
    mappings = await list_user_mappings(connected_database.driver())
    by_server = {(m.user, m.server) for m in mappings}

    assert ("public", "mcpg_fdw_server") in by_server


@pytest.fixture
async def publication_setup(connected_database: Database, sample_schema: str) -> AsyncIterator[str]:
    """Create a logical-replication publication covering the sample widget table."""
    driver = connected_database.driver()
    await driver.execute_query("DROP PUBLICATION IF EXISTS mcpg_widget_pub")
    await driver.execute_query(f"CREATE PUBLICATION mcpg_widget_pub FOR TABLE {sample_schema}.widget")
    try:
        yield "mcpg_widget_pub"
    finally:
        await driver.execute_query("DROP PUBLICATION IF EXISTS mcpg_widget_pub")


async def test_list_publications_finds_the_publication_with_its_table(
    connected_database: Database, publication_setup: str, sample_schema: str
) -> None:
    pubs = {p.name: p for p in await list_publications(connected_database.driver())}

    assert publication_setup in pubs
    assert pubs[publication_setup].all_tables is False
    assert f"{sample_schema}.widget" in pubs[publication_setup].tables
    assert pubs[publication_setup].publishes_insert is True


async def test_list_subscriptions_returns_a_list(connected_database: Database) -> None:
    # Subscriptions need a remote publisher and superuser to read; we only
    # assert the call succeeds and returns a (possibly empty) list — the
    # mapping is exercised in the unit tests.
    subscriptions = await list_subscriptions(connected_database.driver())

    assert isinstance(subscriptions, list)


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
