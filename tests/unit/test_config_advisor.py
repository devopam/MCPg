"""Tests for the configuration & sizing advisors (roadmap §16)."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.config_advisor import (
    STATUS_CRITICAL,
    STATUS_WARNING,
    ConfigAdvisorError,
    SequenceAuditResult,
    SettingsAuditResult,
    audit_sequences,
    audit_settings,
    recommend_postgres_conf,
)

_MB = 1024 * 1024


# ===========================================================================
# 16.1 — audit_sequences
# ===========================================================================


def _seq_present(present: bool) -> dict[str, list[dict[str, object]]]:
    return {"to_regclass('pg_catalog.pg_sequences')": [{"present": present}]}


def _seq_rows(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    return {"FROM pg_sequences": rows}


async def test_sequences_rejects_warning_pct_out_of_range() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(ConfigAdvisorError, match="warning_pct"):
        await audit_sequences(driver, warning_pct=0)  # type: ignore[arg-type]


async def test_sequences_rejects_critical_below_warning() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(ConfigAdvisorError, match="critical_pct"):
        await audit_sequences(driver, warning_pct=90, critical_pct=80)  # type: ignore[arg-type]


async def test_sequences_unavailable_pre_pg10() -> None:
    driver = FakeRoutingDriver(_seq_present(False))
    result = await audit_sequences(driver)  # type: ignore[arg-type]
    assert isinstance(result, SequenceAuditResult)
    assert result.available is False
    assert "PostgreSQL < 10" in result.detail


async def test_sequences_flags_critical_and_warning() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_seq_present(True))
    routes.update(
        _seq_rows(
            [
                # 99% → CRITICAL
                {
                    "schemaname": "public",
                    "sequencename": "hot_id_seq",
                    "last_value": 2_120_000_000,
                    "max_value": 2_147_483_647,
                },
                # 85% → WARNING
                {
                    "schemaname": "public",
                    "sequencename": "warm_id_seq",
                    "last_value": 1_825_000_000,
                    "max_value": 2_147_483_647,
                },
                # 10% → GOOD (not listed)
                {
                    "schemaname": "public",
                    "sequencename": "cold_id_seq",
                    "last_value": 214_748_364,
                    "max_value": 2_147_483_647,
                },
            ]
        )
    )
    driver = FakeRoutingDriver(routes)
    result = await audit_sequences(driver)  # type: ignore[arg-type]
    assert result.available is True
    assert result.total_examined == 3
    # Only the two at-risk show up, CRITICAL first (sorted by used_pct desc).
    assert [s.sequence for s in result.sequences] == ["hot_id_seq", "warm_id_seq"]
    assert result.sequences[0].status == STATUS_CRITICAL
    assert result.sequences[1].status == STATUS_WARNING
    assert "ALTER SEQUENCE" in result.detail


async def test_sequences_never_advanced_not_flagged() -> None:
    """A sequence with NULL last_value is counted but never at-risk."""
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_seq_present(True))
    routes.update(
        _seq_rows(
            [{"schemaname": "public", "sequencename": "fresh_seq", "last_value": None, "max_value": 2_147_483_647}]
        )
    )
    driver = FakeRoutingDriver(routes)
    result = await audit_sequences(driver)  # type: ignore[arg-type]
    assert result.total_examined == 1
    assert result.sequences == []
    assert "within healthy bounds" in result.detail


async def test_sequences_remaining_headroom_computed() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_seq_present(True))
    routes.update(_seq_rows([{"schemaname": "s", "sequencename": "q", "last_value": 95, "max_value": 100}]))
    driver = FakeRoutingDriver(routes)
    result = await audit_sequences(driver, warning_pct=80, critical_pct=99)  # type: ignore[arg-type]
    assert result.sequences[0].remaining == 5
    assert result.sequences[0].used_pct == 95.0
    assert result.sequences[0].status == STATUS_WARNING  # 95 < 99 critical


# ===========================================================================
# 16.2 — audit_settings
# ===========================================================================


def _settings_row(**overrides: object) -> dict[str, list[dict[str, object]]]:
    """Build the single-row pg_settings result with sane defaults."""
    base: dict[str, object] = {
        "fsync": "on",
        "full_page_writes": "on",
        "autovacuum": "on",
        "synchronous_commit": "on",
        "shared_buffers": 4 * 1024 * _MB,  # 4GB
        "effective_cache_size": 12 * 1024 * _MB,  # 12GB
        "work_mem": 16 * _MB,
        "maintenance_work_mem": 512 * _MB,
        "max_connections": 100,
        "checkpoint_completion_target": 0.9,
    }
    base.update(overrides)
    return {"current_setting('fsync')": [base]}


async def test_settings_clean_config_no_findings() -> None:
    driver = FakeRoutingDriver(_settings_row())
    result = await audit_settings(driver)  # type: ignore[arg-type]
    assert isinstance(result, SettingsAuditResult)
    assert result.findings == []
    assert result.ram_aware is False
    assert "No configuration issues" in result.detail


async def test_settings_flags_fsync_off_critical() -> None:
    driver = FakeRoutingDriver(_settings_row(fsync="off"))
    result = await audit_settings(driver)  # type: ignore[arg-type]
    codes = {f.code: f for f in result.findings}
    assert "fsync_off" in codes
    assert codes["fsync_off"].status == STATUS_CRITICAL
    assert "critical" in result.detail.lower()


async def test_settings_flags_autovacuum_off_and_synchronous_commit_off() -> None:
    driver = FakeRoutingDriver(_settings_row(autovacuum="off", synchronous_commit="off"))
    result = await audit_settings(driver)  # type: ignore[arg-type]
    codes = {f.code for f in result.findings}
    assert "autovacuum_off" in codes
    assert "synchronous_commit_off" in codes


async def test_settings_flags_tiny_shared_buffers() -> None:
    driver = FakeRoutingDriver(_settings_row(shared_buffers=32 * _MB))
    result = await audit_settings(driver)  # type: ignore[arg-type]
    codes = {f.code for f in result.findings}
    assert "shared_buffers_tiny" in codes


async def test_settings_flags_maintenance_below_work_mem() -> None:
    driver = FakeRoutingDriver(_settings_row(work_mem=256 * _MB, maintenance_work_mem=64 * _MB))
    result = await audit_settings(driver)  # type: ignore[arg-type]
    codes = {f.code for f in result.findings}
    assert "maintenance_work_mem_below_work_mem" in codes


async def test_settings_flags_low_checkpoint_completion_target() -> None:
    driver = FakeRoutingDriver(_settings_row(checkpoint_completion_target=0.5))
    result = await audit_settings(driver)  # type: ignore[arg-type]
    codes = {f.code for f in result.findings}
    assert "checkpoint_completion_target_low" in codes


async def test_settings_ram_ratios_only_run_with_ram_arg() -> None:
    # shared_buffers 4GB of 8GB RAM = 50% → above 45% band → ratio finding.
    driver = FakeRoutingDriver(_settings_row(shared_buffers=4 * 1024 * _MB))
    without = await audit_settings(driver)  # type: ignore[arg-type]
    assert without.ram_aware is False
    assert not any(f.code == "shared_buffers_ratio_off" for f in without.findings)

    driver2 = FakeRoutingDriver(_settings_row(shared_buffers=4 * 1024 * _MB))
    with_ram = await audit_settings(driver2, total_ram_mb=8192)  # type: ignore[arg-type]
    assert with_ram.ram_aware is True
    assert any(f.code == "shared_buffers_ratio_off" for f in with_ram.findings)


async def test_settings_flags_low_effective_cache_size_with_ram() -> None:
    # effective_cache_size 2GB of 16GB RAM = 12.5% → below 40% → finding.
    driver = FakeRoutingDriver(_settings_row(effective_cache_size=2 * 1024 * _MB))
    result = await audit_settings(driver, total_ram_mb=16384)  # type: ignore[arg-type]
    codes = {f.code for f in result.findings}
    assert "effective_cache_size_low" in codes


async def test_settings_rejects_nonpositive_ram() -> None:
    driver = FakeRoutingDriver(_settings_row())
    with pytest.raises(ConfigAdvisorError, match="total_ram_mb"):
        await audit_settings(driver, total_ram_mb=0)  # type: ignore[arg-type]


async def test_settings_examined_list_is_populated() -> None:
    driver = FakeRoutingDriver(_settings_row())
    result = await audit_settings(driver)  # type: ignore[arg-type]
    assert "fsync" in result.examined_settings
    assert "shared_buffers" in result.examined_settings


# ===========================================================================
# 16.3 — recommend_postgres_conf
# ===========================================================================


def test_conf_rejects_tiny_ram() -> None:
    with pytest.raises(ConfigAdvisorError, match="total_ram_mb"):
        recommend_postgres_conf(total_ram_mb=128)


def test_conf_rejects_unknown_workload() -> None:
    with pytest.raises(ConfigAdvisorError, match="workload"):
        recommend_postgres_conf(total_ram_mb=16384, workload="bogus")


def test_conf_rejects_unknown_storage() -> None:
    with pytest.raises(ConfigAdvisorError, match="storage"):
        recommend_postgres_conf(total_ram_mb=16384, storage="tape")


def test_conf_shared_buffers_is_quarter_ram_for_oltp() -> None:
    rec = recommend_postgres_conf(total_ram_mb=16384, cpu_count=8, workload="oltp", storage="ssd")
    # 16GB / 4 = 4GB
    assert rec.shared_buffers == "4GB"
    # effective_cache_size = 3/4 of 16GB = 12GB
    assert rec.effective_cache_size == "12GB"


def test_conf_desktop_uses_smaller_shared_buffers() -> None:
    rec = recommend_postgres_conf(total_ram_mb=16384, workload="desktop", storage="ssd")
    # desktop: RAM/16 = 1GB
    assert rec.shared_buffers == "1GB"


def test_conf_storage_drives_random_page_cost_and_io_concurrency() -> None:
    ssd = recommend_postgres_conf(total_ram_mb=8192, storage="ssd")
    hdd = recommend_postgres_conf(total_ram_mb=8192, storage="hdd")
    san = recommend_postgres_conf(total_ram_mb=8192, storage="san")
    assert ssd.random_page_cost == 1.1
    assert ssd.effective_io_concurrency == 200
    assert hdd.random_page_cost == 4.0
    assert hdd.effective_io_concurrency == 2
    assert san.effective_io_concurrency == 300


def test_conf_default_max_connections_by_workload() -> None:
    assert recommend_postgres_conf(total_ram_mb=8192, workload="web").max_connections == 200
    assert recommend_postgres_conf(total_ram_mb=8192, workload="dw").max_connections == 40


def test_conf_max_connections_override_honoured() -> None:
    rec = recommend_postgres_conf(total_ram_mb=8192, workload="web", max_connections=50)
    assert rec.max_connections == 50


def test_conf_parallel_knobs_scale_with_cpu() -> None:
    big = recommend_postgres_conf(total_ram_mb=32768, cpu_count=16, workload="oltp")
    assert big.max_worker_processes == 16
    assert big.max_parallel_workers == 16
    assert big.max_parallel_workers_per_gather == 8
    assert big.max_parallel_maintenance_workers == 4  # capped at 4


def test_conf_low_cpu_stays_at_defaults() -> None:
    small = recommend_postgres_conf(total_ram_mb=4096, cpu_count=2, workload="web")
    assert small.max_worker_processes == 8
    assert small.max_parallel_workers_per_gather == 2


def test_conf_dw_uses_high_statistics_target() -> None:
    rec = recommend_postgres_conf(total_ram_mb=65536, cpu_count=16, workload="dw")
    assert rec.default_statistics_target == 500


def test_conf_settings_dict_mirrors_structured_fields() -> None:
    rec = recommend_postgres_conf(total_ram_mb=16384, cpu_count=8, workload="oltp", storage="ssd")
    assert rec.settings["shared_buffers"] == rec.shared_buffers
    assert rec.settings["max_connections"] == str(rec.max_connections)
    assert rec.settings["random_page_cost"] == str(rec.random_page_cost)


def test_conf_wal_buffers_capped_at_16mb() -> None:
    # Big RAM → 3% of shared_buffers would exceed 16MB; must cap.
    rec = recommend_postgres_conf(total_ram_mb=131072, cpu_count=16, workload="oltp")
    assert rec.wal_buffers == "16MB"


def test_conf_maintenance_work_mem_capped_at_2gb() -> None:
    rec = recommend_postgres_conf(total_ram_mb=131072, cpu_count=16, workload="oltp")
    # RAM/16 = 8GB, capped at 2GB.
    assert rec.maintenance_work_mem == "2GB"
