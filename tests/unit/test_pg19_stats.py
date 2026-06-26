"""Tests for the PG 19 lock + recovery analytics module."""

from __future__ import annotations

from _fakes import FakeDriver, FakeRoutingDriver

from mcpg.pg19_stats import (
    LockHotspot,
    LockHotspotsResult,
    LockStatRow,
    Pg19StatsStatus,
    RecoveryStatRow,
    analyze_lock_hotspots,
    get_pg19_stats_status,
    read_pg_stat_lock,
    read_pg_stat_recovery,
)


def _version_route(num: int, ver: str) -> dict[str, list[dict[str, object]]]:
    """Helper — wires the server-version probe to a specific version."""
    return {"current_setting('server_version_num')": [{"ver_num": num, "ver": ver}]}


def _view_present_route(views: dict[str, bool]) -> dict[str, list[dict[str, object]]]:
    """Helper — wires the pg_class view-presence probe.

    The view-presence query is parameterised by view name, but
    FakeRoutingDriver routes by substring, so we wire one route per
    expected view name using a fingerprint substring that's stable for
    each call (the param is bound, so query text is identical for each
    view — we have to use FakeParamRoutingDriver or special-case the
    helper). For these tests we use a single combined route: the
    helper returns a "present" row for any view query, and individual
    tests override by setting the rows for "FROM pg_class c" to []
    when both views should be absent.
    """
    # This helper is only used in the *available* tests where both
    # views are present. The simpler case (both absent) is captured by
    # not setting this route at all.
    present_any = any(views.values())
    return {"FROM pg_class c ": [{"present": 1}] if present_any else []}


# --- get_pg19_stats_status -------------------------------------------------


async def test_status_available_on_pg19_with_both_views() -> None:
    routes = _version_route(190001, "19beta1")
    routes.update(_view_present_route({"pg_stat_lock": True, "pg_stat_recovery": True}))
    driver = FakeRoutingDriver(routes)
    status = await get_pg19_stats_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, Pg19StatsStatus)
    assert status.available is True
    assert status.has_pg_stat_lock is True
    assert status.has_pg_stat_recovery is True
    assert "pg_stat_lock" in status.detail


async def test_status_unavailable_on_pg18_with_diagnostic() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    status = await get_pg19_stats_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.has_pg_stat_lock is False
    assert status.has_pg_stat_recovery is False
    assert "find_blocking_chains" in status.detail


async def test_status_never_raises_on_driver_failure() -> None:
    """get_pg19_stats_status documents 'never raises' — confirm with FakeDriver(fail=True)."""
    driver = FakeDriver(fail=True)
    status = await get_pg19_stats_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "find_blocking_chains" in status.detail


async def test_status_unavailable_on_pg19_without_views() -> None:
    """PG 19 server up but neither view present (early Beta build)."""
    routes = _version_route(190001, "19beta1")
    # No view-present rows — _view_present returns False for both.
    driver = FakeRoutingDriver(routes)
    status = await get_pg19_stats_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.has_pg_stat_lock is False
    assert status.has_pg_stat_recovery is False
    assert "fall back" in status.detail.lower()


# --- read_pg_stat_lock -----------------------------------------------------


async def test_read_lock_empty_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    rows = await read_pg_stat_lock(driver)  # type: ignore[arg-type]
    assert rows == []


async def test_read_lock_returns_rows_on_pg19() -> None:
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_lock"] = [
        {"lock_type": "relation", "acquires": 100, "waits": 5, "wait_time_us": 1_500_000},
        {"lock_type": "tuple", "acquires": 50, "waits": 2, "wait_time_us": 100_000},
    ]
    driver = FakeRoutingDriver(routes)
    rows = await read_pg_stat_lock(driver)  # type: ignore[arg-type]
    assert rows == [
        LockStatRow(lock_type="relation", acquires=100, waits=5, wait_time_us=1_500_000),
        LockStatRow(lock_type="tuple", acquires=50, waits=2, wait_time_us=100_000),
    ]


