"""Tests for the PG 19 async-I/O coverage module."""

from __future__ import annotations

from _fakes import FakeDriver, FakeRoutingDriver

from mcpg.aio import (
    AioStatus,
    IoMethodRecommendation,
    RecommendIoMethodResult,
    get_aio_status,
    recommend_io_method,
)


def _version_route(num: int, ver: str) -> dict[str, list[dict[str, object]]]:
    """Helper — wires the server-version probe to a specific version."""
    return {"current_setting('server_version_num')": [{"ver_num": num, "ver": ver}]}


def _settings_route(method: str | None, min_w: int | None, max_w: int | None) -> dict[str, list[dict[str, object]]]:
    """Helper — wires the io_method / io_min_workers / io_max_workers probe."""
    return {
        "current_setting('io_method'": [
            {
                "method": method,
                "min_w": str(min_w) if min_w is not None else None,
                "max_w": str(max_w) if max_w is not None else None,
            }
        ]
    }


def _pressure_route(reads: int, hits: int, window_seconds: float) -> dict[str, list[dict[str, object]]]:
    """Helper — wires the pg_stat_database aggregate probe."""
    return {"FROM pg_stat_database": [{"reads": reads, "hits": hits, "window_seconds": window_seconds}]}


# --- get_aio_status --------------------------------------------------------


async def test_status_available_on_pg19_with_settings() -> None:
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route("worker", 4, 16))
    driver = FakeRoutingDriver(routes)
    status = await get_aio_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, AioStatus)
    assert status.available is True
    assert status.io_method == "worker"
    assert status.io_min_workers == 4
    assert status.io_max_workers == 16
    assert "worker" in status.detail


async def test_status_unavailable_on_pg18_with_diagnostic() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    status = await get_aio_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.server_version_num == 180003
    # Diagnostic must point the agent at the existing IO stat tools.
    assert "read_pg_stat_io" in status.detail
    # All AIO-specific fields nulled when unavailable.
    assert status.io_method is None
    assert status.io_min_workers is None


async def test_status_never_raises_on_driver_failure() -> None:
    """get_aio_status documents 'never raises' — confirm with a failing driver."""
    driver = FakeDriver(fail=True)
    status = await get_aio_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "read_pg_stat_io" in status.detail


async def test_status_handles_unknown_io_method_value() -> None:
    """An io_method value the module doesn't recognise normalises to None."""
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route("frobnicate", 4, 16))
    driver = FakeRoutingDriver(routes)
    status = await get_aio_status(driver)  # type: ignore[arg-type]
    assert status.available is True
    assert status.io_method is None  # unrecognised value gets nulled


async def test_status_handles_missing_settings_rows() -> None:
    """PG 19 build without AIO enabled returns NULL for the GUCs."""
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route(None, None, None))
    driver = FakeRoutingDriver(routes)
    status = await get_aio_status(driver)  # type: ignore[arg-type]
    assert status.available is True
    assert status.io_method is None


# --- recommend_io_method ---------------------------------------------------


async def test_recommend_io_uring_for_high_concurrent_read_load() -> None:
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route("worker", 4, 16))
    # 10_000 reads/s with 50% miss ratio over a 10-minute window.
    routes.update(_pressure_route(reads=6_000_000, hits=6_000_000, window_seconds=600.0))
    driver = FakeRoutingDriver(routes)
    result = await recommend_io_method(driver)  # type: ignore[arg-type]
    assert isinstance(result, RecommendIoMethodResult)
    assert result.available is True
    assert len(result.recommendations) == 1
    rec = result.recommendations[0]
    assert rec.recommended_method == "io_uring"
    assert rec.reason == "high_concurrent_read_load"
    assert rec.current_method == "worker"
    # Differs from current → ready_to_run_sql emitted.
    assert rec.ready_to_run_sql == "ALTER SYSTEM SET io_method = 'io_uring';"


