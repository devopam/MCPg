"""Tests for the PostgreSQL → Ecto (Elixir) schema exporter."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.ecto import (
    EctoExportError,
    _check_identifier,
    _ecto_field_type,
    _is_timestamp_pair,
    _pascal,
    _render_belongs_to,
    _render_module,
    _singularize,
)
from mcpg.introspection import ColumnInfo
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


def test_check_identifier_rejects_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(EctoExportError, match="invalid table"):
        _check_identifier('w"; DROP', "table")


def test_pascal_converts_snake_to_pascal() -> None:
    assert _pascal("widget") == "Widget"
    assert _pascal("order_item") == "OrderItem"


def test_singularize_handles_common_english_plurals() -> None:
    assert _singularize("widgets") == "widget"
    assert _singularize("users") == "user"
    assert _singularize("categories") == "category"  # -ies → -y
    assert _singularize("addresses") == "address"  # -ses → -se → wait, let's check
    # Edge cases — singular nouns are returned unchanged.
    assert _singularize("data") == "data"
    assert _singularize("status") == "status"  # -ss endings preserved
    assert _singularize("") == ""


def _col(name: str, data_type: str, *, nullable: bool = False) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=nullable, default=None, vector_dimension=None)


def test_ecto_field_type_maps_common_pg_types() -> None:
    assert _ecto_field_type(_col("a", "integer"), set()) == ":integer"
    assert _ecto_field_type(_col("a", "bigint"), set()) == ":integer"
    assert _ecto_field_type(_col("a", "boolean"), set()) == ":boolean"
    assert _ecto_field_type(_col("a", "text"), set()) == ":string"
    assert _ecto_field_type(_col("a", "character varying(120)"), set()) == ":string"
    assert _ecto_field_type(_col("a", "numeric(10,2)"), set()) == ":decimal"
    assert _ecto_field_type(_col("a", "uuid"), set()) == "Ecto.UUID"
    assert _ecto_field_type(_col("a", "jsonb"), set()) == ":map"
    assert _ecto_field_type(_col("a", "timestamp with time zone"), set()) == ":utc_datetime"
    assert _ecto_field_type(_col("a", "timestamp without time zone"), set()) == ":naive_datetime"


def test_ecto_field_type_falls_back_to_string_for_enums() -> None:
    # Without the EctoEnum library Ecto can't natively express PG enums.
    assert _ecto_field_type(_col("state", "status"), {"status"}) == ":string"
    assert _ecto_field_type(_col("state", "myschema.status"), {"status"}) == ":string"


def test_is_timestamp_pair_requires_both_inserted_and_updated() -> None:
    cols = [_col("id", "integer"), _col("inserted_at", "timestamptz"), _col("updated_at", "timestamptz")]
    assert _is_timestamp_pair(cols) is True
    assert _is_timestamp_pair([_col("inserted_at", "timestamptz")]) is False
    assert _is_timestamp_pair([_col("updated_at", "timestamptz")]) is False


def test_render_belongs_to_strips_id_suffix_for_association_name() -> None:
    line = _render_belongs_to("owner_id", "owners", "Shop.Owner")
    assert line == "    belongs_to :owner, Shop.Owner, foreign_key: :owner_id"
    # No _id suffix → use the column name as the association.
    line = _render_belongs_to("parent", "parents", "Shop.Parent")
    assert line == "    belongs_to :parent, Shop.Parent, foreign_key: :parent"


def test_render_module_uses_default_primary_key_for_single_column_id() -> None:
    source = _render_module(
        "MyApp",
        "widgets",
        [_col("id", "integer"), _col("name", "text")],
        ["id"],
        belongs_to_lines=[],
        enum_names=set(),
    )
    assert "defmodule MyApp.Widget do" in source
    assert "@primary_key {:id, :id, autogenerate: true}" in source
    # id column NOT declared as a field — @primary_key handles it.
    assert "field :id," not in source
    assert "field :name, :string" in source


def test_render_module_emits_timestamps_macro_when_both_columns_present() -> None:
    cols = [
        _col("id", "integer"),
        _col("name", "text"),
        _col("inserted_at", "timestamp with time zone"),
        _col("updated_at", "timestamp with time zone"),
    ]
    source = _render_module("Shop", "users", cols, ["id"], belongs_to_lines=[], enum_names=set())
    assert "timestamps()" in source
    # The two timestamp columns are NOT re-declared as fields.
    assert "field :inserted_at" not in source
    assert "field :updated_at" not in source


def test_render_module_skips_fk_columns_already_covered_by_belongs_to() -> None:
    cols = [_col("id", "integer"), _col("owner_id", "integer"), _col("name", "text")]
    belongs_to = ["    belongs_to :owner, Shop.Owner, foreign_key: :owner_id"]
    source = _render_module("Shop", "widgets", cols, ["id"], belongs_to_lines=belongs_to, enum_names=set())
    # belongs_to emitted, owner_id NOT also declared as a field (would be a duplicate).
    assert "belongs_to :owner" in source
    assert "field :owner_id" not in source


def test_render_module_emits_composite_primary_key_with_primary_key_true() -> None:
    source = _render_module(
        "Shop",
        "team_members",
        [_col("team_id", "integer"), _col("user_id", "integer"), _col("role", "text")],
        ["team_id", "user_id"],
        belongs_to_lines=[],
        enum_names=set(),
    )
    assert "@primary_key false" in source
    assert "field :team_id, :integer, primary_key: true" in source
    assert "field :user_id, :integer, primary_key: true" in source


# --- tool registration ----------------------------------------------


async def test_generate_ecto_schemas_tool_registered_for_reads() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "generate_ecto_schemas" in listed
