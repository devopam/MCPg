"""Tests for `mcpg.warehousepg.get_warehousepg_status`."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg.warehousepg import WarehousePGStatus, get_warehousepg_status

# Sample version strings the probe should accept (modern WarehousePG +
# legacy Greenplum compatibility) and reject (vanilla PG, edge cases).
_WAREHOUSEPG_VERSION = (
    "PostgreSQL 14.4 (WarehousePG 7.2.0 build commit:abc) on x86_64-pc-linux-gnu, compiled by gcc (GCC) 11.3.0, 64-bit"
)
_GREENPLUM_VERSION = "PostgreSQL 12.12 (Greenplum Database 7.0.0 build commit:def) on x86_64"
_VANILLA_PG_VERSION = "PostgreSQL 16.4 on x86_64-pc-linux-gnu"


def _routes(
    *,
    version: str = _VANILLA_PG_VERSION,
    has_seg_view: bool = False,
    coordinator_role: str | None = "coordinator",
    primary_count: int = 0,
    mirror_count: int = 0,
) -> dict[str, list[dict[str, Any]]]:
    """Build query routes for the three probes:
    1. SELECT version() — version-banner check
    2. to_regclass('pg_catalog.gp_segment_configuration') — catalog presence
    3. gp_segment_configuration — segment metadata roll-up
    """
    return {
        "SELECT version()": [{"version": version}],
        "to_regclass": [{"oid": "pg_catalog.gp_segment_configuration" if has_seg_view else None}],
        "FROM gp_segment_configuration": [
            {
                "coordinator_role": coordinator_role,
                "primary_count": primary_count,
                "mirror_count": mirror_count,
            }
        ],
    }


async def test_returns_available_false_on_vanilla_postgres() -> None:
    """Vanilla PG: version doesn't mention either marker → inert tools."""
    driver = FakeRoutingDriver(_routes(version=_VANILLA_PG_VERSION))
    status = await get_warehousepg_status(driver)
    assert isinstance(status, WarehousePGStatus)
    assert status.available is False
    assert status.version == _VANILLA_PG_VERSION
    assert status.coordinator_role is None
    assert status.segment_count is None
    assert status.mirroring is None
    assert "Not a WarehousePG" in status.detail


async def test_recognises_warehousepg_modern_version_string() -> None:
    driver = FakeRoutingDriver(
        _routes(
            version=_WAREHOUSEPG_VERSION,
            has_seg_view=True,
            coordinator_role="coordinator",
            primary_count=8,
            mirror_count=8,
        )
    )
    status = await get_warehousepg_status(driver)
    assert status.available is True
    assert "WarehousePG" in status.version
    assert status.coordinator_role == "coordinator"
    assert status.segment_count == 8
    assert status.mirroring is True


async def test_recognises_legacy_greenplum_version_string() -> None:
    """The detector honours both 'WarehousePG' and 'Greenplum' substrings
    so a legacy cluster on the original Greenplum brand still surfaces
    as available=True (the catalog signal is the same)."""
    driver = FakeRoutingDriver(
        _routes(
            version=_GREENPLUM_VERSION,
            has_seg_view=True,
            coordinator_role="master",
            primary_count=4,
            mirror_count=0,
        )
    )
    status = await get_warehousepg_status(driver)
    assert status.available is True
    assert status.coordinator_role == "master"
    assert status.segment_count == 4
    assert status.mirroring is False


async def test_rejects_when_version_says_mpp_but_catalog_view_is_missing() -> None:
    """A vanilla-PG cluster with an unusual version banner shouldn't be
    misclassified — both signals must agree."""
    driver = FakeRoutingDriver(_routes(version=_WAREHOUSEPG_VERSION, has_seg_view=False))
    status = await get_warehousepg_status(driver)
    assert status.available is False
    assert "gp_segment_configuration" in status.detail


async def test_unmirrored_cluster_reports_mirroring_false() -> None:
    """Dev / test clusters frequently run without mirrors. The probe
    must distinguish unmirrored (mirror_count = 0) from "mirroring
    unknown" (segment metadata probe failed)."""
    driver = FakeRoutingDriver(
        _routes(version=_WAREHOUSEPG_VERSION, has_seg_view=True, primary_count=4, mirror_count=0)
    )
    status = await get_warehousepg_status(driver)
    assert status.available is True
    assert status.mirroring is False
    assert "disabled" in status.detail


