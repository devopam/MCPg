"""Tests for the PG 19 runtime-toggles module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver

from mcpg.pg19_runtime import (
    DataChecksumsStatus,
    EnableLogicalReplicationOnDemandResult,
    LogicalReplicationStatus,
    Pg19RuntimeError,
    ToggleDataChecksumsResult,
    disable_data_checksums,
    enable_data_checksums,
    enable_logical_replication_on_demand,
    get_data_checksums_status,
    get_logical_replication_status,
)


def _version_route(num: int, ver: str) -> dict[str, list[dict[str, object]]]:
    """Helper — wires the server-version probe to a specific version."""
    return {"current_setting('server_version_num')": [{"ver_num": num, "ver": ver}]}


# --- get_data_checksums_status --------------------------------------------


async def test_checksums_status_available_on_pg19_enabled() -> None:
    routes = _version_route(190001, "19beta1")
    routes["current_setting(%s, true) AS val"] = [{"val": "on"}]
    driver = FakeRoutingDriver(routes)
    status = await get_data_checksums_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, DataChecksumsStatus)
    assert status.available is True
    assert status.enabled is True
    assert "enabled" in status.detail


async def test_checksums_status_available_on_pg19_disabled() -> None:
    routes = _version_route(190001, "19beta1")
    routes["current_setting(%s, true) AS val"] = [{"val": "off"}]
    driver = FakeRoutingDriver(routes)
    status = await get_data_checksums_status(driver)  # type: ignore[arg-type]
    assert status.available is True
    assert status.enabled is False
    assert "disabled" in status.detail


async def test_checksums_status_unavailable_on_pg18_still_reports_state() -> None:
    """PG ≤ 18 reports available=False but still surfaces the GUC value."""
    routes = _version_route(180003, "18.3")
    routes["current_setting(%s, true) AS val"] = [{"val": "on"}]
    driver = FakeRoutingDriver(routes)
    status = await get_data_checksums_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.enabled is True  # set at initdb time, still readable
    assert "pg_checksums" in status.detail  # fallback hint


async def test_checksums_status_never_raises_on_driver_failure() -> None:
    driver = FakeDriver(fail=True)
    status = await get_data_checksums_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.enabled is None
    assert "version probe failed" in status.detail


# --- enable_data_checksums / disable_data_checksums -----------------------


def _pg19_database_with_checksums(enabled: bool) -> FakeDatabase:
    """Wire a FakeDatabase that reports PG 19 + a given checksum state."""
    driver = FakeDriver()
    # Two consecutive read queries: version + setting. The fake returns
    # the same rows for every call — we set rows to satisfy both probes.
    # Tests for the toggle inspect db.unmanaged for the executed SQL.
    driver._rows = [{"ver_num": 190001, "ver": "19beta1", "val": "on" if enabled else "off"}]  # type: ignore[attr-defined]
    return FakeDatabase(driver)


async def test_enable_checksums_emits_pg_enable_function_when_off() -> None:
    db = _pg19_database_with_checksums(enabled=False)
    result = await enable_data_checksums(db)  # type: ignore[arg-type]
    assert isinstance(result, ToggleDataChecksumsResult)
    assert result.was_enabled is False
    assert result.now_enabled is True
    assert result.changed is True
    assert "pg_enable_data_checksums" in result.toggle_sql
    assert db.unmanaged == ["SELECT pg_enable_data_checksums()"]


async def test_enable_checksums_is_noop_when_already_on() -> None:
    db = _pg19_database_with_checksums(enabled=True)
    result = await enable_data_checksums(db)  # type: ignore[arg-type]
    assert result.changed is False
    assert result.was_enabled is True
    assert result.now_enabled is True
    # No DDL dispatched.
    assert db.unmanaged == []


async def test_disable_checksums_emits_pg_disable_function_when_on() -> None:
    db = _pg19_database_with_checksums(enabled=True)
    result = await disable_data_checksums(db)  # type: ignore[arg-type]
    assert result.changed is True
    assert result.was_enabled is True
    assert result.now_enabled is False
    assert "pg_disable_data_checksums" in result.toggle_sql
    assert db.unmanaged == ["SELECT pg_disable_data_checksums()"]


async def test_enable_checksums_raises_on_pg18_with_pg_checksums_hint() -> None:
    driver = FakeDriver()
    driver._rows = [{"ver_num": 180003, "ver": "18.3"}]  # type: ignore[attr-defined]
    db = FakeDatabase(driver)
    with pytest.raises(Pg19RuntimeError, match="pg_checksums"):
        await enable_data_checksums(db)  # type: ignore[arg-type]
    assert db.unmanaged == []


async def test_enable_checksums_wraps_unmanaged_failure() -> None:
    """run_unmanaged exceptions surface as Pg19RuntimeError preserving __cause__."""
    db = _pg19_database_with_checksums(enabled=False)
    db.unmanaged_fail = True
    with pytest.raises(Pg19RuntimeError, match="data_checksums toggle failed"):
        await enable_data_checksums(db)  # type: ignore[arg-type]


# --- get_logical_replication_status ---------------------------------------


async def test_logical_status_available_on_pg19_at_logical() -> None:
    routes = _version_route(190001, "19beta1")
    # Three probes (wal_level, effective_wal_level, max_replication_slots) all
    # hit the same fixture row in FakeRoutingDriver — fake doesn't dispatch
    # by param. We return the same row for all three; mapping in the helper
    # picks the right key.
    routes["current_setting(%s, true) AS val"] = [{"val": "logical"}]
    driver = FakeRoutingDriver(routes)
    status = await get_logical_replication_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, LogicalReplicationStatus)
    assert status.available is True
    assert status.wal_level == "logical"
    assert status.effective_wal_level == "logical"
    assert "already" in status.detail


async def test_logical_status_unavailable_on_pg18() -> None:
    routes = _version_route(180003, "18.3")
    routes["current_setting(%s, true) AS val"] = [{"val": "replica"}]
    driver = FakeRoutingDriver(routes)
    status = await get_logical_replication_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.wal_level == "replica"
    assert "restart" in status.detail.lower()


async def test_logical_status_never_raises_on_driver_failure() -> None:
    driver = FakeDriver(fail=True)
    status = await get_logical_replication_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.wal_level is None
    assert "version probe failed" in status.detail


# --- enable_logical_replication_on_demand ---------------------------------


def _pg19_database_with_wal_level(wal_level: str) -> FakeDatabase:
    """Wire a FakeDatabase that reports PG 19 + a given wal_level."""
    driver = FakeDriver()
    driver._rows = [{"ver_num": 190001, "ver": "19beta1", "val": wal_level}]  # type: ignore[attr-defined]
    return FakeDatabase(driver)


async def test_enable_logical_emits_alter_system_and_reload() -> None:
    db = _pg19_database_with_wal_level("replica")
    result = await enable_logical_replication_on_demand(db)  # type: ignore[arg-type]
    assert isinstance(result, EnableLogicalReplicationOnDemandResult)
    assert result.previous_wal_level == "replica"
    assert result.new_wal_level == "logical"
    assert result.requires_restart is False
    # Exact SQL shape: ALTER SYSTEM SET then pg_reload_conf.
    assert db.unmanaged == [
        "ALTER SYSTEM SET wal_level = 'logical'",
        "SELECT pg_reload_conf()",
    ]


async def test_enable_logical_is_noop_when_already_logical() -> None:
    db = _pg19_database_with_wal_level("logical")
    result = await enable_logical_replication_on_demand(db)  # type: ignore[arg-type]
    assert result.previous_wal_level == "logical"
    assert result.new_wal_level == "logical"
    assert result.requires_restart is False
    assert "no-op" in result.detail
    assert db.unmanaged == []


async def test_enable_logical_raises_on_pg18_with_restart_hint() -> None:
    driver = FakeDriver()
    driver._rows = [{"ver_num": 180003, "ver": "18.3"}]  # type: ignore[attr-defined]
    db = FakeDatabase(driver)
    with pytest.raises(Pg19RuntimeError, match="restart"):
        await enable_logical_replication_on_demand(db)  # type: ignore[arg-type]
    assert db.unmanaged == []


async def test_enable_logical_wraps_unmanaged_failure() -> None:
    db = _pg19_database_with_wal_level("replica")
    db.unmanaged_fail = True
    with pytest.raises(Pg19RuntimeError, match="wal_level on-demand toggle failed"):
        await enable_logical_replication_on_demand(db)  # type: ignore[arg-type]


# --- Dataclass shapes -----------------------------------------------------


def test_dataclass_shapes() -> None:
    cs = DataChecksumsStatus(
        available=True,
        server_version_num=190001,
        server_version="19beta1",
        enabled=True,
        detail="ok",
    )
    assert cs.enabled is True
    toggle = ToggleDataChecksumsResult(
        was_enabled=False,
        now_enabled=True,
        changed=True,
        toggle_sql="SELECT pg_enable_data_checksums()",
    )
    assert toggle.changed is True
    lr = LogicalReplicationStatus(
        available=True,
        server_version_num=190001,
        server_version="19beta1",
        wal_level="logical",
        effective_wal_level="logical",
        max_replication_slots=10,
        detail="ok",
    )
    assert lr.wal_level == "logical"
    en = EnableLogicalReplicationOnDemandResult(
        previous_wal_level="replica",
        new_wal_level="logical",
        requires_restart=False,
        detail="ok",
    )
    assert en.requires_restart is False
