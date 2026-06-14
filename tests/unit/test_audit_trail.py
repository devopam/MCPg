"""Tests for the SQL audit-trail module and its MCP read tool."""

import hashlib
import hmac
import json
from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.audit_trail import (
    AUDIT_SCHEMA,
    AUDIT_TABLE,
    AuditTrailEntry,
    AuditTrailError,
    SchemaDiffSnapshot,
    _redact,
    _reset_audit_init_cache,
    capture_columns,
    ensure_audit_table,
    list_audit_events,
    prune_audit_events,
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


async def test_record_audit_redacts_dsn_credentials_in_error_field() -> None:
    """Regression for the deep-review P1 #5: the ``error`` column got
    persisted verbatim. ``write._persist_audit`` fills it with
    ``str(exc)``, and psycopg / libpq error messages routinely embed
    DSN fragments (``host=… password=…``) or DSN URIs. The same
    obfuscate_password sweep that the arguments / result paths run
    must apply here too."""
    _reset_audit_init_cache()
    driver = FakeDriver()

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_write",
        arguments={"sql": "SELECT 1"},
        status="error",
        error=("connection failed: postgresql://alice:hunter2@db.example.com:5432/app"),
    )

    insert_calls = [call for call in driver.calls if "INSERT INTO mcpg_audit.events" in call[0]]
    params = insert_calls[0][1]
    assert params is not None
    persisted_error = params[3]
    assert isinstance(persisted_error, str)
    # The plaintext password must be scrubbed; the rest of the message
    # is preserved so operators still see WHY the call failed.
    assert "hunter2" not in persisted_error
    assert "connection failed" in persisted_error


