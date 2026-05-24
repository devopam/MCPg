"""Tests for the in-process data-movement export tools."""

import json

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.data_movement import (
    DEFAULT_EXPORT_LIMIT,
    EXPORT_FORMATS,
    ExportError,
    ExportResult,
    _rows_to_csv,
    _rows_to_json,
    export_query,
    export_table,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- _rows_to_csv / _rows_to_json -----------------------------------------


def test_rows_to_csv_emits_header_and_quotes_strings_with_commas() -> None:
    out = _rows_to_csv([{"id": 1, "name": "a, b"}, {"id": 2, "name": "c"}])
    assert out.splitlines()[0] == "id,name"
    assert '"a, b"' in out


def test_rows_to_csv_returns_empty_string_for_no_rows() -> None:
    assert _rows_to_csv([]) == ""


def test_rows_to_csv_stringifies_non_scalar_cell_values() -> None:
    # datetime / UUID / decimal arrive verbatim — anything not a plain
    # scalar gets str()'d so the CSV is always readable.
    from datetime import datetime

    out = _rows_to_csv([{"created": datetime(2026, 5, 24, 12, 0, 0)}])
    assert "2026-05-24" in out


def test_rows_to_json_serialises_with_default_str_for_non_native_types() -> None:
    from datetime import datetime

    out = _rows_to_json([{"id": 1, "created": datetime(2026, 5, 24)}])
    parsed = json.loads(out)
    assert parsed[0]["id"] == 1
    assert "2026-05-24" in parsed[0]["created"]


# --- export_query ---------------------------------------------------------


async def test_export_query_emits_csv_with_header_and_rows() -> None:
    driver = FakeDriver([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    result = await export_query(driver, "SELECT id, name FROM widget", format="csv")  # type: ignore[arg-type]

    assert isinstance(result, ExportResult)
    assert result.format == "csv"
    assert result.row_count == 2
    assert result.truncated is False
    lines = result.content.splitlines()
    assert lines[0] == "id,name"
    assert lines[1].startswith("1,")
    assert lines[2].startswith("2,")


async def test_export_query_emits_json_array_of_objects() -> None:
    driver = FakeDriver([{"id": 1, "name": "a"}])

    result = await export_query(driver, "SELECT id, name FROM widget", format="json")  # type: ignore[arg-type]

    parsed = json.loads(result.content)
    assert parsed == [{"id": 1, "name": "a"}]


async def test_export_query_flags_truncation_when_query_yields_more_than_limit() -> None:
    driver = FakeDriver([{"id": i} for i in range(5)])

    result = await export_query(driver, "SELECT id FROM widget", format="csv", limit=2)  # type: ignore[arg-type]

    assert result.truncated is True
    assert result.row_count == 2


async def test_export_query_rejects_unsupported_format() -> None:
    with pytest.raises(ExportError, match="unsupported export format"):
        await export_query(FakeDriver(), "SELECT 1", format="xml")  # type: ignore[arg-type]


async def test_export_query_rejects_non_positive_limit() -> None:
    with pytest.raises(ExportError, match="must be at least 1"):
        await export_query(FakeDriver(), "SELECT 1", limit=0)  # type: ignore[arg-type]


async def test_export_query_wraps_query_errors() -> None:
    # SafeSqlDriver inside run_select rejects non-SELECT — surfaces as
    # ExportError so the agent sees a single failure mode.
    with pytest.raises(ExportError):
        await export_query(FakeDriver(), "DELETE FROM widget")  # type: ignore[arg-type]


# --- export_table ---------------------------------------------------------


async def test_export_table_builds_a_safe_select_against_quoted_identifiers() -> None:
    driver = FakeDriver([{"id": 1}])

    await export_table(driver, "app", "widget", format="csv")  # type: ignore[arg-type]

    # The vendored SafeSqlDriver wraps the raw driver — the call we
    # care about is whatever SQL it eventually issued. Inspect the
    # FakeDriver's call log for the schema-qualified SELECT shape.
    sqls = [call[0] for call in driver.calls]
    assert any('"app"."widget"' in sql or "app.widget" in sql for sql in sqls)


async def test_export_table_rejects_invalid_identifier_characters() -> None:
    with pytest.raises(ExportError, match="invalid schema name"):
        await export_table(FakeDriver(), 'app"; DROP TABLE x; --', "widget")  # type: ignore[arg-type]
    with pytest.raises(ExportError, match="invalid table name"):
        await export_table(FakeDriver(), "app", "widget; DROP")  # type: ignore[arg-type]


# --- module exports + tool wiring -----------------------------------------


def test_export_formats_set_is_complete() -> None:
    assert EXPORT_FORMATS == {"csv", "json"}


def test_default_export_limit_is_a_sensible_ceiling() -> None:
    # Documented in the docstring; tests pin the constant so accidental
    # bumps surface in PR review.
    assert DEFAULT_EXPORT_LIMIT == 10_000


async def test_export_tools_are_registered_and_callable_in_read_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver([{"id": 1}])))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert {"export_query", "export_table"} <= listed

        result = await client.call_tool("export_query", {"sql": "SELECT id FROM widget"})
    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["format"] == "csv"
    assert payload["row_count"] == 1
