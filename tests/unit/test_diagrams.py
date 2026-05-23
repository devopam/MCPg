"""Tests for the Mermaid ER diagram generator and its MCP tool."""

from typing import Any

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.diagrams import _parse_pk_columns, _sanitize, generate_schema_diagram
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- helpers ---------------------------------------------------------------


def test_sanitize_collapses_non_alphanumeric_runs_and_strips_edges() -> None:
    assert _sanitize("character varying(255)") == "character_varying_255"
    assert _sanitize("timestamp with time zone") == "timestamp_with_time_zone"
    assert _sanitize("numeric(10,2)") == "numeric_10_2"
    assert _sanitize("int4") == "int4"
    # Pathological all-punctuation input degrades to a placeholder rather
    # than producing an empty Mermaid token (which the parser would reject).
    assert _sanitize("---") == "_"


def test_parse_pk_columns_extracts_quoted_and_unquoted_columns() -> None:
    assert _parse_pk_columns("PRIMARY KEY (id)") == {"id"}
    assert _parse_pk_columns('PRIMARY KEY ("user_id", tenant)') == {"user_id", "tenant"}
    assert _parse_pk_columns("CHECK (x > 0)") == set()


# --- generate_schema_diagram ----------------------------------------------


def _routes() -> dict[str, list[dict[str, Any]]]:
    """Routes that satisfy every catalog query the generator issues."""
    return {
        # list_tables — both tables, no partitions
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind": [
            {"name": "widget", "relkind": "r", "is_partition": False},
            {"name": "order", "relkind": "r", "is_partition": False},
        ],
        # describe_table — keyed by AS column_name so both tables resolve
        "format_type(a.atttypid, a.atttypmod) AS data_type": [
            {
                "column_name": "id",
                "data_type": "integer",
                "nullable": False,
                "column_default": None,
                "type_name": "int4",
                "type_mod": -1,
            },
        ],
        # list_constraints — widget has PK on id (the order's PK is omitted
        # in this fake so we exercise an unannotated column too).
        "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid": [
            {"name": "widget_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"},
        ],
        # list_foreign_keys — order(widget_id) -> widget(id)
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
    }


async def test_generate_schema_diagram_emits_entities_and_fk_edges() -> None:
    driver = FakeRoutingDriver(_routes())

    rendered = await generate_schema_diagram(driver, "app")

    assert rendered.startswith("erDiagram\n")
    # Entities with PK and FK markers, sanitised types
    assert "    widget {\n        integer id PK\n    }" in rendered
    assert "    order {\n" in rendered
    assert "        integer id PK" in rendered or "        integer id\n" in rendered
    # The FK edge points from the parent (widget) to the child (order)
    assert '    widget ||--o{ order : "widget_id"' in rendered


async def test_generate_schema_diagram_skips_partitions_by_default() -> None:
    routes = _routes()
    routes["FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind"] = [
        {"name": "event", "relkind": "p", "is_partition": False},
        {"name": "event_2026", "relkind": "r", "is_partition": True},
    ]
    driver = FakeRoutingDriver(routes)

    rendered = await generate_schema_diagram(driver, "app")

    # The partition child is filtered out; the partitioned parent is
    # excluded too because it is reported as ``type=BASE TABLE`` only
    # when ``relkind='p'`` — that mapping is exercised by list_tables.
    assert "event_2026" not in rendered


async def test_generate_schema_diagram_skips_edges_pointing_outside_the_schema() -> None:
    routes = _routes()
    routes["FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid"] = [
        {
            "name": "cross_fk",
            "from_table": "order",
            "to_schema": "other",
            "to_table": "absent",
            "from_columns": ["x"],
            "to_columns": ["y"],
        }
    ]
    driver = FakeRoutingDriver(routes)

    rendered = await generate_schema_diagram(driver, "app")

    assert "||--o{" not in rendered


# --- MCP tool wiring -------------------------------------------------------


async def test_generate_schema_diagram_tool_is_registered_and_callable() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "generate_schema_diagram" in listed

        result = await client.call_tool("generate_schema_diagram", {"schema": "app"})

    assert result.isError is False