async def test_record_audit_hmac_signs_the_redacted_error_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HMAC payload must sign exactly what gets persisted —
    otherwise the redaction-and-sign races would either break
    verify_audit_chain or sign plaintext that's about to be redacted.
    This test pins the invariant by recomputing the expected hmac
    against the redacted error form and asserting it matches the
    value the writer attached."""
    _reset_audit_init_cache()
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "test_hmac_key_32bytes_minimum_abc!")
    driver = FakeDriver()

    raw_error = "psycopg.OperationalError: connection failed: postgresql://u:p4ss@db/app"

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_ddl",
        arguments={"sql": "DROP TABLE x"},
        status="error",
        error=raw_error,
    )

    insert_calls = [call for call in driver.calls if "INSERT INTO mcpg_audit.events" in call[0]]
    persisted_error = insert_calls[0][1][3]
    event_hmac = insert_calls[0][1][7]
    occurred_at = insert_calls[0][1][5]

    # The redacted form is signed; the raw form is NOT.
    assert "p4ss" not in persisted_error
    expected = _expected_event_hmac(
        key="test_hmac_key_32bytes_minimum_abc!",
        prev_hmac="",
        occurred_at_str=occurred_at.isoformat(),
        tool="run_ddl",
        arguments={"sql": "DROP TABLE x"},
        status="error",
        error=persisted_error,
        result=None,
    )
    assert event_hmac == expected


# --- record_audit HMAC integrity chain -------------------------------------


def _expected_event_hmac(
    *,
    key: str,
    prev_hmac: str,
    occurred_at_str: str,
    tool: str,
    arguments: dict[str, Any],
    status: str,
    error: str | None,
    result: dict[str, Any] | None,
) -> str:
    """Recompute the event HMAC exactly as ``record_audit`` does."""
    payload = {
        "occurred_at": occurred_at_str,
        "tool": tool,
        "arguments": arguments,
        "status": status,
        "error": error,
        "result": result,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    data_to_sign = prev_hmac.encode("utf-8") + payload_bytes
    return hmac.new(key.encode("utf-8"), data_to_sign, hashlib.sha256).hexdigest()


async def test_record_audit_signs_first_event_with_empty_prev_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    # Integrity enabled via the conventional env vars, no driver.settings.
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    _reset_audit_init_cache()
    # No prior row routed -> ORDER BY id DESC returns [] -> prev_hmac == "".
    driver = FakeRoutingDriver({})

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_write",
        arguments={"sql": "SELECT 1"},
        status="ok",
    )

    insert = next(call for call in driver.calls if "INSERT INTO" in call[0])
    params = insert[1]
    assert params is not None
    occurred_at_str = params[5].isoformat()
    # prev_hmac column is "" for the genesis row; event_hmac is signed.
    assert params[6] == ""
    assert params[7] == _expected_event_hmac(
        key="secret_key",
        prev_hmac="",
        occurred_at_str=occurred_at_str,
        tool="run_write",
        arguments={"sql": "SELECT 1"},
        status="ok",
        error=None,
        result=None,
    )


async def test_record_audit_chains_onto_the_previous_events_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    _reset_audit_init_cache()
    # A prior row exists; its event_hmac becomes this row's prev_hmac.
    driver = FakeRoutingDriver({"ORDER BY id DESC": [{"event_hmac": "PREVHMAC"}]})

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_ddl",
        arguments={"sql": "CREATE TABLE x()"},
        status="ok",
    )

    insert = next(call for call in driver.calls if "INSERT INTO" in call[0])
    params = insert[1]
    assert params is not None
    occurred_at_str = params[5].isoformat()
    assert params[6] == "PREVHMAC"
    assert params[7] == _expected_event_hmac(
        key="secret_key",
        prev_hmac="PREVHMAC",
        occurred_at_str=occurred_at_str,
        tool="run_ddl",
        arguments={"sql": "CREATE TABLE x()"},
        status="ok",
        error=None,
        result=None,
    )


async def test_record_audit_reads_integrity_config_from_driver_settings() -> None:
    # When the driver carries Settings, record_audit uses those instead
    # of re-reading the environment.
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_AUDIT_INTEGRITY": "true",
            "MCPG_AUDIT_HMAC_KEY": "from_settings",
        }
    )
    _reset_audit_init_cache()
    driver = FakeRoutingDriver({})
    driver.settings = settings  # type: ignore[attr-defined]

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_write",
        arguments={"sql": "SELECT 1"},
        status="ok",
    )

    insert = next(call for call in driver.calls if "INSERT INTO" in call[0])
    params = insert[1]
    assert params is not None
    occurred_at_str = params[5].isoformat()
    assert params[7] == _expected_event_hmac(
        key="from_settings",
        prev_hmac="",
        occurred_at_str=occurred_at_str,
        tool="run_write",
        arguments={"sql": "SELECT 1"},
        status="ok",
        error=None,
        result=None,
    )


async def test_record_audit_treats_malformed_integrity_flag_as_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-boolean MCPG_AUDIT_INTEGRITY must not crash record_audit; the
    # parse error is swallowed and integrity stays off (HMAC columns NULL).
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "not-a-bool")
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    _reset_audit_init_cache()
    driver = FakeRoutingDriver({})

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_write",
        arguments={"sql": "SELECT 1"},
        status="ok",
    )

    insert = next(call for call in driver.calls if "INSERT INTO" in call[0])
    params = insert[1]
    assert params is not None
    assert params[7] is None  # event_hmac stays NULL


async def test_record_audit_leaves_hmac_columns_null_when_integrity_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Integrity off (default): no prev-row SELECT, and both HMAC columns
    # are written as NULL.
    monkeypatch.delenv("MCPG_AUDIT_INTEGRITY", raising=False)
    monkeypatch.delenv("MCPG_AUDIT_HMAC_KEY", raising=False)
    _reset_audit_init_cache()
    driver = FakeRoutingDriver({})

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_write",
        arguments={"sql": "SELECT 1"},
        status="ok",
    )

    assert not any("ORDER BY id DESC" in call[0] for call in driver.calls)
    insert = next(call for call in driver.calls if "INSERT INTO" in call[0])
    params = insert[1]
    assert params is not None
    assert params[6] is None  # prev_hmac
    assert params[7] is None  # event_hmac
    # Integrity OFF must not write to chain_tip. The CREATE TABLE
    # IF NOT EXISTS chain_tip in ensure_audit_table is fine — the
    # table is provisioned up-front so the first integrity-enabled
    # call doesn't have to migrate it. What matters is that the
    # data-path call stays out of it.
    assert not any("INSERT INTO mcpg_audit.chain_tip" in call[0] for call in driver.calls)


async def test_record_audit_upserts_chain_tip_atomically_when_integrity_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for deep-review P1 #6 truncation anchor: when
    integrity is on, the event INSERT and the chain_tip UPSERT must
    happen in a single PostgreSQL statement (writable CTE) so an
    attacker can't race between them. Test pins the SQL shape."""
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "test_hmac_key_32bytes_minimum_abc!")
    _reset_audit_init_cache()
    driver = FakeDriver()

    await record_audit(  # type: ignore[arg-type]
        driver,
        tool="run_write",
        arguments={"sql": "SELECT 1"},
        status="ok",
    )

    # ensure_audit_table issues CREATE TABLE chain_tip up-front; the
    # data-path call is the one that INSERTs into both via the
    # writable-CTE form.
    write_call = next(
        call for call in driver.calls if "INSERT INTO mcpg_audit.chain_tip" in call[0] and "WITH new_event" in call[0]
    )
    sql = write_call[0]
    assert "WITH new_event" in sql
    assert "INSERT INTO mcpg_audit.events" in sql
    assert "RETURNING id, event_hmac" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    # Single writable-CTE statement → exactly one data-path call.
    data_path_calls = [
        call for call in driver.calls if "INSERT INTO mcpg_audit.chain_tip" in call[0] and "WITH new_event" in call[0]
    ]
    assert len(data_path_calls) == 1


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


