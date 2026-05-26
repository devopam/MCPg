"""Tests for the PostgreSQL → Ent (Go) schema exporter."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.ent import (
    EntExportError,
    _check_identifier,
    _ent_builder_for,
    _pascal,
    _render_default_modifier,
    _render_entity_file,
    _render_field_call,
)
from mcpg.introspection import ColumnInfo
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


def test_check_identifier_rejects_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(EntExportError, match="invalid table"):
        _check_identifier('w"; DROP', "table")


def test_pascal_converts_snake_to_pascal() -> None:
    assert _pascal("widget") == "Widget"
    assert _pascal("order_item") == "OrderItem"
    assert _pascal("") == ""


def _col(name: str, data_type: str, *, nullable: bool = False, default: str | None = None) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=nullable, default=default, vector_dimension=None)


def test_ent_builder_maps_common_pg_types() -> None:
    assert _ent_builder_for(_col("a", "integer"), set()) == ('field.Int("a")', None)
    assert _ent_builder_for(_col("a", "bigint"), set()) == ('field.Int64("a")', None)
    assert _ent_builder_for(_col("a", "boolean"), set()) == ('field.Bool("a")', None)
    assert _ent_builder_for(_col("a", "text"), set()) == ('field.Text("a")', None)


def test_ent_builder_for_uuid_pulls_in_uuid_import() -> None:
    builder, extra = _ent_builder_for(_col("id", "uuid"), set())
    assert "uuid.UUID{}" in builder
    assert extra == "github.com/google/uuid"


def test_ent_builder_for_json_uses_map_string_interface_type() -> None:
    builder, _ = _ent_builder_for(_col("payload", "jsonb"), set())
    assert builder == 'field.JSON("payload", map[string]interface{}{})'


def test_ent_builder_for_enum_uses_field_enum_builder() -> None:
    builder, _ = _ent_builder_for(_col("state", "status"), {"status"})
    assert builder == 'field.Enum("state")'
    # Schema-qualified enum type resolves the same way.
    builder, _ = _ent_builder_for(_col("state", "myschema.status"), {"status"})
    assert builder == 'field.Enum("state")'


def test_render_default_modifier_maps_common_defaults_and_drops_nextval() -> None:
    # nextval (serial) is owned by Ent's primary-key generation.
    assert _render_default_modifier(_col("id", "integer", default="nextval('seq')")) is None
    # No default → no modifier.
    assert _render_default_modifier(_col("a", "integer", default=None)) is None
    # now() / CURRENT_TIMESTAMP map to time.Now (Go std).
    assert _render_default_modifier(_col("a", "timestamptz", default="now()")) == ".Default(time.Now)"
    assert _render_default_modifier(_col("a", "timestamptz", default="CURRENT_TIMESTAMP")) == ".Default(time.Now)"
    # Booleans + numbers round-trip directly.
    assert _render_default_modifier(_col("a", "boolean", default="true")) == ".Default(true)"
    assert _render_default_modifier(_col("a", "integer", default="0")) == ".Default(0)"
    # PG-quoted string → bare Go string literal.
    assert _render_default_modifier(_col("a", "text", default="'hello'::text")) == '.Default("hello")'


def test_render_field_call_attaches_enum_values_when_present() -> None:
    line, _ = _render_field_call(
        _col("state", "status"),
        {"status"},
        {"status": ["active", "inactive"]},
    )
    assert line == 'field.Enum("state").Values("active", "inactive")'


def test_render_field_call_appends_optional_for_nullable_columns() -> None:
    line, _ = _render_field_call(_col("nick", "text", nullable=True), set(), {})
    assert line == 'field.Text("nick").Optional()'


def test_render_entity_file_includes_edge_block_only_when_edges_present() -> None:
    source = _render_entity_file(
        "widget",
        [_col("id", "integer"), _col("name", "text")],
        edges=['edge.To("owner", Owner.Type).Unique().Field("owner_id"),'],
        extra_imports=set(),
        enum_names=set(),
        enum_values_by_name={},
    )
    assert "type Widget struct {" in source
    assert "func (Widget) Fields()" in source
    assert "func (Widget) Edges()" in source
    assert '"entgo.io/ent/schema/edge"' in source


def test_render_entity_file_omits_edge_imports_and_block_when_no_edges() -> None:
    source = _render_entity_file(
        "widget",
        [_col("id", "integer")],
        edges=[],
        extra_imports=set(),
        enum_names=set(),
        enum_values_by_name={},
    )
    assert "func (Widget) Edges()" not in source
    assert '"entgo.io/ent/schema/edge"' not in source


# --- tool registration ----------------------------------------------


async def test_generate_ent_schemas_tool_registered_for_reads() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "generate_ent_schemas" in listed
