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


async def test_verify_audit_chain_tool_is_registered_in_read_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "verify_audit_chain" in listed
