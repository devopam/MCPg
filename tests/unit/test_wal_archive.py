"""Tests for the WAL archive status reader (roadmap 5.2)."""

from __future__ import annotations

from _fakes import FakeDriver, FakeRoutingDriver

from mcpg.wal_archive import WalArchiveStatus, get_wal_archive_status


def _archiver_row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "archive_mode": "on",
        "archive_command_set": True,
        "archived_count": 100,
        "last_archived_wal": "000000010000000000000064",
        "last_archived_time": "2026-06-27 08:00:00+00",
        "failed_count": 0,
        "last_failed_wal": None,
        "last_failed_time": None,
        "stats_reset": "2026-06-20 00:00:00+00",
    }
    base.update(overrides)
    return base


def _driver(row: dict[str, object]) -> FakeRoutingDriver:
    return FakeRoutingDriver({"FROM pg_stat_archiver": [row]})


async def test_healthy_when_archiving_on_no_failures() -> None:
    result = await get_wal_archive_status(_driver(_archiver_row()))  # type: ignore[arg-type]
    assert isinstance(result, WalArchiveStatus)
    assert result.available is True
    assert result.archiving_enabled is True
    assert result.healthy is True
    assert result.archived_count == 100
    assert "healthy" in result.detail.lower()


async def test_archive_command_never_echoed_only_boolean() -> None:
    result = await get_wal_archive_status(_driver(_archiver_row()))  # type: ignore[arg-type]
    assert result.archive_command_set is True
    # No field carries the raw command string.
    assert "archive_command" not in {f for f in vars(result) if "command" in f and f != "archive_command_set"}


async def test_unhealthy_when_latest_attempt_failed() -> None:
    # last_failed_time newer than last_archived_time → FAILING.
    row = _archiver_row(
        failed_count=3,
        last_failed_wal="000000010000000000000065",
        last_failed_time="2026-06-27 08:05:00+00",
        last_archived_time="2026-06-27 08:00:00+00",
    )
    result = await get_wal_archive_status(_driver(row))  # type: ignore[arg-type]
    assert result.healthy is False
    assert "FAILING" in result.detail
    assert result.failed_count == 3


async def test_recovered_when_failures_historical_but_latest_succeeded() -> None:
    # failures exist, but last_archived_time is newer than last_failed_time.
    row = _archiver_row(
        failed_count=2,
        last_failed_wal="000000010000000000000050",
        last_failed_time="2026-06-27 07:00:00+00",
        last_archived_time="2026-06-27 08:00:00+00",
    )
    result = await get_wal_archive_status(_driver(row))  # type: ignore[arg-type]
    assert result.healthy is True
    assert "recovered" in result.detail.lower()


async def test_disabled_archiving_is_healthy_nothing_to_monitor() -> None:
    row = _archiver_row(archive_mode="off", archive_command_set=False, archived_count=0)
    result = await get_wal_archive_status(_driver(row))  # type: ignore[arg-type]
    assert result.archiving_enabled is False
    assert result.healthy is True
    assert "disabled" in result.detail.lower()


async def test_archive_mode_always_counts_as_enabled() -> None:
    result = await get_wal_archive_status(_driver(_archiver_row(archive_mode="always")))  # type: ignore[arg-type]
    assert result.archiving_enabled is True


async def test_failures_with_no_archives_ever_is_unhealthy() -> None:
    row = _archiver_row(
        archived_count=0,
        last_archived_wal=None,
        last_archived_time=None,
        failed_count=5,
        last_failed_wal="000000010000000000000001",
        last_failed_time="2026-06-27 08:05:00+00",
    )
    result = await get_wal_archive_status(_driver(row))  # type: ignore[arg-type]
    assert result.healthy is False


async def test_driver_failure_surfaces_as_unavailable() -> None:
    result = await get_wal_archive_status(FakeDriver(fail=True))  # type: ignore[arg-type]
    assert result.available is False
    assert "Could not read pg_stat_archiver" in result.detail


async def test_empty_result_is_unavailable() -> None:
    result = await get_wal_archive_status(FakeRoutingDriver({"FROM pg_stat_archiver": []}))  # type: ignore[arg-type]
    assert result.available is False
    assert "no rows" in result.detail
