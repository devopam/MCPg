"""Tests for the SQL audit-trail module and its MCP read tool."""

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.audit_trail import (
    AUDIT_SCHEMA,
    AUDIT_TABLE,
    AuditTrailEntry,
    SchemaDiffSnapshot,
    _redact,
    _reset_audit_init_cache,
    capture_columns,
    ensure_audit_table,
    list_audit_events,
    record_audit,
)
from mcpg.config import load_settings
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


@pytest.fixture(autouse=True)
def _isolated_ensure_cache() -> None:
    """Reset the per-driver ensure cache before every test in this module."""
    _reset_audit_init_cache()


# --- _redact ---------------------------------------------------------------


def test_redact_masks_credential_keys_and_obfuscates_passwords_in_strings() -> None:
    redacted = _redact({"sql": "SELECT 1", "password": "hunter2", "schema": "app"})

    assert redacted["password"] == "****"
    # Non-credential keys pass through unchanged.
    assert redacted["sql"] == "SELECT 1"
    assert redacted["schema"] == "app"


def test_redact_obfuscates_connection_string_passwords_in_arbitrary_string_args() -> None:
    redacted = _redact({"url": "postgresql://u:hunter2@localhost/db"})
    # obfuscate_password strips the credential from the connection string.
    assert "hunter2" not in redacted["url"]


def test_redact_walks_nested_dicts_lists_and_tuples() -> None:
    # Sensitive data hiding inside a RETURNING payload must be masked
    # too — Gemini's security-critical PR #12 finding.
    payload = {
        "rows": [
            {"id": 1, "password": "hunter2", "name": "alice"},
            {"id": 2, "token": "tk_abc", "name": "bob"},
        ],
        "schema_diff": {
            "columns_before": (),
            "columns_after": ({"name": "secret", "default": "'tk_def'::text"},),
        },
    }

    redacted = _redact(payload)

    # Top-level structure is preserved.
    assert isinstance(redacted, dict)
    assert {row["name"] for row in redacted["rows"]} == {"alice", "bob"}
    # Sensitive keys are masked everywhere they appear, however deep.
    assert redacted["rows"][0]["password"] == "****"
    assert redacted["rows"][1]["token"] == "****"
    # Lists and tuples both get walked and types preserved.
    assert isinstance(redacted["schema_diff"]["columns_after"], tuple)


# --- ensure_audit_table ----------------------------------------------------


async def test_ensure_audit_table_runs_two_idempotent_creates() -> None:
    _reset_audit_init_cache()
    driver = FakeDriver()

    await ensure_audit_table(driver)  # type: ignore[arg-type]

    sql_run = [call[0] for call in driver.calls]
    assert any("CREATE SCHEMA IF NOT EXISTS" in sql and AUDIT_SCHEMA in sql for sql in sql_run)
    assert any("CREATE TABLE IF NOT EXISTS" in sql and AUDIT_TABLE in sql for sql in sql_run)
    # Writes are not read-only.
    assert all(call[2] is False for call in driver.calls)


async def test_ensure_audit_table_is_cached_per_driver_and_skips_repeat_creates() -> None:
    # Performance fix (Gemini PR #12): every write was paying for two
    # CREATE round-trips even though the table already existed. After
    # the first ensure on a driver, subsequent ensures are no-ops.
    _reset_audit_init_cache()
    driver = FakeDriver()

    await ensure_audit_table(driver)  # type: ignore[arg-type]
    first_call_count = len(driver.calls)
    await ensure_audit_table(driver)  # type: ignore[arg-type]
    await ensure_audit_table(driver)  # type: ignore[arg-type]

    # No additional DB round-trips were made on the second + third call.
    assert len(driver.calls) == first_call_count


async def test_ensure_audit_table_re_runs_for_a_distinct_driver_instance() -> None:
    # The cache is keyed by id(driver) so a different driver always
    # gets its own ensure pass.
    _reset_audit_init_cache()
    first = FakeDriver()
    second = FakeDriver()

    await ensure_audit_table(first)  # type: ignore[arg-type]
    await ensure_audit_table(second)  # type: ignore[arg-type]

    assert len(first.calls) > 0
    assert len(second.calls) > 0


# --- record_audit ----------------------------------------------------------


async def test_record_audit_inserts_redacted_arguments_and_serialises_jsonb() -> None:
    driver = FakeDriver()

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_write",
        arguments={"sql": "UPDATE widget SET x = 1", "password": "hunter2"},
        status="ok",
        error=None,
        result={"rows": [{"id": 1}], "row_count": 1, "schema_diff": None},
    )

    insert_calls = [call for call in driver.calls if "INSERT INTO" in call[0]]
    assert len(insert_calls) == 1
    params = insert_calls[0][1]
    assert params is not None
    assert params[0] == "run_write"  # tool
    assert "hunter2" not in params[1]  # credential redacted in jsonb
    assert "****" in params[1]
    assert params[2] == "ok"  # status
    assert params[3] is None  # error
    assert params[4] is not None and "row_count" in params[4]  # result jsonb


