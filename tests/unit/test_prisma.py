"""Tests for the PostgreSQL → Prisma schema exporter."""

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeParamRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import ColumnInfo
from mcpg.prisma import (
    PrismaError,
    _parse_pk_columns,
    _prisma_default,
    _prisma_type,
    _strip_type_parameters,
    generate_prisma_schema,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- pure helpers ----------------------------------------------------------


def test_strip_type_parameters_handles_modifiers_and_array() -> None:
    assert _strip_type_parameters("integer") == "integer"
    assert _strip_type_parameters("character varying(255)") == "character varying"
    assert _strip_type_parameters("numeric(10,2)") == "numeric"
    assert _strip_type_parameters("vector(384)") == "vector"
    assert _strip_type_parameters("text[]") == "text"
    assert _strip_type_parameters("INTEGER") == "integer"
    # User-defined types come back schema-qualified from format_type when
    # the schema is not on the search_path — the schema prefix is stripped.
    assert _strip_type_parameters("mcpg_prisma_it.status") == "status"


def test_parse_pk_columns_extracts_quoted_and_unquoted() -> None:
    assert _parse_pk_columns("PRIMARY KEY (id)") == ["id"]
    assert _parse_pk_columns('PRIMARY KEY ("user_id", tenant)') == ["user_id", "tenant"]
    assert _parse_pk_columns("CHECK (x > 0)") == []


def _column(
    name: str,
    data_type: str,
    *,
    nullable: bool = False,
    default: str | None = None,
    vector_dimension: int | None = None,
) -> ColumnInfo:
    return ColumnInfo(
        name=name,
        data_type=data_type,
        nullable=nullable,
        default=default,
        vector_dimension=vector_dimension,
    )


def test_prisma_type_maps_common_scalars() -> None:
    assert _prisma_type(_column("a", "integer"), set()) == "Int"
    assert _prisma_type(_column("a", "bigint"), set()) == "BigInt"
    assert _prisma_type(_column("a", "text"), set()) == "String"
    assert _prisma_type(_column("a", "boolean"), set()) == "Boolean"
    assert _prisma_type(_column("a", "jsonb"), set()) == "Json"
    assert _prisma_type(_column("a", "timestamp with time zone"), set()) == "DateTime"
    assert _prisma_type(_column("a", "uuid"), set()) == "String"
    assert _prisma_type(_column("a", "bytea"), set()) == "Bytes"


def test_prisma_type_strips_modifiers_for_lookup() -> None:
    assert _prisma_type(_column("a", "character varying(255)"), set()) == "String"
    assert _prisma_type(_column("a", "numeric(10,2)"), set()) == "Decimal"


def test_prisma_type_handles_array_suffix() -> None:
    assert _prisma_type(_column("tags", "text[]"), set()) == "String[]"


def test_prisma_type_resolves_enum_columns_to_the_enum_name() -> None:
    assert _prisma_type(_column("status", "status"), {"status"}) == "status"


def test_prisma_type_resolves_schema_qualified_enum_columns_to_the_bare_name() -> None:
    # format_type emits qualified names when the type's schema isn't on
    # the search_path — Prisma can only reference the bare enum name.
    assert _prisma_type(_column("status", "mcpg_it.status"), {"status"}) == "status"


def test_prisma_type_renders_enum_arrays_as_enum_lists() -> None:
    assert _prisma_type(_column("tags", "status[]"), {"status"}) == "status[]"


def test_prisma_type_falls_back_to_unsupported_for_unknown_pg_types() -> None:
    assert _prisma_type(_column("embedding", "vector(384)"), set()) == 'Unsupported("vector(384)")'


def test_prisma_type_escapes_embedded_quotes_in_unsupported_fallback() -> None:
    # A pathological type name with quotes would otherwise break the
    # generated Prisma string literal; ensure they're escaped.
    rendered = _prisma_type(_column("x", 'weird"name'), set())
    assert rendered == 'Unsupported("weird\\"name")'


def test_prisma_default_maps_sequences_to_autoincrement() -> None:
    assert _prisma_default("nextval('widgets_id_seq'::regclass)") == "autoincrement()"


def test_prisma_default_maps_timestamps_and_uuid_generators() -> None:
    assert _prisma_default("now()") == "now()"
    assert _prisma_default("CURRENT_TIMESTAMP") == "now()"
    assert _prisma_default("gen_random_uuid()") == "uuid()"
    assert _prisma_default("uuid_generate_v4()") == "uuid()"


def test_prisma_default_maps_literals_with_un_doubled_quotes() -> None:
    assert _prisma_default("'draft'::text") == '"draft"'
    assert _prisma_default("'it''s'::text") == '"it\'s"'
    assert _prisma_default("42") == "42"
    assert _prisma_default("-3.14") == "-3.14"
    assert _prisma_default("true") == "true"
    assert _prisma_default("false") == "false"


def test_prisma_default_returns_none_for_unknown_expressions() -> None:
    assert _prisma_default(None) is None
    assert _prisma_default("some_function(1, 2)") is None
    assert _prisma_default("'unterminated") is None  # no ::cast, no match


# --- full generate_prisma_schema via parameter-routing fake driver ---------


def _column_row(
    name: str,
    data_type: str = "integer",
    *,
    nullable: bool = False,
    default: str | None = None,
    type_name: str = "int4",
    type_mod: int = -1,
) -> dict[str, Any]:
    return {
        "column_name": name,
        "data_type": data_type,
        "nullable": nullable,
        "column_default": default,
        "type_name": type_name,
        "type_mod": type_mod,
    }


_LIST_TABLES = "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind"
_DESCRIBE = "format_type(a.atttypid, a.atttypmod) AS data_type"
_LIST_INDEXES = "FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_index"
_LIST_CONSTRAINTS = "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid"
_LIST_FKS = "FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid"
_LIST_ENUMS = "FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace JOIN pg_enum"


async def test_generate_prisma_schema_renders_models_with_pk_fk_and_back_relation() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [
            {"name": "widget", "relkind": "r", "is_partition": False},
            {"name": "order_item", "relkind": "r", "is_partition": False},
        ],
        (_DESCRIBE, ("app", "widget")): [
            _column_row("id", "integer", nullable=False, default="nextval('widgets_id_seq'::regclass)"),
            _column_row("name", "text", nullable=False, type_name="text"),
        ],
        (_DESCRIBE, ("app", "order_item")): [
            _column_row("id", "integer", nullable=False, default="nextval('order_id_seq'::regclass)"),
            _column_row("widget_id", "integer", nullable=False),
        ],
        (_LIST_CONSTRAINTS, ("app", "widget")): [
            {"name": "widget_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}
        ],
        (_LIST_CONSTRAINTS, ("app", "order_item")): [
            {"name": "order_item_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}
        ],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [
            {
                "name": "order_widget_fk",
                "from_table": "order_item",
                "to_schema": "app",
                "to_table": "widget",
                "from_columns": ["widget_id"],
                "to_columns": ["id"],
            }
        ],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    # Datasource + generator preamble is always present.
    assert 'provider = "postgresql"' in out
    assert 'provider = "prisma-client-js"' in out

    # Models include the PK marker on a single-column PK and an
    # autoincrement default lifted from nextval(...).
    assert "model widget {" in out
    assert "id Int @id @default(autoincrement())" in out
    assert "name String" in out

    # Outgoing FK on the child side renders the relation field;
    # back-relation appears on the parent side.
    assert "model order_item {" in out
    assert "widget_id Int" in out
    assert '@relation("order_widget_fk", fields: [widget_id], references: [id])' in out
    assert 'order_item[] @relation("order_widget_fk")' in out


async def test_generate_prisma_schema_emits_enum_blocks_and_uses_them_in_columns() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [{"name": "post", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, ("app", "post")): [
            _column_row("id", "integer", nullable=False),
            _column_row("status", "status", nullable=False, type_name="status"),
        ],
        (_LIST_CONSTRAINTS, ("app", "post")): [
            {"name": "post_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}
        ],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [],
        (_LIST_ENUMS, ("app",)): [{"name": "status", "values": ["draft", "live", "archived"]}],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    assert "status status" in out  # field references the enum type
    assert "enum status {" in out
    assert "  draft" in out and "  live" in out and "  archived" in out


async def test_generate_prisma_schema_renders_single_column_unique_as_field_attribute() -> None:
    # Single-column UNIQUE should fold into the field as ``@unique`` and
    # NOT generate a separate ``@@unique([name])`` block.
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [{"name": "project", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, ("app", "project")): [
            _column_row("id", "integer"),
            _column_row("name", "text", type_name="text"),
        ],
        (_LIST_CONSTRAINTS, ("app", "project")): [
            {"name": "project_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"},
            {"name": "project_name_key", "type_code": "u", "definition": "UNIQUE (name)"},
        ],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    assert "name String @unique" in out
    assert "@@unique([name])" not in out


async def test_generate_prisma_schema_renders_nullable_array_without_optional_marker() -> None:
    # Prisma's SQL data model treats arrays as inherently optional (empty
    # == null), so even a nullable text[] column must render as
    # ``String[]`` rather than ``String[]?`` — Prisma rejects the latter.
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [{"name": "post", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, ("app", "post")): [
            _column_row("id", "integer"),
            _column_row("tags", "text[]", nullable=True, type_name="text"),
        ],
        (_LIST_CONSTRAINTS, ("app", "post")): [
            {"name": "post_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}
        ],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    assert "tags String[]" in out
    assert "String[]?" not in out


async def test_generate_prisma_schema_skips_expression_indexes_and_emits_unique_secondary() -> None:
    # _render_index should:
    # - de-duplicate the PK-backing index (already covered above)
    # - render a non-constraint UNIQUE INDEX as @@unique([cols])
    # - skip an expression-based index (lower(name)) since the indexed
    #   "column" isn't a bare identifier.
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [{"name": "widget", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, ("app", "widget")): [
            _column_row("id", "integer"),
            _column_row("name", "text", type_name="text"),
        ],
        (_LIST_CONSTRAINTS, ("app", "widget")): [
            {"name": "widget_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}
        ],
        (_LIST_INDEXES, ("app", "widget")): [
            {
                "name": "widget_name_key",
                "method": "btree",
                "relkind": "i",
                "definition": "CREATE UNIQUE INDEX widget_name_key ON app.widget USING btree (name)",
            },
            {
                "name": "widget_lower_name_idx",
                "method": "btree",
                "relkind": "i",
                "definition": "CREATE INDEX widget_lower_name_idx ON app.widget USING btree (lower(name))",
            },
        ],
        (_LIST_FKS, ("app",)): [],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    assert "@@unique([name])" in out
    # Expression index isn't representable as @@index — must be dropped.
    assert "lower(name)" not in out
    assert "@@index([lower" not in out


async def test_generate_prisma_schema_rejects_foreign_key_constraint_with_invalid_name() -> None:
    # FK constraint names are interpolated into Prisma text; an invalid
    # one (spaces, quotes, etc.) must fail up-front instead of producing
    # broken DSL.
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [
            {"name": "widget", "relkind": "r", "is_partition": False},
            {"name": "order_item", "relkind": "r", "is_partition": False},
        ],
        (_DESCRIBE, ("app", "widget")): [_column_row("id", "integer")],
        (_DESCRIBE, ("app", "order_item")): [_column_row("id", "integer"), _column_row("widget_id", "integer")],
        (_LIST_CONSTRAINTS, None): [],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [
            {
                "name": "weird name with spaces",
                "from_table": "order_item",
                "to_schema": "app",
                "to_table": "widget",
                "from_columns": ["widget_id"],
                "to_columns": ["id"],
            }
        ],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    with pytest.raises(PrismaError, match="invalid relation"):
        await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]


async def test_generate_prisma_schema_rejects_fk_name_colliding_with_a_column() -> None:
    # If a FK constraint shares a name with an existing column in the
    # same table, the relation field would shadow the scalar field —
    # Prisma rejects models with duplicate field names. Surface it as a
    # hard error so the agent renames the constraint.
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [
            {"name": "widget", "relkind": "r", "is_partition": False},
            {"name": "order_item", "relkind": "r", "is_partition": False},
        ],
        (_DESCRIBE, ("app", "widget")): [_column_row("id", "integer")],
        (_DESCRIBE, ("app", "order_item")): [_column_row("id", "integer"), _column_row("widget_id", "integer")],
        (_LIST_CONSTRAINTS, None): [],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [
            {
                # Constraint name collides with the widget_id column.
                "name": "widget_id",
                "from_table": "order_item",
                "to_schema": "app",
                "to_table": "widget",
                "from_columns": ["widget_id"],
                "to_columns": ["id"],
            }
        ],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    with pytest.raises(PrismaError, match="collides with column"):
        await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]


async def test_generate_prisma_schema_renders_composite_pk_and_composite_unique() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [{"name": "shard", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, ("app", "shard")): [
            _column_row("tenant", "integer"),
            _column_row("shard_no", "integer"),
            _column_row("region", "text", type_name="text"),
            _column_row("zone", "text", type_name="text"),
        ],
        (_LIST_CONSTRAINTS, ("app", "shard")): [
            {"name": "shard_pkey", "type_code": "p", "definition": "PRIMARY KEY (tenant, shard_no)"},
            {"name": "shard_region_zone_uq", "type_code": "u", "definition": "UNIQUE (region, zone)"},
        ],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    # Composite PK uses @@id since no single column is @id; UNIQUE renders as @@unique.
    assert "@@id([tenant, shard_no])" in out
    assert "@@unique([region, zone])" in out
    # No standalone @id should appear because PK is composite.
    assert " @id" not in out


async def test_generate_prisma_schema_emits_index_blocks_without_duplicating_pk() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [{"name": "widget", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, ("app", "widget")): [
            _column_row("id", "integer"),
            _column_row("name", "text", type_name="text"),
        ],
        (_LIST_CONSTRAINTS, ("app", "widget")): [
            {"name": "widget_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}
        ],
        (_LIST_INDEXES, ("app", "widget")): [
            # The PK's implicit index — should be suppressed.
            {
                "name": "widget_pkey",
                "method": "btree",
                "relkind": "i",
                "definition": "CREATE UNIQUE INDEX widget_pkey ON widget USING btree (id)",
            },
            # A real secondary index — should render.
            {
                "name": "widget_name_idx",
                "method": "btree",
                "relkind": "i",
                "definition": "CREATE INDEX widget_name_idx ON widget USING btree (name)",
            },
        ],
        (_LIST_FKS, ("app",)): [],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    assert "@@index([name])" in out
    # The (id) index is the PK and must not appear as a separate @@index/@@unique.
    assert "@@index([id])" not in out and "@@unique([id])" not in out


async def test_generate_prisma_schema_falls_back_to_unsupported_for_vector_columns() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [{"name": "doc", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, ("app", "doc")): [
            _column_row("id", "integer"),
            _column_row("embedding", "vector(384)", nullable=True, type_name="vector", type_mod=384),
        ],
        (_LIST_CONSTRAINTS, ("app", "doc")): [{"name": "doc_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    assert 'embedding Unsupported("vector(384)")?' in out


async def test_generate_prisma_schema_skips_cross_schema_fk_relation_fields() -> None:
    routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        (_LIST_TABLES, ("app",)): [{"name": "order_item", "relkind": "r", "is_partition": False}],
        (_DESCRIBE, ("app", "order_item")): [
            _column_row("id", "integer"),
            _column_row("widget_id", "integer"),
        ],
        (_LIST_CONSTRAINTS, ("app", "order_item")): [
            {"name": "order_pkey", "type_code": "p", "definition": "PRIMARY KEY (id)"}
        ],
        (_LIST_INDEXES, None): [],
        (_LIST_FKS, ("app",)): [
            {
                "name": "cross_fk",
                "from_table": "order_item",
                "to_schema": "other_schema",
                "to_table": "widget",
                "from_columns": ["widget_id"],
                "to_columns": ["id"],
            }
        ],
        (_LIST_ENUMS, ("app",)): [],
    }
    driver = FakeParamRoutingDriver(routes)

    out = await generate_prisma_schema(driver, "app")  # type: ignore[arg-type]

    # Scalar column stays, but no relation field is emitted (target not in
    # the rendered model set).
    assert "widget_id Int" in out
    assert "@relation" not in out


async def test_generate_prisma_schema_rejects_invalid_schema_identifier() -> None:
    driver = FakeParamRoutingDriver({})
    with pytest.raises(PrismaError, match="invalid schema name"):
        await generate_prisma_schema(driver, 'app"; DROP TABLE x; --')  # type: ignore[arg-type]


# --- MCP tool wiring -------------------------------------------------------


async def test_generate_prisma_schema_tool_is_registered_and_callable() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "generate_prisma_schema" in listed

        result = await client.call_tool("generate_prisma_schema", {"schema": "public"})

    assert result.isError is False
    # Empty FakeDriver returns no tables / enums, but the preamble is
    # always present.
    assert 'provider = "postgresql"' in result.content[0].text  # type: ignore[union-attr]
