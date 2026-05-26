"""Tests for the lock-inspection tools (Phase 4.5)."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.locks import (
    BlockingPair,
    LockInfo,
    find_blocking_chains,
    list_locks,
)


async def test_list_locks_returns_typed_lock_rows() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_locks": [
                {
                    "pid": 1234,
                    "locktype": "relation",
                    "mode": "AccessShareLock",
                    "granted": True,
                    "relation": "public.widget",
                    "transactionid": None,
                    "virtualxid": None,
                    "application_name": "psql",
                    "state": "idle in transaction",
                    "wait_event_type": None,
                    "wait_event": None,
                    "query": "SELECT * FROM widget",
                },
                {
                    "pid": 1235,
                    "locktype": "transactionid",
                    "mode": "ExclusiveLock",
                    "granted": False,
                    "relation": None,
                    "transactionid": 9999,
                    "virtualxid": "3/42",
                    "application_name": None,
                    "state": "active",
                    "wait_event_type": "Lock",
                    "wait_event": "transactionid",
                    "query": "UPDATE widget SET name = 'x'",
                },
            ]
        }
    )

    rows = await list_locks(driver)  # type: ignore[arg-type]

    assert len(rows) == 2
    assert isinstance(rows[0], LockInfo)
    assert rows[0].relation == "public.widget"
    assert rows[1].granted is False
    assert rows[1].transactionid == 9999
    assert rows[1].virtualxid == "3/42"


async def test_list_locks_returns_empty_list_when_no_locks_held() -> None:
    driver = FakeRoutingDriver({"pg_locks": []})
    rows = await list_locks(driver)  # type: ignore[arg-type]
    assert rows == []


async def test_list_locks_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        await list_locks(FakeRoutingDriver({}), limit=0)  # type: ignore[arg-type]


async def test_find_blocking_chains_returns_typed_pairs() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_stat_activity blocked": [
                {
                    "blocked_pid": 100,
                    "blocked_query": "UPDATE widget SET name = 'a'",
                    "blocked_application_name": "app-A",
                    "blocked_wait_event": "transactionid",
                    "blocking_pid": 200,
                    "blocking_query": "UPDATE widget SET name = 'b'",
                    "blocking_application_name": "app-B",
                    "blocking_state": "idle in transaction",
                }
            ]
        }
    )

    pairs = await find_blocking_chains(driver)  # type: ignore[arg-type]

    assert len(pairs) == 1
    pair = pairs[0]
    assert isinstance(pair, BlockingPair)
    assert pair.blocked_pid == 100
    assert pair.blocking_pid == 200
    assert pair.blocking_state == "idle in transaction"


async def test_find_blocking_chains_returns_empty_list_when_nothing_blocked() -> None:
    driver = FakeRoutingDriver({"pg_stat_activity blocked": []})
    pairs = await find_blocking_chains(driver)  # type: ignore[arg-type]
    assert pairs == []


async def test_find_blocking_chains_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        await find_blocking_chains(FakeRoutingDriver({}), limit=0)  # type: ignore[arg-type]
