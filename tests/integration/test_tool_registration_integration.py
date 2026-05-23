"""Integration-level smoke test for MCP tool wiring.

The unit suite verifies tool registration (set membership) and the
dataclass → row mapping in isolation via fakes. This module complements
those by exercising the end-to-end tool surface against a *real*
PostgreSQL: every registered introspection-family tool is invoked
through the in-process MCP client and asserted to succeed.

If a tool errors against real catalog data here, we want to know in CI
across every PG version — that's the gap fakes cannot close.
"""

from collections.abc import AsyncIterator

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.database import Database
from mcpg.extensions import enable_extension
from mcpg.introspection import list_available_extensions
from mcpg.server import create_server

_SCHEMA = "mcpg_wiring_it"


@pytest.fixture
async def wired_schema(connected_database: Database) -> AsyncIterator[tuple[str, bool]]:
    """Build a schema rich enough to feed every introspection tool.

    Returns ``(schema_name, has_postgres_fdw)`` — the FDW objects only
    exist when the contrib extension is available, so the test can skip
    FDW tools cleanly otherwise.
    """
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query("DROP PUBLICATION IF EXISTS mcpg_wiring_pub")
    await driver.execute_query("DROP SERVER IF EXISTS mcpg_wiring_server CASCADE")

    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.widget (id integer PRIMARY KEY, name text NOT NULL)")
    await driver.execute_query(f"CREATE INDEX widget_name_idx ON {_SCHEMA}.widget (name)")
    await driver.execute_query(f"CREATE SEQUENCE {_SCHEMA}.widget_seq")
    await driver.execute_query(f"CREATE VIEW {_SCHEMA}.widget_names AS SELECT name FROM {_SCHEMA}.widget")
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.event (id integer, created date) PARTITION BY RANGE (created)")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.event_2026 PARTITION OF {_SCHEMA}.event "
        f"FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
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
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.status AS ENUM ('draft', 'live')")
    await driver.execute_query(f"CREATE DOMAIN {_SCHEMA}.positive_int AS integer NOT NULL DEFAULT 1 CHECK (VALUE > 0)")
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.address AS (street text, city text)")
    await driver.execute_query(f"CREATE PUBLICATION mcpg_wiring_pub FOR TABLE {_SCHEMA}.widget")

    available = {extension.name for extension in await list_available_extensions(driver)}
    has_fdw = "postgres_fdw" in available
    if has_fdw:
        await enable_extension(driver, "postgres_fdw")
        await driver.execute_query(
            "CREATE SERVER mcpg_wiring_server FOREIGN DATA WRAPPER postgres_fdw "
            "OPTIONS (host 'localhost', dbname 'postgres')"
        )
        await driver.execute_query("CREATE USER MAPPING FOR PUBLIC SERVER mcpg_wiring_server OPTIONS (user 'postgres')")
        await driver.execute_query(
            f"CREATE FOREIGN TABLE {_SCHEMA}.remote_widget (id integer) "
            "SERVER mcpg_wiring_server OPTIONS (schema_name 'public', table_name 'widget')"
        )

    try:
        yield _SCHEMA, has_fdw
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await driver.execute_query("DROP PUBLICATION IF EXISTS mcpg_wiring_pub")
        await driver.execute_query("DROP SERVER IF EXISTS mcpg_wiring_server CASCADE")


# Tools that take only ``schema`` — call them with the wired schema name.
_SCHEMA_ONLY_TOOLS = (
    "list_tables",
    "list_views",
    "list_functions",
    "list_sequences",
    "list_enums",
    "list_domains",
    "list_composite_types",
    "list_foreign_keys",
)

# Tools that take ``schema`` and ``table`` — call them on the widget table.
_SCHEMA_TABLE_TOOLS = (
    "describe_table",
    "list_indexes",
    "list_constraints",
    "list_triggers",
    "list_policies",
    "list_grants",
)


async def test_every_registered_introspection_tool_runs_against_real_postgres(
    connected_database: Database, database_url: str, wired_schema: tuple[str, bool]
) -> None:
    schema, has_fdw = wired_schema
    settings = load_settings({"MCPG_DATABASE_URL": database_url})
    server = create_server(settings, database=connected_database)

    async with create_connected_server_and_client_session(server) as client:
        # No-argument tools — every one must reach the real catalog cleanly.
        for tool in ("list_schemas", "list_roles", "list_extensions", "list_available_extensions", "list_publications"):
            result = await client.call_tool(tool, {})
            assert result.isError is False, tool

        for tool in _SCHEMA_ONLY_TOOLS:
            result = await client.call_tool(tool, {"schema": schema})
            assert result.isError is False, tool

        for tool in _SCHEMA_TABLE_TOOLS:
            result = await client.call_tool(tool, {"schema": schema, "table": "widget"})
            assert result.isError is False, tool

        # list_partitions also needs schema + table — exercise it on the
        # partitioned parent specifically so the partitioned branch runs.
        partitioned = await client.call_tool("list_partitions", {"schema": schema, "table": "event"})
        assert partitioned.isError is False

        # The diagram + diff tools share the introspection surface; smoke
        # them through the same client.
        diagram = await client.call_tool("generate_schema_diagram", {"schema": schema})
        assert diagram.isError is False
        assert diagram.content[0].text.startswith("erDiagram\n")  # type: ignore[union-attr]

        diff = await client.call_tool("compare_schemas", {"left_schema": schema, "right_schema": schema})
        assert diff.isError is False
        assert diff.structuredContent is not None
        assert diff.structuredContent["tables_added"] == []
        assert diff.structuredContent["tables_removed"] == []

        # subscriptions require superuser; we don't assert content, only
        # that the call succeeds — the empty/limited result is valid.
        subs = await client.call_tool("list_subscriptions", {})
        assert subs.isError is False

        if has_fdw:
            for tool, args in (
                ("list_foreign_data_wrappers", {}),
                ("list_foreign_servers", {}),
                ("list_user_mappings", {}),
                ("list_foreign_tables", {"schema": schema}),
            ):
                result = await client.call_tool(tool, args)
                assert result.isError is False, tool