async def test_read_lock_propagates_stats_reset_when_present() -> None:
    """PG 19 added ``stats_reset`` to every ``pg_stat_*`` view that
    didn't already have it; the reader surfaces it as a string so
    callers can tell "no contention" from "counters were just reset"."""
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_lock"] = [
        {
            "lock_type": "relation",
            "acquires": 0,
            "waits": 0,
            "wait_time_us": 0,
            "stats_reset": "2026-06-26 12:00:00+00",
        }
    ]
    driver = FakeRoutingDriver(routes)
    rows = await read_pg_stat_lock(driver)  # type: ignore[arg-type]
    assert rows == [
        LockStatRow(
            lock_type="relation",
            acquires=0,
            waits=0,
            wait_time_us=0,
            stats_reset="2026-06-26 12:00:00+00",
        )
    ]


async def test_read_lock_empty_when_view_absent_on_pg19() -> None:
    """PG 19 but pg_stat_lock view is missing — early Beta."""
    routes = _version_route(190001, "19beta1")
    # _view_present returns False for missing view.
    driver = FakeRoutingDriver(routes)
    rows = await read_pg_stat_lock(driver)  # type: ignore[arg-type]
    assert rows == []


# --- read_pg_stat_recovery -------------------------------------------------


async def test_read_recovery_returns_row_for_standby() -> None:
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_recovery"] = [
        {
            "replay_lsn": "0/1234ABCD",
            "replay_lag_seconds": 0.5,
            "last_replayed_at": "2026-06-20 18:00:00+00",
            "startup_state": "streaming",
        }
    ]
    driver = FakeRoutingDriver(routes)
    rows = await read_pg_stat_recovery(driver)  # type: ignore[arg-type]
    assert rows == [
        RecoveryStatRow(
            replay_lsn="0/1234ABCD",
            replay_lag_seconds=0.5,
            last_replayed_at="2026-06-20 18:00:00+00",
            startup_state="streaming",
        )
    ]


async def test_read_recovery_propagates_stats_reset_when_present() -> None:
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_recovery"] = [
        {
            "replay_lsn": "0/1",
            "replay_lag_seconds": None,
            "last_replayed_at": None,
            "startup_state": "streaming",
            "stats_reset": "2026-06-26 12:00:00+00",
        }
    ]
    driver = FakeRoutingDriver(routes)
    rows = await read_pg_stat_recovery(driver)  # type: ignore[arg-type]
    assert rows == [
        RecoveryStatRow(
            replay_lsn="0/1",
            replay_lag_seconds=None,
            last_replayed_at=None,
            startup_state="streaming",
            stats_reset="2026-06-26 12:00:00+00",
        )
    ]


async def test_read_recovery_empty_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    rows = await read_pg_stat_recovery(driver)  # type: ignore[arg-type]
    assert rows == []


async def test_read_recovery_handles_null_lag_field() -> None:
    """Primary servers / not-yet-replaying standbys have NULL fields."""
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_recovery"] = [
        {
            "replay_lsn": None,
            "replay_lag_seconds": None,
            "last_replayed_at": None,
            "startup_state": None,
        }
    ]
    driver = FakeRoutingDriver(routes)
    rows = await read_pg_stat_recovery(driver)  # type: ignore[arg-type]
    assert rows == [
        RecoveryStatRow(
            replay_lsn=None,
            replay_lag_seconds=None,
            last_replayed_at=None,
            startup_state=None,
        )
    ]


# --- analyze_lock_hotspots -------------------------------------------------


