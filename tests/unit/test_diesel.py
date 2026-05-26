"""Tests for the PostgreSQL → Diesel ORM (Rust) exporter."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.diesel import (
    DieselExportError,
    _check_identifier,
    _diesel_sql_type,
    _parse_pk_columns,
    _pascal,
    _render_allow_join,
    _render_enum_module,
    _render_joinable,
    _render_table_block,
)
from mcpg.introspection import ColumnInfo
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- pure helpers ----------------------------------------------------


def test_check_identifier_rejects_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(DieselExportError, match="invalid table"):
        _check_identifier('w"; DROP', "table")
    with pytest.raises(DieselExportError, match="invalid schema"):
        _check_identifier("with space", "schema")


def test_pascal_converts_snake_and_kebab_to_pascal() -> None:
    assert _pascal("widget") == "Widget"
    assert _pascal("in-progress") == "InProgress"
    assert _pascal("order_item") == "OrderItem"
    assert _pascal("") == ""


def _col(name: str, data_type: str, *, nullable: bool = False) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=nullable, default=None, vector_dimension=None)


def test_diesel_sql_type_maps_common_pg_types() -> None:
    assert _diesel_sql_type(_col("a", "integer"), set()) == "Integer"
    assert _diesel_sql_type(_col("a", "bigint"), set()) == "BigInt"
    assert _diesel_sql_type(_col("a", "boolean"), set()) == "Bool"
    assert _diesel_sql_type(_col("a", "text"), set()) == "Text"
    assert _diesel_sql_type(_col("a", "character varying(120)"), set()) == "Varchar"
    assert _diesel_sql_type(_col("a", "timestamp with time zone"), set()) == "Timestamptz"
    assert _diesel_sql_type(_col("a", "jsonb"), set()) == "Jsonb"
    assert _diesel_sql_type(_col("a", "uuid"), set()) == "Uuid"


def test_diesel_sql_type_wraps_nullable_in_nullable_t() -> None:
    assert _diesel_sql_type(_col("a", "integer", nullable=True), set()) == "Nullable<Integer>"
    assert _diesel_sql_type(_col("a", "text", nullable=True), set()) == "Nullable<Text>"


def test_diesel_sql_type_routes_enum_columns_to_text_backed_wrapper() -> None:
    # Both unqualified and schema-qualified enum types resolve.
    assert _diesel_sql_type(_col("state", "status"), {"status"}) == "Text"
    assert _diesel_sql_type(_col("state", "myschema.status"), {"status"}) == "Text"
    assert _diesel_sql_type(_col("state", "status", nullable=True), {"status"}) == "Nullable<Text>"


def test_diesel_sql_type_falls_back_to_text_for_unknown_pg_types() -> None:
    assert _diesel_sql_type(_col("a", "totally_made_up"), set()) == "Text"


def test_parse_pk_columns_extracts_from_constraint_definitions() -> None:
    assert _parse_pk_columns("PRIMARY KEY (id)") == ["id"]
    assert _parse_pk_columns('PRIMARY KEY ("user_id", tenant)') == ["user_id", "tenant"]
    assert _parse_pk_columns("UNIQUE (email)") == []


def test_render_table_block_emits_table_macro_with_pk_clause() -> None:
    block = _render_table_block("widget", [_col("id", "integer"), _col("name", "text")], ["id"], set())
    assert block.startswith("table! {")
    assert "widget (id) {" in block
    assert "        id -> Integer," in block
    assert "        name -> Text," in block
    assert block.rstrip().endswith("}")


def test_render_table_block_omits_pk_clause_when_no_primary_key() -> None:
    block = _render_table_block("audit_log", [_col("ts", "timestamp without time zone")], [], set())
    # Diesel allows omitting the (pk) clause when there isn't one — Diesel
    # treats the first column as the PK by default.
    assert "audit_log {" in block
    assert "audit_log (" not in block


def test_render_table_block_emits_composite_pk_in_macro_header() -> None:
    block = _render_table_block(
        "team_member",
        [_col("team_id", "integer"), _col("user_id", "integer")],
        ["team_id", "user_id"],
        set(),
    )
    assert "team_member (team_id, user_id) {" in block


def test_render_joinable_emits_macro_with_fk_column() -> None:
    assert _render_joinable("widget", "owner", "owner_id") == "joinable!(widget -> owner (owner_id));"


def test_render_allow_join_sorts_table_names_alphabetically() -> None:
    # Stable ordering matters for repeatable output across runs.
    out = _render_allow_join(["widget", "owner", "audit"])
    assert out == "allow_tables_to_appear_in_same_query!(audit, owner, widget);"


def test_render_enum_module_emits_text_backed_wrapper_per_enum() -> None:
    out = _render_enum_module([("status", ["active", "inactive"])])
    assert "pub mod pg_enum {" in out
    assert "pub enum Status {" in out
    assert "Active," in out
    assert "Inactive," in out
    assert "diesel(sql_type = diesel::sql_types::Text)" in out


def test_render_enum_module_returns_empty_string_when_no_enums() -> None:
    assert _render_enum_module([]) == ""


# --- tool registration ----------------------------------------------


async def test_generate_diesel_schema_tool_registered_for_reads() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "generate_diesel_schema" in listed
