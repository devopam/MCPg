"""Tests for the PITR-readiness advisor (roadmap 5.3)."""

from __future__ import annotations

from _fakes import FakeDriver, FakeRoutingDriver

from mcpg.pitr import PitrReadinessReport, check_pitr_readiness


def _routes(
    *,
    archive_mode: str = "on",
    archived_count: int = 100,
    failed_count: int = 0,
    last_archived_time: str | None = "2026-06-27 08:00:00+00",
    last_failed_time: str | None = None,
    wal_level: str = "replica",
    max_wal_senders: int = 10,
    full_page_writes: str = "on",
) -> dict[str, list[dict[str, object]]]:
    return {
        "FROM pg_stat_archiver": [
            {
                "archive_mode": archive_mode,
                "archive_command_set": archive_mode in {"on", "always"},
                "archived_count": archived_count,
                "last_archived_wal": "0001",
                "last_archived_time": last_archived_time,
                "failed_count": failed_count,
                "last_failed_wal": None if failed_count == 0 else "0002",
                "last_failed_time": last_failed_time,
                "stats_reset": "2026-06-20 00:00:00+00",
            }
        ],
        "current_setting('wal_level')": [
            {
                "wal_level": wal_level,
                "max_wal_senders": max_wal_senders,
                "full_page_writes": full_page_writes,
            }
        ],
    }


async def test_fully_ready_cluster() -> None:
    result = await check_pitr_readiness(FakeRoutingDriver(_routes()))  # type: ignore[arg-type]
    assert isinstance(result, PitrReadinessReport)
    assert result.available is True
    assert result.ready is True
    assert result.remediation == []
    assert all(g.ok for g in result.gates)
    assert "PITR-ready" in result.detail


async def test_archiving_disabled_blocks_readiness() -> None:
    result = await check_pitr_readiness(FakeRoutingDriver(_routes(archive_mode="off")))  # type: ignore[arg-type]
    assert result.ready is False
    arch = next(g for g in result.gates if g.name == "archiving")
    assert arch.ok is False
    assert any("archive_mode = on" in r for r in result.remediation)


async def test_failing_archiver_blocks_readiness() -> None:
    result = await check_pitr_readiness(
        FakeRoutingDriver(  # type: ignore[arg-type]
            _routes(failed_count=3, last_failed_time="2026-06-27 08:05:00+00")
        )
    )
    assert result.ready is False
    assert result.archiving_healthy is False
    arch = next(g for g in result.gates if g.name == "archiving")
    assert "failing" in arch.observed.lower()


async def test_minimal_wal_level_blocks_readiness() -> None:
    result = await check_pitr_readiness(FakeRoutingDriver(_routes(wal_level="minimal")))  # type: ignore[arg-type]
    assert result.ready is False
    g = next(g for g in result.gates if g.name == "wal_level")
    assert g.ok is False
    assert any("wal_level = replica" in r for r in result.remediation)


async def test_logical_wal_level_is_sufficient() -> None:
    result = await check_pitr_readiness(FakeRoutingDriver(_routes(wal_level="logical")))  # type: ignore[arg-type]
    g = next(g for g in result.gates if g.name == "wal_level")
    assert g.ok is True


async def test_no_wal_senders_blocks_base_backup_gate() -> None:
    result = await check_pitr_readiness(FakeRoutingDriver(_routes(max_wal_senders=0)))  # type: ignore[arg-type]
    assert result.ready is False
    g = next(g for g in result.gates if g.name == "base_backup_capable")
    assert g.ok is False
    assert any("max_wal_senders" in r for r in result.remediation)


async def test_full_page_writes_off_blocks_readiness() -> None:
    result = await check_pitr_readiness(FakeRoutingDriver(_routes(full_page_writes="off")))  # type: ignore[arg-type]
    assert result.ready is False
    g = next(g for g in result.gates if g.name == "full_page_writes")
    assert g.ok is False


async def test_multiple_gaps_all_surface_in_remediation() -> None:
    result = await check_pitr_readiness(
        FakeRoutingDriver(_routes(archive_mode="off", wal_level="minimal", max_wal_senders=0))  # type: ignore[arg-type]
    )
    assert result.ready is False
    assert len(result.remediation) == 3  # archiving + wal_level + base_backup
    # gates list always carries all four in order.
    assert [g.name for g in result.gates] == ["archiving", "wal_level", "base_backup_capable", "full_page_writes"]


async def test_archiver_probe_failure_is_unavailable() -> None:
    result = await check_pitr_readiness(FakeDriver(fail=True))  # type: ignore[arg-type]
    assert result.available is False
    assert "archiver probe failed" in result.detail
