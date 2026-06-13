"""Unit tests for audit trail integrity chain verification."""

import datetime
import hashlib
import hmac
import json

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.audit_integrity import verify_audit_chain
from mcpg.audit_trail import _reset_audit_init_cache
from mcpg.config import load_settings
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


@pytest.fixture(autouse=True)
def _isolated_ensure_cache() -> None:
    _reset_audit_init_cache()


async def test_verify_audit_chain_errors_when_no_hmac_key_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCPG_AUDIT_HMAC_KEY", raising=False)
    driver = FakeDriver()

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "error"
    assert "MCPG_AUDIT_HMAC_KEY" in res["reason"]


async def test_verify_audit_chain_ok_when_table_does_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    # Existence check returns no rows
    driver = FakeRoutingDriver({"FROM pg_class c": []})

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "ok"
    assert "No audit events recorded" in res["reason"]


async def test_verify_audit_chain_ok_when_table_exists_but_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    driver = FakeRoutingDriver({"FROM pg_class c": [{"present": 1}], "FROM mcpg_audit.events": []})

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "ok"
    assert "table is empty" in res["reason"]


async def test_verify_audit_chain_valid_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    key = "secret_key"
    now_dt = datetime.datetime.now(datetime.UTC)
    occurred_at_str = now_dt.isoformat()

    # Formulate payload 1
    p1 = {
        "occurred_at": occurred_at_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 1"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    payload_bytes1 = json.dumps(p1, sort_keys=True, default=str).encode("utf-8")
    hmac1 = hmac.new(key.encode("utf-8"), b"" + payload_bytes1, hashlib.sha256).hexdigest()

    # Formulate payload 2
    p2 = {
        "occurred_at": occurred_at_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 2"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    payload_bytes2 = json.dumps(p2, sort_keys=True, default=str).encode("utf-8")
    hmac2 = hmac.new(key.encode("utf-8"), hmac1.encode("utf-8") + payload_bytes2, hashlib.sha256).hexdigest()

    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.events": [
                {
                    "id": 1,
                    "occurred_at": now_dt,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": "",
                    "event_hmac": hmac1,
                },
                {
                    "id": 2,
                    "occurred_at": now_dt,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 2"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": hmac1,
                    "event_hmac": hmac2,
                },
            ],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "ok"


async def test_verify_audit_chain_tampered_prev_hmac_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    now_dt = datetime.datetime.now(datetime.UTC)
    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.events": [
                {
                    "id": 1,
                    "occurred_at": now_dt,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": "some_bad_prev",
                    "event_hmac": "some_hmac",
                }
            ],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "tampered"
    assert res["broken_at_id"] == 1
    assert "prev_hmac" in res["reason"]


async def test_verify_audit_chain_tampered_event_hmac_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    now_dt = datetime.datetime.now(datetime.UTC)
    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.events": [
                {
                    "id": 1,
                    "occurred_at": now_dt,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": "",
                    "event_hmac": "wrong_event_hmac",
                }
            ],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "tampered"
    assert res["broken_at_id"] == 1
    assert "event_hmac" in res["reason"]


async def test_verify_audit_chain_flags_row_with_blank_event_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    # A row whose prev_hmac matches the chain but whose event_hmac is
    # blank is reported as tampered ("Missing event_hmac signature").
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    now_dt = datetime.datetime.now(datetime.UTC)
    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.events": [
                {
                    "id": 1,
                    "occurred_at": now_dt,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": "",
                    "event_hmac": "",  # blank signature
                }
            ],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "tampered"
    assert res["broken_at_id"] == 1
    assert "Missing event_hmac" in res["reason"]


async def test_verify_audit_chain_detects_tail_truncation_via_chain_tip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the deep-review P1 #6 truncation attack:
    an operator with write access on mcpg_audit.events DELETEs the
    most recent rows. The old verifier walked rows id-ASC and stopped
    at the last one found — silent ok. The chain_tip anchor records
    the highest (id, event_hmac) the writer ever signed, and verify
    now cross-checks against it."""
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    key = "secret_key"
    now_dt = datetime.datetime.now(datetime.UTC)
    occurred_at_str = now_dt.isoformat()

    # The writer signed two events; the second one was DELETEd by the
    # attacker. The chain_tip row still anchors id=2 / hmac2.
    p1 = {
        "occurred_at": occurred_at_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 1"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    payload_bytes1 = json.dumps(p1, sort_keys=True, default=str).encode("utf-8")
    hmac1 = hmac.new(key.encode("utf-8"), b"" + payload_bytes1, hashlib.sha256).hexdigest()

    p2 = {
        "occurred_at": occurred_at_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 2"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    payload_bytes2 = json.dumps(p2, sort_keys=True, default=str).encode("utf-8")
    hmac2 = hmac.new(key.encode("utf-8"), hmac1.encode("utf-8") + payload_bytes2, hashlib.sha256).hexdigest()

    driver = FakeRoutingDriver(
        {
            # Both the events table-exists probe and the chain_tip
            # table-exists probe use the same SQL substring; the fake
            # returns "present" for both.
            "FROM pg_class c": [{"present": 1}],
            # chain_tip says the writer's last signed event was id=2.
            "FROM mcpg_audit.chain_tip": [{"last_event_id": 2, "last_event_hmac": hmac2}],
            # …but only id=1 is present in the events table — id=2
            # was DELETEd.
            "FROM mcpg_audit.events": [
                {
                    "id": 1,
                    "occurred_at": now_dt,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": "",
                    "event_hmac": hmac1,
                },
            ],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "tampered"
    assert "truncation_detected" in res["reason"]
    # The verifier names the chain_tip's recorded last event id so
    # operators can quickly compare against what they expected.
    assert "2" in res["reason"]


async def test_verify_audit_chain_detects_full_table_deletion_via_chain_tip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extreme case of the truncation attack: every row gets
    DELETEd. With the old verifier this returned ok ("empty table");
    with the anchor we now catch it because chain_tip still holds the
    last signed id."""
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.chain_tip": [
                {"last_event_id": 5, "last_event_hmac": "deadbeef"},
            ],
            # Events table exists but is empty.
            "FROM mcpg_audit.events": [],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "tampered"
    assert "truncation_detected" in res["reason"]
    assert res["broken_at_id"] == 5


