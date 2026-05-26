"""Tests for the PostgreSQL → Drizzle ORM schema exporter."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.drizzle import (
    _PK_COLS_RE,
    _UNIQUE_COLS_RE,
    DrizzleError,
    _camel_case,
    _check_identifier,
    _collect_used_helpers,
    _drizzle_helper_for,
    _is_serial,
    _parse_columns,
    _render_column,
    _render_default,
    _render_enum,
    _strip_type_params,
)
from mcpg.introspection import ColumnInfo
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- pure helpers ----------------------------------------------------


def test_camel_case_converts_snake_to_camel() -> None:
    assert _camel_case("owner_id") == "ownerId"
    assert _camel_case("created_at") == "createdAt"
    assert _camel_case("simple") == "simple"
    # Leading underscore is preserved as empty head; ensures we don't crash.
    assert _camel_case("_internal") == "Internal"


def test_strip_type_params_drops_parameter_block() -> None:
    assert _strip_type_params("varchar(120)") == "varchar"
    assert _strip_type_params("numeric(10,2)") == "numeric"
    assert _strip_type_params("integer") == "integer"


def test_check_identifier_rejects_quoted_or_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(DrizzleError, match="invalid table"):
        _check_identifier('w"; DROP', "table")
    with pytest.raises(DrizzleError, match="invalid schema"):
        _check_identifier("with space", "schema")


def _col(
    name: str,
    data_type: str,
    *,
    nullable: bool = False,
    default: str | None = None,
) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=nullable, default=default, vector_dimension=None)


def test_drizzle_helper_maps_common_scalars() -> None:
    assert _drizzle_helper_for(_col("a", "integer"), set()) == ("integer", [])
    assert _drizzle_helper_for(_col("a", "bigint"), set()) == ("bigint", [])
    assert _drizzle_helper_for(_col("a", "boolean"), set()) == ("boolean", [])
    assert _drizzle_helper_for(_col("a", "text"), set()) == ("text", [])
    assert _drizzle_helper_for(_col("a", "jsonb"), set()) == ("jsonb", [])
    assert _drizzle_helper_for(_col("a", "uuid"), set()) == ("uuid", [])


def test_drizzle_helper_captures_varchar_length() -> None:
    helper, options = _drizzle_helper_for(_col("a", "character varying(120)"), set())
    assert helper == "varchar"
    assert options == ["length: 120"]


def test_drizzle_helper_marks_timestamp_with_timezone() -> None:
    helper, options = _drizzle_helper_for(_col("a", "timestamp with time zone"), set())
    assert helper == "timestamp"
    assert "withTimezone: true" in options


def test_drizzle_helper_resolves_enum_columns_to_the_generated_const() -> None:
    helper, _ = _drizzle_helper_for(_col("state", "status"), {"status"})
    assert helper == "statusEnum"
    # Schema-qualified enum types resolve to the bare enum name.
    helper, _ = _drizzle_helper_for(_col("state", "myschema.status"), {"status"})
    assert helper == "statusEnum"


def test_drizzle_helper_falls_back_to_text_for_unknown_types() -> None:
    helper, _ = _drizzle_helper_for(_col("a", "totally_made_up_type"), set())
    assert helper == "text"


def test_is_serial_detects_nextval_default() -> None:
    assert _is_serial(_col("id", "integer", default="nextval('seq')")) is True
    assert _is_serial(_col("id", "integer", default='nextval("seq"::regclass)')) is True
    assert _is_serial(_col("id", "integer", default="0")) is False
    assert _is_serial(_col("id", "integer", default=None)) is False


def test_parse_columns_extracts_column_names_from_constraint_definitions() -> None:
    assert _parse_columns("PRIMARY KEY (id)", _PK_COLS_RE) == ["id"]
    assert _parse_columns('PRIMARY KEY ("user_id", tenant)', _PK_COLS_RE) == ["user_id", "tenant"]
    assert _parse_columns("UNIQUE (email)", _UNIQUE_COLS_RE) == ["email"]
    assert _parse_columns("CHECK (x > 0)", _PK_COLS_RE) == []


def test_render_default_maps_common_pg_defaults_to_drizzle_clauses() -> None:
    assert _render_default(_col("a", "integer", default="0")) == ".default(0)"
    assert _render_default(_col("a", "boolean", default="true")) == ".default(true)"
    assert _render_default(_col("a", "boolean", default="false")) == ".default(false)"
    # CURRENT_TIMESTAMP / now() both map to .defaultNow().
    assert _render_default(_col("a", "timestamptz", default="now()")) == ".defaultNow()"
    assert _render_default(_col("a", "timestamptz", default="CURRENT_TIMESTAMP")) == ".defaultNow()"
    # PG-quoted string literal with ::type suffix → bare TS string.
    assert _render_default(_col("a", "text", default="'hello'::text")) == '.default("hello")'
    # No default → no clause.
    assert _render_default(_col("a", "integer", default=None)) is None
    # Unrecognised (non-quoted, non-numeric, non-boolean) → sql template
    # literal fallback so the agent sees the raw expression instead of
    # silently losing the default.
    out = _render_default(_col("a", "uuid", default="gen_random_uuid()"))
    assert out is not None and out.startswith(".default(sql`")


def test_render_enum_emits_typed_const_with_label_array() -> None:
    out = _render_enum("status", ["active", "inactive", "pending"])
    assert out == 'export const statusEnum = pgEnum("status", ["active", "inactive", "pending"]);'


def test_collect_used_helpers_picks_up_top_level_calls_only() -> None:
    body = (
        'export const widget = pgTable("widget", {\n'
        '  id: serial("id").primaryKey(),\n'
        '  name: text("name").notNull().unique(),\n'
        "});\n"
    )
    helpers = _collect_used_helpers(body)
    # Top-level helpers we used.
    assert "pgTable" in helpers
    assert "serial" in helpers
    assert "text" in helpers
    # Chain methods must NOT be picked up — they don't need imports.
    assert "primaryKey" not in helpers
    assert "unique" not in helpers
    assert "notNull" not in helpers


def test_render_column_chains_not_null_default_and_references() -> None:
    line = _render_column(
        _col("owner_id", "integer", nullable=False),
        pk_columns=set(),
        unique_columns=set(),
        fk_lookup={
            "owner_id": type(
                "Fk",
                (),
                {"to_table": "owner", "to_columns": ["id"]},
            )(),  # type: ignore[arg-type]
        },
        enum_names=set(),
    )
    assert 'ownerId: integer("owner_id")' in line
    assert ".notNull()" in line
    assert ".references(() => owner.id)" in line


def test_render_column_marks_single_column_pk_with_primary_key_chain() -> None:
    line = _render_column(
        _col("id", "integer", nullable=False, default="nextval('seq')"),
        pk_columns={"id"},
        unique_columns=set(),
        fk_lookup={},
        enum_names=set(),
    )
    # serial fields don't carry the .notNull() chain (the helper implies it)
    # and never re-emit the nextval default.
    assert "serial" in line
    assert ".primaryKey()" in line
    assert ".notNull()" not in line


# --- tool registration ----------------------------------------------


async def test_generate_drizzle_schema_tool_registered_for_reads() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "generate_drizzle_schema" in listed
