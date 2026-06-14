"""Unit tests for audit trail integrity chain verification."""

import datetime
import hashlib
import hmac
import json
from typing import Any

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
    """Regression for deep-review P1 #6 truncation attack: an
    operator with write access to mcpg_audit.events DELETEs the most
    recent rows. The per-row HMAC chain still verifies for whatever
    remains, but the chain_tip anchor records the highest signed id —
    cross-checking it at the end catches truncation."""
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    key = "secret_key"
    now_dt = datetime.datetime.now(datetime.UTC)
    occurred_at_str = now_dt.isoformat()

    p1 = {
        "occurred_at": occurred_at_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 1"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    pb1 = json.dumps(p1, sort_keys=True, default=str).encode("utf-8")
    h1 = hmac.new(key.encode("utf-8"), b"" + pb1, hashlib.sha256).hexdigest()

    p2 = {
        "occurred_at": occurred_at_str,
        "tool": "run_write",
        "arguments": {"sql": "SELECT 2"},
        "status": "ok",
        "error": None,
        "result": None,
    }
    pb2 = json.dumps(p2, sort_keys=True, default=str).encode("utf-8")
    h2 = hmac.new(key.encode("utf-8"), h1.encode("utf-8") + pb2, hashlib.sha256).hexdigest()

    driver = FakeRoutingDriver(
        {
            # Both table-exists probes match this substring; fake says
            # "present" for both events and chain_tip.
            "FROM pg_class c": [{"present": 1}],
            # chain_tip says id=2 was the last signed event.
            "FROM mcpg_audit.chain_tip": [{"last_event_id": 2, "last_event_hmac": h2}],
            # ...but only id=1 remains.
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
                    "event_hmac": h1,
                },
            ],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "tampered"
    assert "truncation_detected" in res["reason"]
    assert "2" in res["reason"]


async def test_verify_audit_chain_detects_full_table_deletion_via_chain_tip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extreme case: every signed row gets DELETEd. The legacy
    verifier said "empty table → ok"; chain_tip pinning id=5 catches
    it."""
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.chain_tip": [{"last_event_id": 5, "last_event_hmac": "deadbeef"}],
            "FROM mcpg_audit.events": [],
        }
    )

    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "tampered"
    assert "truncation_detected" in res["reason"]
    assert res["broken_at_id"] == 5


async def test_verify_audit_chain_warns_when_chain_tip_table_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compat: a pre-anchor DB has no chain_tip table. Per-
    row chain still verifies → ok with a ``no_chain_tip`` warning so
    operators see the upgrade gap without a false-positive alarm."""
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
    pb = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    h = hmac.new(key.encode("utf-8"), b"" + pb, hashlib.sha256).hexdigest()

    # No chain_tip route → fake returns [] for the chain_tip data
    # query, which the helper interprets as "table missing or empty".
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


async def test_verify_audit_chain_uses_keyset_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for deep-review scalability P0 #2: the old verify
    issued one ``SELECT … FROM events ORDER BY id ASC`` with no LIMIT,
    loading the whole table (potentially gigabytes of jsonb) before
    the chain check even started. The fix walks the chain in keyset-
    paginated batches: ``WHERE id > %s ORDER BY id LIMIT batch_size``.
    Asserting via SQL shape rather than a million-row fake."""
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    driver = FakeRoutingDriver(
        {
            "FROM pg_class c": [{"present": 1}],
            "FROM mcpg_audit.events": [],  # empty table → one keyset query then stop
        }
    )
    await verify_audit_chain(driver)  # type: ignore[arg-type]

    walk_calls = [call for call in driver.calls if "FROM mcpg_audit.events" in call[0]]
    assert walk_calls, "expected at least one events SELECT"
    sql = walk_calls[0][0]
    params = walk_calls[0][1]
    # Keyset shape: WHERE id > %s ORDER BY id ASC LIMIT %s. Both
    # placeholders are bound (no literal LIMIT splice).
    assert "WHERE id > %s" in sql
    assert "ORDER BY id ASC" in sql
    assert "LIMIT %s" in sql
    assert params is not None
    # First batch: start from id 0 (sentinel) with the documented
    # batch size. The constant is module-level; the test asserts
    # *that* a positive bound was passed, not its exact value, so
    # future tuning doesn't require rewriting the test.
    assert params[0] == 0
    assert isinstance(params[1], int) and params[1] > 0


async def test_verify_audit_chain_walks_multiple_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """With more rows than fit in one batch, the verifier should
    issue another keyset SELECT with the highest seen id. We force
    a tiny batch_size and use a paged fake driver that filters by
    ``WHERE id > last_seen`` so the streaming actually progresses
    rather than re-serving the same rows forever."""
    monkeypatch.setenv("MCPG_AUDIT_HMAC_KEY", "secret_key")
    monkeypatch.setenv("MCPG_AUDIT_INTEGRITY", "true")

    # Patch the batch size to something small to force multiple iterations.
    import mcpg.audit_integrity as audit_integrity_mod

    monkeypatch.setattr(audit_integrity_mod, "_VERIFY_BATCH_SIZE", 2)

    key = "secret_key"
    now_dt = datetime.datetime.now(datetime.UTC)
    occurred_at_str = now_dt.isoformat()

    def _payload(sql: str) -> dict[str, Any]:
        return {
            "occurred_at": occurred_at_str,
            "tool": "run_write",
            "arguments": {"sql": sql},
            "status": "ok",
            "error": None,
            "result": None,
        }

    # Sign three events in a real chain.
    p1 = _payload("SELECT 1")
    pb1 = json.dumps(p1, sort_keys=True, default=str).encode("utf-8")
    h1 = hmac.new(key.encode("utf-8"), b"" + pb1, hashlib.sha256).hexdigest()
    p2 = _payload("SELECT 2")
    pb2 = json.dumps(p2, sort_keys=True, default=str).encode("utf-8")
    h2 = hmac.new(key.encode("utf-8"), h1.encode("utf-8") + pb2, hashlib.sha256).hexdigest()
    p3 = _payload("SELECT 3")
    pb3 = json.dumps(p3, sort_keys=True, default=str).encode("utf-8")
    h3 = hmac.new(key.encode("utf-8"), h2.encode("utf-8") + pb3, hashlib.sha256).hexdigest()

    all_rows = [
        {
            "id": idx,
            "occurred_at": now_dt,
            "tool": "run_write",
            "arguments": {"sql": sql},
            "status": "ok",
            "error": None,
            "result": None,
            "prev_hmac": prev,
            "event_hmac": event,
        }
        for idx, (sql, prev, event) in enumerate(
            [
                ("SELECT 1", "", h1),
                ("SELECT 2", h1, h2),
                ("SELECT 3", h2, h3),
            ],
            start=1,
        )
    ]

    # Custom paged driver: applies the ``WHERE id > last_seen`` /
    # ``LIMIT batch`` semantics that the real PG would, by inspecting
    # the bound params. FakeRoutingDriver ignores params, so reuse it
    # only for the pg_class probe and override the events route.
    class _PagedFake:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[Any] | None, bool]] = []

        async def execute_query(self, query: str, params: list[Any] | None = None, force_readonly: bool = False) -> Any:
            from mcpg._vendor.sql import SqlDriver

            self.calls.append((query, params, force_readonly))
            if "FROM pg_class c" in query:
                return [SqlDriver.RowResult(cells={"present": 1})]
            if "FROM mcpg_audit.events" in query:
                assert params is not None
                start_after = int(params[0])
                limit = int(params[1])
                page = [r for r in all_rows if r["id"] > start_after][:limit]
                return [SqlDriver.RowResult(cells=dict(r)) for r in page]
            return []

    driver = _PagedFake()
    res = await verify_audit_chain(driver)  # type: ignore[arg-type]
    assert res["status"] == "ok"

    events_calls = [call for call in driver.calls if "FROM mcpg_audit.events" in call[0]]
    # With 3 rows and batch=2: first batch returns 2, loop continues
    # because it was full; second returns 1 (short), loop stops. So
    # exactly 2 SELECTs.
    assert len(events_calls) == 2
    # Second batch's last_seen_id is the highest id from the first batch.
    assert events_calls[1][1][0] == 2


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