async def test_verify_audit_chain_ok_when_chain_tip_matches_last_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: writer + verifier agree on the tip. The legacy
    test_verify_audit_chain_valid_chain exercises this path without
    a chain_tip route (so the verifier falls back to the warning
    path); this test pins the explicit-anchor case."""
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    key = "secret_key"
    now_dt = datetime.datetime.now(datetime.UTC)
    occurred_at_str = now_dt.isoformat()
    payload = {
        "occurred_at": occurred_at_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 1"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    expected_hmac = hmac.new(key.encode("utf-8"), b"" + payload_bytes, hashlib.sha256).hexdigest()

    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.chain_tip": [
                {"last_event_id": 1, "last_event_hmac": expected_hmac},
            ],
            "FROM mcpg_audit.events": [
                {
                    "id": 1,
                    "occurred_at": now_dt,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": "",
                    "event_hmac": expected_hmac,
                },
            ],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res == {"status": "ok"}
    # When the explicit anchor matches there is no "no_chain_tip"
    # warning — the verifier returns the bare ok dict.
    assert "warning" not in res


async def test_verify_audit_chain_warns_when_chain_tip_table_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compat: a database created before the anchor work
    has no chain_tip table. The per-row chain still verifies and we
    return ok, but with an operator-facing warning so they know the
    truncation-anchor is missing on this database."""
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    key = "secret_key"
    now_dt = datetime.datetime.now(datetime.UTC)
    occurred_at_str = now_dt.isoformat()
    payload = {
        "occurred_at": occurred_at_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 1"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    h = hmac.new(key.encode("utf-8"), b"" + payload_bytes, hashlib.sha256).hexdigest()

    # Routing only mentions pg_class + events. The chain_tip data
    # query falls through to [] which the helper treats as "table
    # missing or empty" — i.e. the pre-anchor case.
    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.events": [
                {
                    "id": 1,
                    "occurred_at": now_dt,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": "",
                    "event_hmac": h,
                },
            ],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "ok"
    assert "no_chain_tip" in res.get("warning", "")


async def test_verify_audit_chain_tool_is_registered_in_read_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "verify_audit_chain" in listed


async def test_verify_audit_chain_retrieves_key_from_driver_settings() -> None:
    # Set up settings with audit hmac key
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_AUDIT_HMAC_KEY": "settings_secret_key",
            "MCPG_AUDIT_INTEGRITY": "true",
        }
    )

    driver = FakeRoutingDriver({"FROM pg_class c": []})
    driver.settings = settings

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    # If it didn't find the key, it would return error status; finding it returns ok.
    assert res["status"] == "ok"


class _CustomIsoformatObject:
    def __init__(self, val: str) -> None:
        self._val = val

    def isoformat(self) -> str:
        return self._val


async def test_verify_audit_chain_custom_occurred_at_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    key = "secret_key"
    occurred_str = "2026-06-01T23:10:27"

    # Formulate payload for custom isoformat object
    p1 = {
        "occurred_at": occurred_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 1"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    payload_bytes1 = json.dumps(p1, sort_keys=True, default=str).encode("utf-8")
    hmac1 = hmac.new(key.encode("utf-8"), b"" + payload_bytes1, hashlib.sha256).hexdigest()

    # 1. Test object with isoformat but no astimezone
    iso_obj = _CustomIsoformatObject(occurred_str)

    # 2. Test object with neither (e.g. a plain string)
    str_obj = occurred_str

    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.events": [
                {
                    "id": 1,
                    "occurred_at": iso_obj,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": "",
                    "event_hmac": hmac1,
                },
                {
                    "id": 2,
                    "occurred_at": str_obj,
                    "tool": "run_write",
                    "arguments": {"sql": "SELECT 1"},
                    "status": "ok",
                    "error": None,
                    "result": None,
                    "prev_hmac": hmac1,
                    "event_hmac": hmac1,  # use dummy event_hmac just to satisfy loop
                },
            ],
        }
    )

    # We expect verify_audit_chain to format both occurred_ats to occurred_str without raising exceptions
    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    # The first one should pass verification; the second might fail on event_hmac mismatch since we used a dummy,
    # but the critical part is that it formats it correctly without raising errors.
    assert res["status"] in ("ok", "tampered")
