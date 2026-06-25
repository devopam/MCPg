"""WarehousePG (Greenplum-derived MPP) integration.

`WarehousePG <https://github.com/WarehousePG/warehousepg>`_ is the
community-maintained fork of Greenplum that picked up the mantle
after Broadcom's licensing change. It's an MPP analytical database
built on top of PostgreSQL — distributed coordinator + segments,
append-optimized + column-oriented tables, ``DISTRIBUTED BY`` clause,
resource groups, ``gp_*`` catalog surface.

Since WarehousePG is wire-compatible with libpq and the core SQL
surface is PostgreSQL, most MCPg tools already work. This module
provides the **gating status probe** for the MPP-specific surface:
``get_warehousepg_status``. Every subsequent ``mcpg.warehousepg.*``
tool advertises its readiness via this probe, mirroring the pattern
established by ``mcpg.pgq.get_pgq_status`` and ``mcpg.pg19_runtime
.get_logical_replication_status``.

Detection strategy
==================

WarehousePG identifies itself in two complementary ways:

1. **Version string** — ``SELECT version()`` returns something like
   ``PostgreSQL 14.4 (WarehousePG 7.2.0 build commit:...)``. The
   ``WarehousePG`` / ``Greenplum`` substring is the canonical
   signal; we honour both for forward compatibility with any
   downstream rebrand.

2. **Catalog presence** — the MPP coordinator exposes
   ``gp_segment_configuration``, a view describing each primary +
   mirror segment. Vanilla PostgreSQL doesn't have this. Probing it
   via ``to_regclass()`` is cheaper than running an actual SELECT
   and works even when the audit role lacks segment-level grants.

Both signals must agree before we report ``available=True``: an
operator who's set up a vanilla-PG cluster with a hand-rolled
``gp_segment_configuration`` view shouldn't be misclassified as MPP.

Returned payload
================

When ``available=True``, the probe additionally surfaces:

- ``coordinator_role`` — the coordinator's role label
  (``coordinator`` on modern WarehousePG; ``master`` on legacy
  Greenplum versions). Lets agents differentiate vintage without
  forcing them to parse the version string.
- ``segment_count`` — total primary segments (excludes mirrors and
  the coordinator). The single most-asked-for number for capacity
  planning.
- ``mirroring`` — ``True`` when at least one mirror segment exists,
  ``False`` on an unmirrored cluster (common in dev / test).

Security
========

Pure read-only catalog joins. No caller-supplied identifiers in
identifier slots. Driver errors surface as ``available=False`` with
the actual error message in ``detail`` — same convention as
``mcpg.health`` and the other status probes.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver


@dataclass(frozen=True)
class WarehousePGStatus:
    """Reports whether the connected server is a WarehousePG cluster.

    ``available`` is the gating bit for every other ``mcpg.warehousepg.*``
    tool — when ``False``, those tools advertise themselves as inert
    rather than raising on a vanilla PG cluster.

    ``version`` carries the full ``SELECT version()`` string verbatim
    so agents can pin behaviour to specific WarehousePG releases
    without re-querying. ``coordinator_role`` / ``segment_count`` /
    ``mirroring`` populate only when ``available`` is ``True``.
    """

    available: bool
    version: str
    coordinator_role: str | None
    segment_count: int | None
    mirroring: bool | None
    detail: str


# Substring matches for the MPP variant. Both forms surface in the
# wild — WarehousePG keeps the Greenplum compatibility identifier in
# many versions for tooling compatibility.
_MPP_VERSION_MARKERS: tuple[str, ...] = ("warehousepg", "greenplum")


async def get_warehousepg_status(driver: SqlDriver) -> WarehousePGStatus:
    """Probe the connected server for WarehousePG / MPP signature.

    Read-only; never raises. Returns ``available=False`` on every
    error path so callers can branch on the bit without try/except
    boilerplate. Carries a diagnostic in ``detail`` for the cases an
    agent should surface to the user.
    """
    version = ""
    try:
        rows = await driver.execute_query(
            "SELECT version() AS version",
            force_readonly=True,
        )
        if rows:
            version = str(rows[0].cells.get("version") or "")
    except Exception as exc:
        return WarehousePGStatus(
            available=False,
            version="",
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=f"WarehousePG status probe failed (version): {exc}",
        )

    if not any(marker in version.lower() for marker in _MPP_VERSION_MARKERS):
        return WarehousePGStatus(
            available=False,
            version=version,
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=(
                "Not a WarehousePG / Greenplum cluster — `SELECT version()` "
                "doesn't mention either. The mcpg.warehousepg tools are inert "
                "on this server; the core MCPg tools work as usual."
            ),
        )

    # Second confirmation — catalog presence. An operator could in
    # principle put 'WarehousePG' in their version string without
    # being on the real product; the gp_segment_configuration view
    # is the canonical MPP signal.
    try:
        catalog_rows = await driver.execute_query(
            "SELECT to_regclass('pg_catalog.gp_segment_configuration')::text AS oid",
            force_readonly=True,
        )
    except Exception as exc:
        return WarehousePGStatus(
            available=False,
            version=version,
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=(f"WarehousePG status probe failed (catalog probe): {exc}"),
        )
    has_seg_view = bool(catalog_rows and catalog_rows[0].cells.get("oid"))
    if not has_seg_view:
        return WarehousePGStatus(
            available=False,
            version=version,
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=(
                "Version string mentions WarehousePG / Greenplum but "
                "`gp_segment_configuration` view is missing — this looks "
                "like a vanilla PostgreSQL cluster with an unusual version "
                "banner. mcpg.warehousepg tools stay inert."
            ),
        )

    # Both signals positive — we're on a real MPP. Surface coordinator
    # role + segment counts. role='c' is the modern coordinator label;
    # role='p' / 'm' are primary / mirror segments.
    coordinator_role: str | None = None
    segment_count: int | None = None
    mirroring: bool | None = None
    try:
        seg_rows = await driver.execute_query(
            "SELECT "
            "  (SELECT role FROM gp_segment_configuration "
            "    WHERE content = -1 LIMIT 1) AS coordinator_role, "
            "  (SELECT count(*)::int FROM gp_segment_configuration "
            "    WHERE role = 'p' AND content >= 0) AS primary_count, "
            "  (SELECT count(*)::int FROM gp_segment_configuration "
            "    WHERE role = 'm' AND content >= 0) AS mirror_count",
            force_readonly=True,
        )
        if seg_rows:
            cells = seg_rows[0].cells
            raw_role = cells.get("coordinator_role")
            # Legacy Greenplum labels coordinator as 'master' ('m' on
            # primaries vs the literal 'master' string on the
            # coordinator — they don't collide); modern WarehousePG
            # uses 'coordinator'. Normalise to the human-readable form.
            if raw_role == "p":
                # On some versions the coordinator's role column also
                # carries 'p' (primary); content=-1 still distinguishes
                # it. Translate to the human form.
                coordinator_role = "coordinator"
            elif raw_role is not None:
                coordinator_role = str(raw_role)
            primary_count = cells.get("primary_count")
            mirror_count = cells.get("mirror_count")
            if primary_count is not None:
                segment_count = int(primary_count)
            if mirror_count is not None:
                mirroring = int(mirror_count) > 0
    except Exception as exc:
        # Segment metadata probe failed but we already confirmed MPP —
        # report available=True with the metadata fields None plus
        # the diagnostic. Avoids the awkward "version says WarehousePG,
        # but the gating bit flips to False because we couldn't count
        # segments" trap.
        return WarehousePGStatus(
            available=True,
            version=version,
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=(
                f"WarehousePG detected, but segment metadata probe failed "
                f"({exc}). Check that the audit role has SELECT on "
                "`gp_segment_configuration` for full status reporting."
            ),
        )

    return WarehousePGStatus(
        available=True,
        version=version,
        coordinator_role=coordinator_role,
        segment_count=segment_count,
        mirroring=mirroring,
        detail=(
            f"WarehousePG cluster detected: {segment_count} primary "
            f"segment(s), mirroring "
            f"{'enabled' if mirroring else 'disabled'}. "
            "`mcpg.warehousepg.*` tools are active on this server."
        ),
    )


__all__ = [
    "WarehousePGStatus",
    "get_warehousepg_status",
]
