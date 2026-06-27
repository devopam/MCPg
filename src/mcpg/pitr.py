"""Point-in-time-recovery readiness advisor.

Realises roadmap row **5.3**. Composes the WAL-archive health probe
(5.2) with the handful of GUCs that gate PITR into a single
``check_pitr_readiness`` verdict — "could I actually recover this
cluster to an arbitrary point in time right now, and if not, what's
missing?"

PITR depends on three things being simultaneously true:

1. **Continuous archiving works** — ``archive_mode`` on and the
   archiver isn't failing (covered by ``mcpg.wal_archive``).
2. **WAL carries enough detail** — ``wal_level`` >= ``replica`` (the
   ``minimal`` level omits the records a recovery needs).
3. **A base backup is takeable** — at least one WAL sender slot
   (``max_wal_senders`` > 0) so ``pg_basebackup`` can stream, and
   ``full_page_writes`` on (torn-page safety during recovery replay).

This tool reports each gate's status, an overall ``ready`` verdict,
and a short ordered list of remediation steps for whatever's missing.
It's a **read-only advisor** — it changes nothing and emits no
secrets (the archive command is reported only as a boolean, inherited
from the 5.2 probe).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg._vendor.sql import SqlDriver
from mcpg.wal_archive import get_wal_archive_status

# wal_level ordering for the ">= replica" gate.
_WAL_LEVEL_RANK = {"minimal": 0, "replica": 1, "logical": 2}


@dataclass(frozen=True)
class PitrGate:
    """One PITR prerequisite and whether it's satisfied.

    ``name`` is a stable identifier (``archiving`` / ``wal_level`` /
    ``base_backup_capable`` / ``full_page_writes``). ``ok`` is whether
    the gate passes. ``observed`` is the relevant current value as
    text; ``remediation`` is empty when ``ok`` else the one-line fix.
    """

    name: str
    ok: bool
    observed: str
    remediation: str


@dataclass(frozen=True)
class PitrReadinessReport:
    """Roll-up of :func:`check_pitr_readiness`.

    ``available`` is ``False`` only when the probe itself fails (driver
    error). ``ready`` is ``True`` when every gate passes. ``gates`` is
    the full per-gate breakdown (always all four, in evaluation order);
    ``remediation`` is the ordered list of fixes for the failing gates
    (empty when ``ready``).
    """

    available: bool
    ready: bool
    wal_level: str
    archiving_healthy: bool
    gates: list[PitrGate] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    detail: str = ""


async def check_pitr_readiness(driver: SqlDriver) -> PitrReadinessReport:
    """Assess whether the cluster is ready for point-in-time recovery.

    Single archiver probe (via :func:`mcpg.wal_archive.get_wal_archive_status`)
    plus one GUC read. Read-only; never raises — a driver failure comes
    back as ``available=False`` with the error in ``detail``.
    """
    archive = await get_wal_archive_status(driver)
    if not archive.available:
        return PitrReadinessReport(
            available=False,
            ready=False,
            wal_level="unknown",
            archiving_healthy=False,
            gates=[],
            remediation=[],
            detail=f"PITR readiness unknown — archiver probe failed: {archive.detail}",
        )

    try:
        rows = await driver.execute_query(
            "SELECT "
            "  current_setting('wal_level') AS wal_level, "
            "  current_setting('max_wal_senders')::int AS max_wal_senders, "
            "  current_setting('full_page_writes') AS full_page_writes",
            force_readonly=True,
        )
    except Exception as exc:
        return PitrReadinessReport(
            available=False,
            ready=False,
            wal_level="unknown",
            archiving_healthy=archive.healthy,
            gates=[],
            remediation=[],
            detail=f"PITR readiness unknown — could not read GUCs: {exc}",
        )

    c = (rows[0].cells if rows else {}) or {}
    wal_level = str(c.get("wal_level") or "unknown")
    max_wal_senders = int(c.get("max_wal_senders") or 0)
    full_page_writes = str(c.get("full_page_writes") or "off")

    gates: list[PitrGate] = []

    # 1. Continuous archiving.
    if archive.archiving_enabled and archive.healthy:
        gates.append(PitrGate("archiving", True, f"archive_mode={archive.archive_mode}, healthy", ""))
    elif not archive.archiving_enabled:
        gates.append(
            PitrGate(
                "archiving",
                False,
                f"archive_mode={archive.archive_mode}",
                "Enable continuous archiving: set archive_mode = on and a working "
                "archive_command / archive_library, then restart.",
            )
        )
    else:
        gates.append(
            PitrGate(
                "archiving",
                False,
                "archive_mode on but archiver is failing",
                "Fix the failing archive_command / archive_library "
                "(see get_wal_archive_status) before relying on PITR.",
            )
        )

    # 2. wal_level >= replica.
    wal_ok = _WAL_LEVEL_RANK.get(wal_level, -1) >= _WAL_LEVEL_RANK["replica"]
    gates.append(
        PitrGate(
            "wal_level",
            wal_ok,
            wal_level,
            ""
            if wal_ok
            else "Set wal_level = replica (or logical) and restart — minimal omits records PITR replay needs.",
        )
    )

    # 3. Base-backup capable (at least one WAL sender).
    senders_ok = max_wal_senders > 0
    gates.append(
        PitrGate(
            "base_backup_capable",
            senders_ok,
            f"max_wal_senders={max_wal_senders}",
            "" if senders_ok else "Set max_wal_senders >= 1 (and restart) so pg_basebackup can stream a base backup.",
        )
    )

    # 4. full_page_writes (torn-page safety during replay).
    fpw_ok = full_page_writes == "on"
    gates.append(
        PitrGate(
            "full_page_writes",
            fpw_ok,
            full_page_writes,
            ""
            if fpw_ok
            else "Set full_page_writes = on — recovery replay can hit torn "
            "pages otherwise (unless the storage guarantees atomic 8kB writes).",
        )
    )

    remediation = [g.remediation for g in gates if not g.ok]
    ready = not remediation
    if ready:
        detail = "Cluster is PITR-ready: archiving healthy, wal_level sufficient, base backups takeable."
    else:
        detail = f"Cluster is NOT PITR-ready — {len(remediation)} prerequisite(s) unmet. See remediation."

    return PitrReadinessReport(
        available=True,
        ready=ready,
        wal_level=wal_level,
        archiving_healthy=archive.healthy,
        gates=gates,
        remediation=remediation,
        detail=detail,
    )


__all__ = ["PitrGate", "PitrReadinessReport", "check_pitr_readiness"]