async def test_analyze_classifies_contention_dominant() -> None:
    """High wait_time + high wait_count → contention_dominant."""
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_lock"] = [
        {"lock_type": "relation", "acquires": 100_000, "waits": 5_000, "wait_time_us": 10_000_000},
    ]
    driver = FakeRoutingDriver(routes)
    result = await analyze_lock_hotspots(driver)  # type: ignore[arg-type]
    assert isinstance(result, LockHotspotsResult)
    assert result.available is True
    assert len(result.hotspots) == 1
    assert result.hotspots[0].reason == "contention_dominant"
    assert "find_blocking_chains" in result.hotspots[0].suggested_followup


async def test_analyze_classifies_high_wait_time() -> None:
    """High wait_time, low wait_count → high_wait_time (long-running txn)."""
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_lock"] = [
        {"lock_type": "advisory", "acquires": 100, "waits": 5, "wait_time_us": 5_000_000},
    ]
    driver = FakeRoutingDriver(routes)
    result = await analyze_lock_hotspots(driver)  # type: ignore[arg-type]
    assert result.hotspots[0].reason == "high_wait_time"
    assert "long-running transaction" in result.hotspots[0].suggested_followup


async def test_analyze_classifies_high_wait_count() -> None:
    """High wait_count, low wait_time → high_wait_count (hot-row contention)."""
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_lock"] = [
        {"lock_type": "tuple", "acquires": 100_000, "waits": 5_000, "wait_time_us": 100_000},
    ]
    driver = FakeRoutingDriver(routes)
    result = await analyze_lock_hotspots(driver)  # type: ignore[arg-type]
    assert result.hotspots[0].reason == "high_wait_count"
    assert "hot-row contention" in result.hotspots[0].suggested_followup


async def test_analyze_low_contention_emits_busiest_for_context() -> None:
    """No lock type crosses the threshold → emit busiest as low_contention."""
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_lock"] = [
        {"lock_type": "tuple", "acquires": 1_000, "waits": 10, "wait_time_us": 5_000},
    ]
    driver = FakeRoutingDriver(routes)
    result = await analyze_lock_hotspots(driver)  # type: ignore[arg-type]
    assert len(result.hotspots) == 1
    assert result.hotspots[0].reason == "low_contention"
    assert result.hotspots[0].lock_type == "tuple"
    assert "healthy bounds" in result.detail or "healthy bounds" in result.hotspots[0].suggested_followup


async def test_analyze_empty_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    result = await analyze_lock_hotspots(driver)  # type: ignore[arg-type]
    assert result.available is False
    assert result.hotspots == []
    assert "find_blocking_chains" in result.detail


async def test_analyze_empty_when_view_absent_on_pg19() -> None:
    routes = _version_route(190001, "19beta1")
    driver = FakeRoutingDriver(routes)  # no view-present row
    result = await analyze_lock_hotspots(driver)  # type: ignore[arg-type]
    assert result.available is False
    assert result.hotspots == []


async def test_analyze_empty_view_returns_clean_result() -> None:
    """View present but no rows — stats reset just happened."""
    routes = _version_route(190001, "19beta1")
    routes["FROM pg_class c "] = [{"present": 1}]
    routes["FROM pg_stat_lock"] = []
    driver = FakeRoutingDriver(routes)
    result = await analyze_lock_hotspots(driver)  # type: ignore[arg-type]
    assert result.available is True
    assert result.hotspots == []
    assert "empty" in result.detail.lower() or "no lock activity" in result.detail.lower()


# --- Dataclass shapes ------------------------------------------------------


def test_dataclass_shapes() -> None:
    status = Pg19StatsStatus(
        available=True,
        server_version_num=190001,
        server_version="19beta1",
        has_pg_stat_lock=True,
        has_pg_stat_recovery=True,
        detail="ok",
    )
    assert status.available is True
    row = LockStatRow(lock_type="relation", acquires=100, waits=5, wait_time_us=1_500_000)
    assert row.lock_type == "relation"
    hot = LockHotspot(
        lock_type="relation",
        waits=5,
        wait_time_us=1_500_000,
        reason="high_wait_time",
        suggested_followup="x",
    )
    assert hot.reason == "high_wait_time"
