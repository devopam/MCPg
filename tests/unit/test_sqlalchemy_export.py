"""Tests for the PostgreSQL → SQLAlchemy 2.0 exporter."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import ColumnInfo
from mcpg.server import create_server
from mcpg.sqlalchemy_export import (
    SqlAlchemyExportError,
    _check_identifier,
    _is_serial,
    _pascal_case,
    _render_default,
    _render_enum_class,
    _sa_type_for,
    _strip_type_params,
)

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- pure helpers ----------------------------------------------------


def test_pascal_case_converts_snake_to_pascal() -> None:
    assert _pascal_case("widget") == "Widget"
    assert _pascal_case("order_item") == "OrderItem"
    assert _pascal_case("HTTP_request") == "HTTPRequest"
    assert _pascal_case("") == ""


def test_strip_type_params_drops_parens() -> None:
    assert _strip_type_params("varchar(120)") == "varchar"
    assert _strip_type_params("numeric(10,2)") == "numeric"


def test_check_identifier_rejects_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(SqlAlchemyExportError, match="invalid table"):
        _check_identifier('w"; DROP', "table")
    with pytest.raises(SqlAlchemyExportError, match="invalid schema"):
        _check_identifier("with space", "schema")


def _col(
    name: str,
    data_type: str,
    *,
    nullable: bool = False,
    default: str | None = None,
) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=nullable, default=default, vector_dimension=None)


def test_sa_type_maps_common_pg_types_to_core_and_pg_dialect_helpers() -> None:
    assert _sa_type_for(_col("a", "integer"), set()) == ("Integer", "int", "core")
    assert _sa_type_for(_col("a", "bigint"), set()) == ("BigInteger", "int", "core")
    assert _sa_type_for(_col("a", "boolean"), set()) == ("Boolean", "bool", "core")
    assert _sa_type_for(_col("a", "text"), set()) == ("Text", "str", "core")
    assert _sa_type_for(_col("a", "uuid"), set()) == ("Uuid", "UUID", "core")
    # jsonb is from the postgresql dialect.
    assert _sa_type_for(_col("a", "jsonb"), set()) == ("JSONB", "dict", "pg")


def test_sa_type_carries_varchar_length() -> None:
    sa, py, source = _sa_type_for(_col("a", "character varying(120)"), set())
    assert sa == "String(120)"
    assert py == "str"
    assert source == "core"


def test_sa_type_carries_numeric_precision() -> None:
    sa, py, source = _sa_type_for(_col("a", "numeric(10,2)"), set())
    assert sa == "Numeric(10, 2)"
    assert py == "Decimal"
    assert source == "core"


def test_sa_type_marks_timestamp_with_timezone() -> None:
    sa, py, source = _sa_type_for(_col("a", "timestamp with time zone"), set())
    assert sa == "DateTime(timezone=True)"
    assert py == "datetime"
    assert source == "core"


def test_sa_type_resolves_enum_columns_to_generated_class() -> None:
    sa, py, _ = _sa_type_for(_col("state", "status"), {"status"})
    assert sa == "Enum(Status)"
    assert py == "Status"
    # Schema-qualified types resolve via the bare suffix.
    sa, py, _ = _sa_type_for(_col("state", "myschema.status"), {"status"})
    assert sa == "Enum(Status)"
    assert py == "Status"


def test_sa_type_unknown_falls_back_to_string() -> None:
    sa, py, source = _sa_type_for(_col("a", "totally_made_up"), set())
    assert sa == "String"
    assert py == "str"
    assert source == "core"


def test_is_serial_detects_nextval_defaults() -> None:
    assert _is_serial(_col("id", "integer", default="nextval('seq')")) is True
    assert _is_serial(_col("id", "integer", default="0")) is False


def test_render_default_skips_serial_and_maps_common_pg_defaults() -> None:
    # nextval defaults → SQLAlchemy generates them from the column type,
    # so we emit no server_default.
    assert _render_default(_col("id", "integer", default="nextval('seq')")) is None
    # No default → no kwarg.
    assert _render_default(_col("a", "integer", default=None)) is None
    # now() / CURRENT_TIMESTAMP → server_default=func.now()
    assert _render_default(_col("a", "timestamptz", default="now()")) == "server_default=func.now()"
    assert _render_default(_col("a", "timestamptz", default="CURRENT_TIMESTAMP")) == "server_default=func.now()"
    # Booleans wrap in text().
    assert _render_default(_col("a", "boolean", default="true")) == 'server_default=text("true")'
    # Integers wrap in text() too — keeps server-side parity.
    assert _render_default(_col("a", "integer", default="0")) == 'server_default=text("0")'


def test_render_enum_class_emits_python_enum_with_string_values() -> None:
    out = _render_enum_class("status", ["active", "inactive"])
    assert "class Status(enum.Enum):" in out
    assert '    active = "active"' in out
    assert '    inactive = "inactive"' in out


# --- tool registration ----------------------------------------------


async def test_generate_sqlalchemy_models_tool_registered_for_reads() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "generate_sqlalchemy_models" in listed
