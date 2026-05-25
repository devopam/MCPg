"""Tests for the PostgreSQL → sqlc schema exporter."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import ColumnInfo
from mcpg.server import create_server
from mcpg.sqlc import (
    SqlcExportError,
    _check_identifier,
    _render_column_line,
    _render_constraint,
    _render_enum,
    _render_index,
    _render_table,
)

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- pure helpers ----------------------------------------------------


def test_check_identifier_rejects_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(SqlcExportError, match="invalid table"):
        _check_identifier('w"; DROP', "table")
    with pytest.raises(SqlcExportError, match="invalid schema"):
        _check_identifier("with space", "schema")


def _col(
    name: str,
    data_type: str,
    *,
    nullable: bool = False,
    default: str | None = None,
) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=nullable, default=default, vector_dimension=None)


def test_render_column_line_emits_quoted_name_and_full_type_with_nullability() -> None:
    assert _render_column_line(_col("id", "integer", nullable=False)) == '    "id" integer NOT NULL'
    assert _render_column_line(_col("name", "text", nullable=True)) == '    "name" text'
    assert (
        _render_column_line(_col("flag", "boolean", nullable=False, default="false"))
        == '    "flag" boolean NOT NULL DEFAULT false'
    )
    # varchar(N) round-trips because data_type already contains the parens.
    assert _render_column_line(_col("name", "character varying(120)")) == '    "name" character varying(120) NOT NULL'


def test_render_enum_emits_create_type_with_quoted_labels() -> None:
    assert _render_enum("status", ["active", "inactive"]) == "CREATE TYPE \"status\" AS ENUM ('active', 'inactive');"


def test_render_table_emits_qualified_create_table_with_indented_columns() -> None:
    ddl = _render_table("app", "widget", [_col("id", "integer"), _col("name", "text", nullable=True)])
    assert ddl.startswith('CREATE TABLE "app"."widget" (\n')
    assert '"id" integer NOT NULL,\n    "name" text' in ddl
    assert ddl.rstrip().endswith(");")


def test_render_constraint_attaches_alter_table_clause() -> None:
    out = _render_constraint("app", "widget", "widget_pkey", "PRIMARY KEY (id)")
    assert out == 'ALTER TABLE "app"."widget" ADD CONSTRAINT "widget_pkey" PRIMARY KEY (id);'


def test_render_index_preserves_create_index_statement_with_trailing_semicolon() -> None:
    # pg_get_indexdef returns the full statement without a trailing
    # semicolon; the renderer appends one if missing and trims a duplicate.
    raw = "CREATE INDEX idx_x ON app.widget USING btree (name)"
    assert _render_index(raw) == raw + ";"
    assert _render_index(raw + ";") == raw + ";"


# --- tool registration ----------------------------------------------


async def test_generate_sqlc_schema_tool_registered_for_reads() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "generate_sqlc_schema" in listed
