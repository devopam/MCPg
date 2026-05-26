"""Tests for the PostgreSQL → jOOQ configuration exporter."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.introspection import ColumnInfo
from mcpg.jooq import (
    JooqExportError,
    _check_identifier,
    _is_json_column,
    _render_forced_type,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- pure helpers ----------------------------------------------------


def test_check_identifier_rejects_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(JooqExportError, match="invalid table"):
        _check_identifier('w"; DROP', "table")
    with pytest.raises(JooqExportError, match="invalid schema"):
        _check_identifier("with space", "schema")


def _col(name: str, data_type: str) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, nullable=True, default=None, vector_dimension=None)


def test_is_json_column_picks_up_json_and_jsonb() -> None:
    assert _is_json_column(_col("p", "jsonb")) == (True, "org.jooq.JSONB")
    assert _is_json_column(_col("p", "json")) == (True, "org.jooq.JSON")
    assert _is_json_column(_col("p", "integer")) == (False, None)
    assert _is_json_column(_col("p", "text")) == (False, None)


def test_render_forced_type_emits_anchored_expression_regex() -> None:
    out = _render_forced_type("owner", "profile", "org.jooq.JSONB", "app")
    # The expression must scope to the exact column path so it doesn't
    # accidentally match other columns of the same name.
    assert "<expression>app\\.owner\\.profile</expression>" in out
    assert "<userType>org.jooq.JSONB</userType>" in out
    assert "<types>.*</types>" in out


# --- tool registration ----------------------------------------------


async def test_generate_jooq_config_tool_registered_for_reads() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "generate_jooq_config" in listed
