"""Tests for read-replica routing (Phase 1.6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from mcpg.replicas import (
    DEFAULT_DEGRADED_RETRY_SECONDS,
    ReplicaError,
    ReplicaInfo,
    ReplicaPool,
    RoutedSqlDriver,
)

# We can't open real psycopg pools in a unit test, so the integration-
# level construction (which calls DbConnPool inside ReplicaPool.__init__)
# is exercised by injecting a faked pool via the public ``ReplicaPool``
# state. The routing logic itself is provider-agnostic — it picks
# replicas based on health and delegates to a driver.


@dataclass
class _RecordingDriver:
    """SqlDriver double that records every execute_query and returns a tag.

    ``raises`` simulates a transport failure so the routing code can
    exercise the fallback path.
    """

    tag: str
    raises: BaseException | None = None
    calls: list[tuple[str, Any, bool]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []
        # Surface attrs that RoutedSqlDriver.__init__ mirrors.
        self.conn = object()
        self.is_pool = True

    async def execute_query(
        self,
        query: str,
        params: list[Any] | None = None,
        force_readonly: bool = False,
    ) -> list[Any]:
        self.calls.append((query, params, force_readonly))
        if self.raises is not None:
            raise self.raises
        return [self.tag]  # type: ignore[list-item]


def _build_pool(size: int = 2) -> ReplicaPool:
    """Construct a ReplicaPool without opening psycopg pools.

    ``ReplicaPool.__init__`` instantiates ``DbConnPool`` per DSN; we
    accept that — psycopg-pool defers the actual connect until
    ``pool_connect()`` so construction is cheap and side-effect-free.
    """
    return ReplicaPool(
        tuple(f"postgresql://u:p@replica-{i}/db" for i in range(size)),
        pool_min_size=1,
        pool_max_size=1,
    )


def test_replica_pool_rejects_empty_dsn_list() -> None:
    with pytest.raises(ReplicaError):
        ReplicaPool((), pool_min_size=1, pool_max_size=1)


async def test_replica_pool_next_healthy_round_robins_through_replicas() -> None:
    pool = _build_pool(size=2)

    picks = []
    for _ in range(4):
        candidate = await pool.next_healthy()
        assert candidate is not None
        picks.append(candidate.index)

    # Two replicas, four picks → 0, 1, 0, 1.
    assert picks == [0, 1, 0, 1]


async def test_replica_pool_mark_degraded_removes_replica_from_rotation() -> None:
    pool = _build_pool(size=2)

    await pool.mark_degraded(0, "connection refused")

    for _ in range(4):
        candidate = await pool.next_healthy()
        assert candidate is not None
        assert candidate.index == 1  # only healthy one left


async def test_replica_pool_returns_none_when_every_replica_is_degraded() -> None:
    pool = _build_pool(size=2)
    await pool.mark_degraded(0, "down")
    await pool.mark_degraded(1, "down")

    assert await pool.next_healthy() is None


async def test_replica_pool_snapshot_obfuscates_password_and_reports_state() -> None:
    pool = _build_pool(size=1)
    await pool.mark_degraded(0, "boom")

    snapshot = await pool.snapshot()
    assert len(snapshot) == 1
    info = snapshot[0]
    assert isinstance(info, ReplicaInfo)
    assert info.index == 0
    assert ":p@" not in info.dsn  # password obfuscated
    assert info.degraded is True
    assert info.last_error == "boom"
    assert 0 < info.seconds_until_retry <= DEFAULT_DEGRADED_RETRY_SECONDS


async def test_routed_driver_sends_writes_to_primary() -> None:
    primary = _RecordingDriver(tag="primary")
    replica = _RecordingDriver(tag="replica")
    pool = _build_pool(size=1)
    driver = RoutedSqlDriver(primary=primary, replicas=[replica], replica_pool=pool)  # type: ignore[arg-type]

    result = await driver.execute_query("INSERT INTO t VALUES (1)", force_readonly=False)

    assert result == ["primary"]
    assert len(primary.calls) == 1
    assert replica.calls == []  # writes never touch replicas


async def test_routed_driver_sends_reads_to_a_replica() -> None:
    primary = _RecordingDriver(tag="primary")
    replica = _RecordingDriver(tag="replica")
    pool = _build_pool(size=1)
    driver = RoutedSqlDriver(primary=primary, replicas=[replica], replica_pool=pool)  # type: ignore[arg-type]

    result = await driver.execute_query("SELECT 1", force_readonly=True)

    assert result == ["replica"]
    assert primary.calls == []
    assert len(replica.calls) == 1


async def test_routed_driver_falls_back_to_primary_when_replica_fails() -> None:
    primary = _RecordingDriver(tag="primary")
    flaky = _RecordingDriver(tag="replica", raises=RuntimeError("connection lost"))
    pool = _build_pool(size=1)
    driver = RoutedSqlDriver(primary=primary, replicas=[flaky], replica_pool=pool)  # type: ignore[arg-type]

    result = await driver.execute_query("SELECT 1", force_readonly=True)

    assert result == ["primary"]
    # The replica was tried first, then the primary picked up the call.
    assert len(flaky.calls) == 1
    assert len(primary.calls) == 1
    # And the failed replica is now degraded.
    snapshot = await pool.snapshot()
    assert snapshot[0].degraded is True
    assert "connection lost" in (snapshot[0].last_error or "")


async def test_routed_driver_routes_to_primary_when_every_replica_is_degraded() -> None:
    primary = _RecordingDriver(tag="primary")
    replica = _RecordingDriver(tag="replica")
    pool = _build_pool(size=1)
    await pool.mark_degraded(0, "down")
    driver = RoutedSqlDriver(primary=primary, replicas=[replica], replica_pool=pool)  # type: ignore[arg-type]

    result = await driver.execute_query("SELECT 1", force_readonly=True)

    # No healthy replica → primary fallback path.
    assert result == ["primary"]
    assert replica.calls == []
    assert len(primary.calls) == 1


async def test_replica_pool_connect_failure_is_temporarily_degraded_not_permanent() -> None:
    """Regression: a startup connect failure must be the same finite
    retry window as a per-query failure. Earlier code set
    degraded_until=inf which permanently disabled the replica until
    a restart; we want a transient DNS / network blip at startup to
    self-heal at the next sweep."""
    import time as _time
    import unittest.mock as _mock

    # ReplicaPool.__init__ builds DbConnPools eagerly but does NOT
    # connect them. Make pool_connect() raise to simulate a startup
    # failure, then check the degraded window is finite.
    pool = _build_pool(size=1)
    state = pool._states[0]
    with _mock.patch.object(state.pool, "pool_connect", side_effect=RuntimeError("DNS failure")):
        await pool.connect()

    assert state.degraded_until != float("inf")
    assert state.degraded_until > _time.monotonic()
    assert "DNS failure" in (state.last_error or "")
    # After the retry window passes, the replica returns to service.
    snapshot = await pool.snapshot()
    assert snapshot[0].seconds_until_retry <= DEFAULT_DEGRADED_RETRY_SECONDS