async def test_record_audit_redacts_nested_credentials_inside_the_result_payload() -> None:
    # Gemini PR #12 security-critical: a RETURNING * over a credentials
    # table would have written plaintext into the audit log. The result
    # payload now goes through _redact too.
    _reset_audit_init_cache()
    driver = FakeDriver()

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_write",
        arguments={"sql": "INSERT ... RETURNING *"},
        status="ok",
        result={"rows": [{"id": 1, "password": "hunter2", "name": "alice"}], "row_count": 1},
    )

    insert_calls = [call for call in driver.calls if "INSERT INTO mcpg_audit.events" in call[0]]
    params = insert_calls[0][1]
    assert params is not None
    # The persisted result jsonb must NOT contain the plaintext credential.
    assert "hunter2" not in params[4]
    assert "****" in params[4]


async def test_record_audit_persists_a_null_result_when_caller_supplies_none() -> None:
    _reset_audit_init_cache()
    driver = FakeDriver()

    await record_audit(driver, tool="run_ddl", arguments={"sql": "DROP TABLE x"}, status="error", error="boom")  # type: ignore[arg-type]

    insert_calls = [call for call in driver.calls if "INSERT INTO" in call[0]]
    params = insert_calls[0][1]
    assert params is not None
    assert params[3] == "boom"
    assert params[4] is None  # result column is NULL


# --- list_audit_events ----------------------------------------------------


async def test_list_audit_events_returns_empty_list_when_table_does_not_exist() -> None:
    # Driver returns no rows for the existence-check query -> table absent.
    assert await list_audit_events(FakeDriver()) == []  # type: ignore[arg-type]


async def test_list_audit_events_maps_rows_when_table_exists() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            f"FROM {AUDIT_SCHEMA}.{AUDIT_TABLE}": [
                {
                    "id": 7,
                    "occurred_at": "2026-05-24T15:30:00Z",
                    "tool": "run_ddl",
                    "arguments": {"sql": "CREATE TABLE x()"},
                    "status": "ok",
                    "error": None,
                    "result": {"rows": [], "row_count": 0},
                }
            ],
        }
    )

    events = await list_audit_events(driver, limit=10)  # type: ignore[arg-type]

    assert events == [
        AuditTrailEntry(
            id=7,
            occurred_at="2026-05-24T15:30:00Z",
            tool="run_ddl",
            arguments={"sql": "CREATE TABLE x()"},
            status="ok",
            error=None,
            result={"rows": [], "row_count": 0},
        )
    ]


async def test_list_audit_events_filters_by_tool_when_supplied() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            f"FROM {AUDIT_SCHEMA}.{AUDIT_TABLE}": [],
        }
    )

    await list_audit_events(driver, limit=5, tool="run_write")  # type: ignore[arg-type]

    select_calls = [call for call in driver.calls if "ORDER BY id DESC" in call[0]]
    assert len(select_calls) == 1
    params = select_calls[0][1]
    assert params == ["run_write", 5]


# --- capture_columns ------------------------------------------------------


async def test_capture_columns_emits_one_dict_per_column() -> None:
    driver = FakeDriver(
        [
            {"name": "id", "data_type": "integer", "nullable": False, "default_value": None},
            {"name": "name", "data_type": "text", "nullable": True, "default_value": "'unnamed'::text"},
        ]
    )

    snapshot = await capture_columns(driver, "app", "widget")  # type: ignore[arg-type]

    assert snapshot == [
        {"name": "id", "data_type": "integer", "nullable": False, "default": None},
        {"name": "name", "data_type": "text", "nullable": True, "default": "'unnamed'::text"},
    ]


# --- MCP tool wiring -------------------------------------------------------


async def test_list_audit_events_tool_is_registered_in_read_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "list_audit_events" in listed

        result = await client.call_tool("list_audit_events", {})

    assert result.isError is False
    # Empty FakeDriver -> table-existence check fails -> empty list.
    payload = result.structuredContent
    assert payload is not None
    assert payload["result"] == []


# --- SchemaDiffSnapshot is structured -------------------------------------


def test_schema_diff_snapshot_holds_before_and_after_column_lists() -> None:
    snapshot = SchemaDiffSnapshot(
        schema="app",
        table="widget",
        columns_before=[{"name": "id", "data_type": "integer", "nullable": False, "default": None}],
        columns_after=[
            {"name": "id", "data_type": "integer", "nullable": False, "default": None},
            {"name": "name", "data_type": "text", "nullable": False, "default": None},
        ],
    )
    assert len(snapshot.columns_before) == 1
    assert len(snapshot.columns_after) == 2
    assert snapshot.schema == "app" and snapshot.table == "widget"


def _placeholder(_: Any) -> None:  # quiet unused-import alert for FakeRoutingDriver typing
    return None
