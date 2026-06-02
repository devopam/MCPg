"""Tests for the lock-inspection tools (Phase 4.5)."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.locks import (
    BlockingChainDetail,
    BlockingGraphReport,
    BlockingPair,
    LockInfo,
    find_blocking_chains,
    list_locks,
    walk_blocking_chains,
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


async def test_walk_blocking_chains_returns_empty_when_no_blocks() -> None:
    driver = FakeRoutingDriver({"unnest(pg_blocking_pids": []})
    report = await walk_blocking_chains(driver)  # type: ignore[arg-type]

    assert isinstance(report, BlockingGraphReport)
    assert report.cycles == []
    assert report.paths == []
    assert report.roots == []
    assert report.nodes == {}
    assert report.mermaid == (
        "graph TD\n"
        "  classDef root fill:#ff9999,stroke:#333,stroke-width:2px;\n"
        "  classDef cycle fill:#ffff99,stroke:#333,stroke-width:2px;"
    )


async def test_walk_blocking_chains_linear_chain() -> None:
    driver = FakeRoutingDriver(
        {
            "unnest(pg_blocking_pids": [
                {
                    "blocked_pid": 200,
                    "blocked_query": "UPDATE `widget` SET x = 1",
                    "blocked_application_name": "appA",
                    "blocked_wait_event": "transactionid",
                    "blocked_state": "active",
                    "blocking_pid": 201,
                    "blocking_query": "UPDATE widget SET x = 2",
                    "blocking_application_name": "appB",
                    "blocking_wait_event": "transactionid",
                    "blocking_state": "active",
                },
                {
                    "blocked_pid": 201,
                    "blocked_query": "UPDATE widget SET x = 2",
                    "blocked_application_name": "appB",
                    "blocked_wait_event": "transactionid",
                    "blocked_state": "active",
                    "blocking_pid": 202,
                    "blocking_query": "UPDATE widget SET x = 3",
                    "blocking_application_name": "appC",
                    "blocking_wait_event": None,
                    "blocking_state": "idle in transaction",
                },
            ]
        }
    )

    report = await walk_blocking_chains(driver)  # type: ignore[arg-type]

    assert report.cycles == []
    assert report.roots == [202]
    assert report.paths == [[200, 201, 202]]
    assert len(report.nodes) == 3

    assert isinstance(report.nodes[200], BlockingChainDetail)
    assert report.nodes[200].pid == 200
    assert report.nodes[200].application_name == "appA"

    assert "200[\"PID 200 (appA) [active]<br/>`UPDATE 'widget' SET x = 1`\"]" in report.mermaid
    assert '200 -->|"transactionid"| 201' in report.mermaid
    assert '201 -->|"transactionid"| 202' in report.mermaid
    assert "class 202 root;" in report.mermaid


async def test_walk_blocking_chains_deadlock_cycle() -> None:
    driver = FakeRoutingDriver(
        {
            "unnest(pg_blocking_pids": [
                {
                    "blocked_pid": 200,
                    "blocked_query": "UPDATE widget SET x = 1",
                    "blocked_application_name": "appA",
                    "blocked_wait_event": "transactionid",
                    "blocked_state": "active",
                    "blocking_pid": 201,
                    "blocking_query": "UPDATE widget SET x = 2",
                    "blocking_application_name": "appB",
                    "blocking_wait_event": "transactionid",
                    "blocking_state": "active",
                },
                {
                    "blocked_pid": 201,
                    "blocked_query": "UPDATE widget SET x = 2",
                    "blocked_application_name": "appB",
                    "blocked_wait_event": "transactionid",
                    "blocked_state": "active",
                    "blocking_pid": 200,
                    "blocking_query": "UPDATE widget SET x = 1",
                    "blocking_application_name": "appA",
                    "blocking_wait_event": "transactionid",
                    "blocking_state": "active",
                },
            ]
        }
    )

    report = await walk_blocking_chains(driver)  # type: ignore[arg-type]

    assert report.cycles == [[200, 201, 200]]
    assert report.roots == []
    assert report.paths == []
    assert len(report.nodes) == 2

    assert '200 -->|"transactionid"| 201' in report.mermaid
    assert '201 -->|"transactionid"| 200' in report.mermaid
    assert "class 200 cycle;" in report.mermaid
    assert "class 201 cycle;" in report.mermaid


async def test_walk_blocking_chains_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        await walk_blocking_chains(FakeRoutingDriver({}), limit=0)  # type: ignore[arg-type]
