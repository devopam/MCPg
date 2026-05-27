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
    # Leading and trailing whitespace must not survive as separator underscores.
    assert _sanitize("  text with spaces  ") == "text_with_spaces"


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


async def test_generate_schema_diagram_includes_partitions_when_requested() -> None:
    routes = _routes()
    routes["FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind"] = [
        {"name": "event", "relkind": "p", "is_partition": False},
        {"name": "event_2026", "relkind": "r", "is_partition": True},
    ]
    driver = FakeRoutingDriver(routes)

    rendered = await generate_schema_diagram(driver, "app", include_partitions=True)

    # Both the partitioned parent and the partition itself are drawn.
    assert "    event {\n" in rendered
    assert "    event_2026 {\n" in rendered


async def test_generate_schema_diagram_skips_edges_pointing_to_filtered_partitions() -> None:
    # Same-schema partner of the cross-schema test — the FK target table is
    # filtered out (partition skipped by default), so no edge is emitted
    # even though the FK has to_schema == 'app'.
    routes = _routes()
    routes["FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind"] = [
        {"name": "order", "relkind": "r", "is_partition": False},
        {"name": "event_2026", "relkind": "r", "is_partition": True},
    ]
    routes["FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid"] = [
        {
            "name": "order_event_fk",
            "from_table": "order",
            "to_schema": "app",
            "to_table": "event_2026",
            "from_columns": ["event_id"],
            "to_columns": ["id"],
        }
    ]
    driver = FakeRoutingDriver(routes)

    rendered = await generate_schema_diagram(driver, "app", include_partitions=False)

    assert "||--o{" not in rendered


# --- MCP tool wiring -------------------------------------------------------


async def test_generate_schema_diagram_tool_is_registered_and_callable() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "generate_schema_diagram" in listed

        result = await client.call_tool("generate_schema_diagram", {"schema": "app"})

    assert result.isError is False
    # The wiring should hand the raw Mermaid string straight through; an empty
    # schema still produces the ``erDiagram`` preamble.
    assert result.content[0].text.startswith("erDiagram\n")  # type: ignore[union-attr]


# --- generate_fk_cascade_graph (Phase 8.5) -------------------------------


async def test_generate_fk_cascade_graph_filters_to_cascade_actions_by_default() -> None:
    from mcpg.diagrams import generate_fk_cascade_graph

    driver = FakeRoutingDriver(
        {
            "con.contype = 'f'": [
                # CASCADE FK — included.
                {
                    "fk_name": "fk_orders_user",
                    "from_table": "orders",
                    "to_schema": "public",
                    "to_table": "users",
                    "on_delete": "c",
                    "on_update": "a",
                },
                # NO ACTION FK — excluded by default.
                {
                    "fk_name": "fk_invoices_customer",
                    "from_table": "invoices",
                    "to_schema": "public",
                    "to_table": "customers",
                    "on_delete": "a",
                    "on_update": "a",
                },
            ]
        }
    )

    mermaid = await generate_fk_cascade_graph(driver, "public")  # type: ignore[arg-type]

    assert mermaid.startswith("graph LR")
    assert "orders" in mermaid
    assert "users" in mermaid
    assert "DEL CASCADE" in mermaid
    # NO ACTION FK was excluded; its tables should NOT be in the graph.
    assert "invoices" not in mermaid
    assert "customers" not in mermaid


async def test_generate_fk_cascade_graph_include_all_draws_no_action_fks_too() -> None:
    from mcpg.diagrams import generate_fk_cascade_graph

    driver = FakeRoutingDriver(
        {
            "con.contype = 'f'": [
                {
                    "fk_name": "fk_invoices_customer",
                    "from_table": "invoices",
                    "to_schema": "public",
                    "to_table": "customers",
                    "on_delete": "a",
                    "on_update": "a",
                },
            ]
        }
    )

    mermaid = await generate_fk_cascade_graph(driver, "public", include_all=True)  # type: ignore[arg-type]

    assert "invoices" in mermaid
    assert "customers" in mermaid
    assert "NO ACTION" in mermaid


async def test_generate_fk_cascade_graph_emits_placeholder_when_no_cascades() -> None:
    from mcpg.diagrams import generate_fk_cascade_graph

    driver = FakeRoutingDriver({"con.contype = 'f'": []})

    mermaid = await generate_fk_cascade_graph(driver, "public")  # type: ignore[arg-type]

    assert "no cascade foreign keys" in mermaid


async def test_generate_fk_cascade_graph_prefixes_cross_schema_targets() -> None:
    from mcpg.diagrams import generate_fk_cascade_graph

    driver = FakeRoutingDriver(
        {
            "con.contype = 'f'": [
                {
                    "fk_name": "fk_app_audit",
                    "from_table": "events",
                    "to_schema": "audit",
                    "to_table": "log",
                    "on_delete": "c",
                    "on_update": "a",
                },
            ]
        }
    )

    mermaid = await generate_fk_cascade_graph(driver, "public")  # type: ignore[arg-type]

    # Cross-schema target rendered with its schema prefix.
    assert "audit.log" in mermaid