async def test_version_probe_driver_failure_surfaces_as_available_false() -> None:
    """A driver failure on the first probe means we can't even tell
    what server we're talking to — flip available to False with a
    diagnostic that carries the actual error."""

    class _FailingDriver:
        async def execute_query(
            self, query: str, params: list[Any] | None = None, force_readonly: bool = False
        ) -> list[Any]:
            del query, params, force_readonly
            raise RuntimeError("connection lost")

    status = await get_warehousepg_status(_FailingDriver())  # type: ignore[arg-type]
    assert status.available is False
    assert "version" in status.detail
    assert "connection lost" in status.detail


async def test_catalog_probe_failure_surfaces_as_available_false() -> None:
    """First probe succeeds (version OK), second probe fails on the
    to_regclass call → we can't confirm MPP → available=False with
    a diagnostic explaining where the probe broke."""

    class _CatalogFailingDriver:
        def __init__(self) -> None:
            self.call_index = 0

        async def execute_query(
            self, query: str, params: list[Any] | None = None, force_readonly: bool = False
        ) -> list[Any]:
            del params, force_readonly
            self.call_index += 1
            if self.call_index == 1:
                # Version probe — succeed with a WarehousePG banner.
                from mcpg.sql import SqlDriver

                return [SqlDriver.RowResult(cells={"version": _WAREHOUSEPG_VERSION})]
            raise RuntimeError("permission denied for view gp_segment_configuration")

    status = await get_warehousepg_status(_CatalogFailingDriver())  # type: ignore[arg-type]
    assert status.available is False
    assert "catalog probe" in status.detail
    assert "permission denied" in status.detail


async def test_segment_metadata_failure_keeps_available_true_with_diagnostic() -> None:
    """Version + catalog signal positive but the segment-metadata
    rollup fails (e.g. audit role can't SELECT from
    gp_segment_configuration). We've already confirmed MPP — flip
    available=True with the diagnostic and None'd metadata so the
    rest of the warehousepg.* family activates, just without rich
    status."""

    class _MetadataFailingDriver:
        def __init__(self) -> None:
            self.call_index = 0

        async def execute_query(
            self, query: str, params: list[Any] | None = None, force_readonly: bool = False
        ) -> list[Any]:
            del params, force_readonly
            self.call_index += 1
            from mcpg.sql import SqlDriver

            if self.call_index == 1:
                return [SqlDriver.RowResult(cells={"version": _WAREHOUSEPG_VERSION})]
            if self.call_index == 2:
                return [SqlDriver.RowResult(cells={"oid": "pg_catalog.gp_segment_configuration"})]
            raise RuntimeError("permission denied for view gp_segment_configuration")

    status = await get_warehousepg_status(_MetadataFailingDriver())  # type: ignore[arg-type]
    assert status.available is True
    assert status.segment_count is None
    assert status.mirroring is None
    assert "permission denied" in status.detail


@pytest.mark.parametrize(
    ("version_substring", "expected_match"),
    [
        ("warehousepg", True),
        ("WarehousePG", True),  # case-insensitive
        ("greenplum", True),
        ("Greenplum", True),
        ("warehouseGP", False),  # close but not the marker
        ("PostgreSQL only", False),
    ],
)
async def test_version_marker_recognition_is_case_insensitive(version_substring: str, expected_match: bool) -> None:
    """The version string scan must be case-insensitive — operators
    have shipped releases with both 'WarehousePG' and 'warehousepg'
    spellings across the years."""
    routes = _routes(
        version=f"PostgreSQL 14 ({version_substring} 7 build commit:x)",
        has_seg_view=True,
        primary_count=2,
        mirror_count=0,
    )
    driver = FakeRoutingDriver(routes)
    status = await get_warehousepg_status(driver)
    assert status.available is expected_match


def test_status_dataclass_is_frozen() -> None:
    """`WarehousePGStatus` is frozen so the report shape can't be
    mutated by an agent inadvertently after the probe returns it."""
    status = WarehousePGStatus(
        available=False,
        version="",
        coordinator_role=None,
        segment_count=None,
        mirroring=None,
        detail="x",
    )
    with pytest.raises(AttributeError):
        status.available = True  # type: ignore[misc]
