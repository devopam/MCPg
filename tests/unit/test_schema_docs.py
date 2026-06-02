"""Tests for the Markdown schema-docs generator and its MCP tool."""

from typing import Any

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.schema_docs import _parse_pk_columns, generate_schema_docs
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- helpers ---------------------------------------------------------------


def test_parse_pk_columns_extracts_quoted_and_unquoted_columns() -> None:
    assert _parse_pk_columns("PRIMARY KEY (id)") == {"id"}
    assert _parse_pk_columns('PRIMARY KEY ("user_id", tenant)') == {"user_id", "tenant"}
    assert _parse_pk_columns("CHECK (x > 0)") == set()


# --- generate_schema_docs --------------------------------------------------


def _routes() -> dict[str, list[dict[str, Any]]]:
    """Routes that satisfy every catalog query the generator issues."""
    return {
        # list_tables — both tables, no partitions
        "relispartition": [
            {"name": "widget", "relkind": "r", "is_partition": False},
            {"name": "order", "relkind": "r", "is_partition": False},
        ],
        # describe_table — keyed by AS column_name so both tables resolve
        "format_type(a.atttypid, a.atttypmod) AS data_type": [
            {
                "column_name": "id",
                "data_type": "integer",
                "nullable": False,
                "column_default": "nextval('widget_id_seq'::regclass)",
                "type_name": "int4",
                "type_mod": -1,
            },
            {
                "column_name": "name",
                "data_type": "character varying(255)",
                "nullable": True,
                "column_default": None,
                "type_name": "varchar",
                "type_mod": 259,
            },
        ],
        # list_constraints
        "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid": [
            {"name": "widget_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"},
        ],
        # list_foreign_keys
        "FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid": [
            {
                "name": "order_widget_fk",
                "from_table": "order",
                "to_schema": "app",
                "to_table": "widget",
                "from_columns": ["widget_id"],
                "to_columns": ["id"],
            },
        ],
        # list_indexes
        "FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_index ix ON ix.indrelid = t.oid": [
            {
                "name": "widget_pkey_idx",
                "method": "btree",
                "definition": "CREATE UNIQUE INDEX widget_pkey_idx ON app.widget USING btree (id)",
                "relkind": "i",
            }
        ],
        # _get_table_comments
        "obj_description(c.oid, 'pg_class')": [
            {"name": "widget", "description": "A table for widgets"},
            {"name": "order", "description": "A table for orders\nwith newlines"},
        ],
        # _get_column_comments
        "col_description(c.oid, a.attnum)": [
            {"table_name": "widget", "column_name": "id", "description": "The unique identifier"},
            {"table_name": "widget", "column_name": "name", "description": "The widget name"},
        ],
        # list_views
        "pg_get_viewdef(c.oid, true)": [
            {"name": "widget_summary", "materialized": False, "definition": "SELECT id, name FROM widget;"}
        ],
        # list_foreign_tables
        "FROM pg_foreign_table ft JOIN pg_class c ON c.oid = ft.ftrelid": [
            {"name": "remote_widget", "server": "widget_server", "options": ["host=remote", "port=5432"]}
        ],
        # list_enums
        "FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace JOIN pg_enum e ON e.enumtypid = t.oid": [
            {"name": "widget_status", "values": ["active", "inactive"]}
        ],
        # SELECT * FROM sample query
        'SELECT * FROM "app"."widget" LIMIT 10': [
            {"id": 1, "name": "Alpha"},
            {"id": 2, "name": "Beta"},
        ],
        'SELECT * FROM "app"."order" LIMIT 10': [],
    }


async def test_generate_schema_docs_renders_tables_views_foreign_enums() -> None:
    driver = FakeRoutingDriver(_routes())

    rendered = await generate_schema_docs(driver, "app")

    assert rendered.startswith("# Schema Reference: app\n")
    assert "This schema contains 2 tables, 1 views, 1 foreign tables, and 1 custom enums." in rendered

    # Table section
    assert "### Table: widget" in rendered
    assert "A table for widgets" in rendered
    assert "| Column | Type | Nullable | Key | Default | Description |" in rendered
    assert "| id | integer | No | PK | nextval('widget_id_seq'::regclass) | The unique identifier |" in rendered
    assert "| name | character varying(255) | Yes |  |  | The widget name |" in rendered
    assert "**Constraints:**" in rendered
    assert "- **widget_pkey** (Primary Key): `PRIMARY KEY (id)`" in rendered
    assert "**Indexes:**" in rendered
    assert (
        "- **widget_pkey_idx** (btree): `CREATE UNIQUE INDEX widget_pkey_idx ON app.widget USING btree (id)`"
    ) in rendered

    # View section
    assert "## Views" in rendered
    assert "### View: widget_summary" in rendered
    assert "SELECT id, name FROM widget;" in rendered

    # Foreign Table section
    assert "## Foreign Tables" in rendered
    assert "### Foreign Table: remote_widget" in rendered
    assert "Server: `widget_server`" in rendered
    assert "Options: `host=remote, port=5432`" in rendered

    # Enum section
    assert "## Custom Enums" in rendered
    assert "### Enum: widget_status" in rendered
    assert "Values: 'active', 'inactive'" in rendered


async def test_generate_schema_docs_empty_schema() -> None:
    driver = FakeRoutingDriver({})

    rendered = await generate_schema_docs(driver, "empty_schema")

    assert "# Schema Reference: empty_schema" in rendered
    assert "contains no tables, views, or custom types" in rendered


async def test_generate_schema_docs_with_samples() -> None:
    driver = FakeRoutingDriver(_routes())

    rendered = await generate_schema_docs(driver, "app", include_samples=True)

    assert "Sample Values" in rendered
    assert "| id | integer | No | PK | nextval('widget_id_seq'::regclass) | The unique identifier | 1, 2 |" in rendered
    assert "| name | character varying(255) | Yes |  |  | The widget name | Alpha, Beta |" in rendered


# --- MCP tool wiring -------------------------------------------------------


async def test_generate_schema_docs_tool_is_registered_and_callable() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "generate_schema_docs" in listed

        result = await client.call_tool("generate_schema_docs", {"schema": "app", "include_samples": True})

    assert result.isError is False
    assert result.content[0].text.startswith("# Schema Reference: app\n")  # type: ignore[union-attr]