# --- prune_audit_events ----------------------------------------------------


async def test_prune_audit_events_deletes_old_rows_and_reports_counts() -> None:
    import datetime

    cutoff_dt = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            # CTE DELETE returns a single aggregated row (count + cutoff).
            "DELETE FROM mcpg_audit.events": [{"deleted_count": 3, "cutoff_ts": cutoff_dt}],
            "count(*) AS n": [{"n": 7}],
        }
    )

    result = await prune_audit_events(driver, older_than_days=30)  # type: ignore[arg-type]

    assert result.deleted == 3
    assert result.remaining == 7
    assert result.cutoff == cutoff_dt.isoformat()
    # The DELETE was a write (force_readonly False).
    delete_call = next(call for call in driver.calls if "DELETE FROM" in call[0])
    assert delete_call[2] is False
    assert delete_call[1] == [30]


async def test_prune_audit_events_refuses_when_integrity_enabled() -> None:
    driver = FakeRoutingDriver({"FROM pg_class c": [{"present": 1}]})

    with pytest.raises(AuditTrailError, match="MCPG_AUDIT_INTEGRITY"):
        await prune_audit_events(driver, older_than_days=30, integrity_enabled=True)  # type: ignore[arg-type]

    # Nothing was deleted — it bailed before touching the table.
    assert not any("DELETE FROM" in call[0] for call in driver.calls)


async def test_prune_audit_events_rejects_non_positive_window() -> None:
    with pytest.raises(AuditTrailError, match="older_than_days"):
        await prune_audit_events(FakeDriver(), older_than_days=0)  # type: ignore[arg-type]


async def test_prune_audit_events_is_a_noop_when_table_absent() -> None:
    # Existence check returns no rows -> table not created yet.
    driver = FakeRoutingDriver({"FROM pg_class c": []})

    result = await prune_audit_events(driver, older_than_days=30)  # type: ignore[arg-type]

    assert result.deleted == 0
    assert result.remaining == 0
    assert not any("DELETE FROM" in call[0] for call in driver.calls)


async def test_prune_audit_events_tool_is_registered_in_unrestricted_mode() -> None:
    unrestricted = load_settings(
        {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
    )
    driver = FakeRoutingDriver({"FROM pg_class c": []})
    server = create_server(unrestricted, database=FakeDatabase(driver))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "prune_audit_events" in listed
        result = await client.call_tool("prune_audit_events", {"older_than_days": 90})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["deleted"] == 0


@pytest.mark.parametrize("mode", ["read-only", "restricted"])
async def test_prune_audit_events_tool_is_absent_without_write_capability(mode: str) -> None:
    # prune deletes rows -> it must only appear in unrestricted mode.
    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": mode})
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}

    assert "prune_audit_events" not in listed


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
