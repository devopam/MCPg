"""Tests for the token-efficient compact schema introspection tool."""

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import get_compact_schema
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_get_compact_schema_condenses_correctly() -> None:
    # Set up mock routes for FakeRoutingDriver
    driver = FakeRoutingDriver(
        {
            "c.relkind IN ('r', 'p', 'v', 'f')": [
                {"name": "books", "relkind": "r", "is_partition": False},
                {"name": "authors", "relkind": "r", "is_partition": False},
                {"name": "partitions_child", "relkind": "r", "is_partition": True},
            ],
            "format_type(a.atttypid": [
                # books columns
                {
                    "table_name": "books",
                    "column_name": "id",
                    "data_type": "integer",
                    "nullable": False,
                    "type_name": "int4",
                    "type_mod": -1,
                },
                {
                    "table_name": "books",
                    "column_name": "title",
                    "data_type": "character varying",
                    "nullable": True,
                    "type_name": "varchar",
                    "type_mod": -1,
                },
                {
                    "table_name": "books",
                    "column_name": "author_id",
                    "data_type": "integer",
                    "nullable": True,
                    "type_name": "int4",
                    "type_mod": -1,
                },
                # authors columns
                {
                    "table_name": "authors",
                    "column_name": "id",
                    "data_type": "integer",
                    "nullable": False,
                    "type_name": "int4",
                    "type_mod": -1,
                },
                {
                    "table_name": "authors",
                    "column_name": "name",
                    "data_type": "text",
                    "nullable": False,
                    "type_name": "text",
                    "type_mod": -1,
                },
            ],
            "constraint_type = 'PRIMARY KEY'": [
                {"table_name": "books", "column_name": "id"},
                {"table_name": "authors", "column_name": "id"},
            ],
            "c.contype = 'f'": [
                {
                    "name": "books_author_id_fkey",
                    "from_table": "books",
                    "to_schema": "public",
                    "to_table": "authors",
                    "from_columns": ["author_id"],
                    "to_columns": ["id"],
                }
            ],
        }
    )

    result = await get_compact_schema(driver, "public")  # type: ignore[arg-type]

    # Expect:
    # [books] pk:id | id:integer | title:character varying? | author_id:integer?->authors.id
    # [authors] pk:id | id:integer | name:text
    # (partitions_child should be skipped because is_partition is True)
    expected_lines = [
        "[books] pk:id | id:integer | title:character varying? | author_id:integer?->authors.id",
        "[authors] pk:id | id:integer | name:text",
    ]
    assert result.strip().split("\n") == expected_lines


async def test_get_compact_schema_tool_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "get_compact_schema" in listed

        # Calling it with FakeDriver (empty results) should return empty tables message
        result = await client.call_tool("get_compact_schema", {"schema": "public"})

    assert result.isError is False
    assert "no tables" in result.content[0].text