async def test_recommend_worker_for_bursty_io_with_cache_pressure() -> None:
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route("sync", 4, 16))
    # Moderate read rate, 50% miss ratio → worker.
    routes.update(_pressure_route(reads=200_000, hits=200_000, window_seconds=600.0))
    driver = FakeRoutingDriver(routes)
    result = await recommend_io_method(driver)  # type: ignore[arg-type]
    rec = result.recommendations[0]
    assert rec.recommended_method == "worker"
    assert rec.reason == "bursty_io_with_cache_pressure"
    assert rec.ready_to_run_sql == "ALTER SYSTEM SET io_method = 'worker';"


async def test_recommend_sync_for_low_io_pressure() -> None:
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route("worker", 4, 16))
    # 10 reads/s — below the 50-reads/s floor.
    routes.update(_pressure_route(reads=6_000, hits=100_000, window_seconds=600.0))
    driver = FakeRoutingDriver(routes)
    result = await recommend_io_method(driver)  # type: ignore[arg-type]
    rec = result.recommendations[0]
    assert rec.recommended_method == "sync"
    assert rec.reason == "low_io_pressure"


async def test_recommend_current_setting_optimal_when_no_change_needed() -> None:
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route("worker", 4, 16))
    # I/O exists but cache miss ratio is low (< 30%) → current is fine.
    routes.update(_pressure_route(reads=60_000, hits=10_000_000, window_seconds=600.0))
    driver = FakeRoutingDriver(routes)
    result = await recommend_io_method(driver)  # type: ignore[arg-type]
    rec = result.recommendations[0]
    assert rec.reason == "current_setting_optimal"
    assert rec.recommended_method == "worker"
    # Same as current → no ready_to_run_sql.
    assert rec.ready_to_run_sql is None


async def test_recommend_insufficient_stats_when_window_too_short() -> None:
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route("worker", 4, 16))
    # 5-minute window threshold; 100s is too short.
    routes.update(_pressure_route(reads=200_000, hits=200_000, window_seconds=100.0))
    driver = FakeRoutingDriver(routes)
    result = await recommend_io_method(driver)  # type: ignore[arg-type]
    rec = result.recommendations[0]
    assert rec.reason == "insufficient_stats"
    # Stays on current method while collecting more data.
    assert rec.recommended_method == "worker"
    assert rec.ready_to_run_sql is None


async def test_recommend_empty_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    result = await recommend_io_method(driver)  # type: ignore[arg-type]
    assert result.available is False
    assert result.recommendations == []
    assert "PostgreSQL 19" in result.detail


async def test_recommend_handles_zero_total_reads_safely() -> None:
    """Div-by-zero guard on a quiet cluster (no blks read/hit at all)."""
    routes = _version_route(190001, "19beta1")
    routes.update(_settings_route("worker", 4, 16))
    routes.update(_pressure_route(reads=0, hits=0, window_seconds=600.0))
    driver = FakeRoutingDriver(routes)
    result = await recommend_io_method(driver)  # type: ignore[arg-type]
    rec = result.recommendations[0]
    # 0 reads/s → low_io_pressure → sync.
    assert rec.reason == "low_io_pressure"
    assert rec.recommended_method == "sync"
    assert rec.cache_miss_ratio == 0.0
    assert rec.reads_per_second == 0.0


# --- Dataclass shapes ------------------------------------------------------


def test_dataclass_shapes() -> None:
    status = AioStatus(
        available=True,
        server_version_num=190001,
        server_version="19beta1",
        io_method="worker",
        io_min_workers=4,
        io_max_workers=16,
        detail="ok",
    )
    assert status.available is True
    rec = IoMethodRecommendation(
        recommended_method="io_uring",
        reason="high_concurrent_read_load",
        current_method="worker",
        cache_miss_ratio=0.5,
        reads_per_second=10_000.0,
        stats_window_seconds=600.0,
        ready_to_run_sql="ALTER SYSTEM SET io_method = 'io_uring';",
    )
    assert rec.recommended_method == "io_uring"
    result = RecommendIoMethodResult(
        available=True,
        server_version_num=190001,
        detail="ok",
        recommendations=[rec],
    )
    assert len(result.recommendations) == 1
