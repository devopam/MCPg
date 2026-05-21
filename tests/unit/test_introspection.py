"""Tests for schema-introspection queries and their MCP tools."""

from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import (
    ColumnInfo,
    ExtensionInfo,
    IndexInfo,
    SchemaInfo,
    TableInfo,
    describe_table,
    list_extensions,
    list_indexes,
    list_schemas,
    list_tables,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- query logic, exercised with a fake driver -----------------------------


async def test_list_schemas_excludes_system_schemas_by_default() -> None:
    driver = FakeDriver(
        [
            {"schema_name": "public"},
            {"schema_name": "pg_catalog"},
            {"schema_name": "information_schema"},
            {"schema_name": "pg_temp_3"},
            {"schema_name": "app"},
        ]
    )

    assert await list_schemas(driver) == [SchemaInfo("public"), SchemaInfo("app")]


async def test_list_schemas_includes_system_schemas_when_requested() -> None:
    driver = FakeDriver([{"schema_name": "public"}, {"schema_name": "pg_catalog"}])

    assert await list_schemas(driver, include_system=True) == [
        SchemaInfo("public"),
        SchemaInfo("pg_catalog"),
    ]


async def test_list_tables_maps_rows_and_passes_schema_as_a_parameter() -> None:
    driver = FakeDriver([{"table_name": "widget", "table_type": "BASE TABLE"}])

    result = await list_tables(driver, "app")

    assert result == [TableInfo("widget", "BASE TABLE")]
    # The schema must be bound as a parameter, never interpolated into SQL.
    assert driver.calls[0][1] == ["app"]


async def test_describe_table_maps_columns_and_nullability() -> None:
    driver = FakeDriver(
        [
            {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": "0"},
            {"column_name": "note", "data_type": "text", "is_nullable": "YES", "column_default": None},
        ]
    )

    result = await describe_table(driver, "app", "widget")

    assert result == [
        ColumnInfo("id", "integer", nullable=False, default="0"),
        ColumnInfo("note", "text", nullable=True, default=None),
    ]


async def test_list_indexes_maps_rows() -> None:
    driver = FakeDriver([{"indexname": "widget_pkey", "indexdef": "CREATE UNIQUE INDEX widget_pkey ..."}])

    assert await list_indexes(driver, "app", "widget") == [
        IndexInfo("widget_pkey", "CREATE UNIQUE INDEX widget_pkey ...")
    ]


async def test_list_extensions_maps_rows() -> None:
    driver = FakeDriver([{"extname": "plpgsql", "extversion": "1.0"}])

    assert await list_extensions(driver) == [ExtensionInfo("plpgsql", "1.0")]


# --- MCP tool registration -------------------------------------------------

_INTROSPECTION_TOOLS = {
    "list_schemas",
    "list_tables",
    "describe_table",
    "list_indexes",
    "list_extensions",
}


async def test_introspection_tools_are_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}

    assert _INTROSPECTION_TOOLS <= listed


async def test_every_introspection_tool_is_callable_from_a_client() -> None:
    cases: dict[str, tuple[dict[str, str], list[dict[str, object]]]] = {
        "list_schemas": ({}, [{"schema_name": "app"}]),
        "list_tables": ({"schema": "app"}, [{"table_name": "w", "table_type": "BASE TABLE"}]),
        "describe_table": (
            {"schema": "app", "table": "w"},
            [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None}],
        ),
        "list_indexes": ({"schema": "app", "table": "w"}, [{"indexname": "i", "indexdef": "d"}]),
        "list_extensions": ({}, [{"extname": "plpgsql", "extversion": "1.0"}]),
    }

    for name, (args, rows) in cases.items():
        server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver(rows)))  # type: ignore[arg-type]
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(name, args)
        assert result.isError is False, name
