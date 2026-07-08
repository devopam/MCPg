"""PG 19 lock + recovery analytics — `pg_stat_lock` and `pg_stat_recovery`.

PG 19 introduces two new monitoring views:

* ``pg_stat_lock`` — per-lock-type wait / acquire counters that let
  operators see *which* lock types (relation / page / tuple / xid /
  virtualxid / advisory / etc.) are the source of contention.
  Previously this required parsing `pg_locks` snapshots or guessing
  from `pg_stat_activity.wait_event_type`.
* ``pg_stat_recovery`` — detailed visibility into recovery operations
  (replay LSN, replay throughput, last-applied-WAL-record timestamp,
  startup-process state). Previously this lived in
  `pg_stat_replication` + ad-hoc XLOG diagnostics.

This module ships three read tools and one advisor:

* ``get_pg19_stats_status`` — version + view-presence probe; never
  raises. Lets agents feature-detect both views in one call before
  calling the readers.
* ``read_pg_stat_lock`` — surface every row from `pg_stat_lock`.
* ``read_pg_stat_recovery`` — surface the single-row recovery summary.
* ``analyze_lock_hotspots`` — read-only advisor that buckets lock
  types by wait dominance and surfaces stable reason codes
  (``high_wait_time``, ``contention_dominant``,
  ``low_contention``).

Backward compatibility
----------------------
The existing `find_blocking_chains` / `list_locks` tools in
`mcpg.locks` are untouched. On PG ≤ 18 (where the new views don't
exist) all four tools degrade to empty results + diagnostics pointing
at the older tools, per the no-deprecation rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg.sql import SqlDriver

# Both views landed in PG 19. The version-num probe is the boundary —
# no extension to install.
_MIN_PG19_VERSION = 190000

# Heuristic thresholds for the advisor. Inline so the docstring and
# the classifier share one source of truth.

# Wait time above which a lock type is considered "hot" (microseconds).
# 1 second of cumulative wait across the cluster is the smallest unit
# worth flagging in a typical incident-response context.
_HIGH_WAIT_TIME_US = 1_000_000

# Wait count above which we surface the type even if cumulative wait
# is low — high *frequency* of short waits often means lock-contention
# from app-side hot-row updates.
_HIGH_WAIT_COUNT = 1_000


class Pg19StatsError(Exception):
    """Raised when a PG 19 stats operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses — one per return shape. frozen + slots throughout.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pg19StatsStatus:
    """Reports whether the PG 19 lock + recovery views are usable.

    ``available`` is True when ``server_version_num`` >= 190000 AND at
    least one of the new views exists. The per-view booleans let agents
    pick by exact feature without separate probes.

    ``detail`` is a human-readable guidance string suitable for surfacing
    back to an LLM when the answer is "not available" — points at the
    legacy `find_blocking_chains` / `pg_stat_replication` paths.
    """

    available: bool
    server_version_num: int
    server_version: str
    has_pg_stat_lock: bool
    has_pg_stat_recovery: bool
    detail: str


@dataclass(frozen=True)
class LockStatRow:
    """One row from ``pg_stat_lock``.

    Field names follow the PG 19 Beta 1 docs. ``lock_type`` is the
    category (e.g. ``relation`` / ``tuple`` / ``advisory``).
    ``waits`` and ``wait_time_us`` are cumulative since the stats reset.

    ``stats_reset`` is the timestamp of the last ``pg_stat_reset()`` call
    against this view (PG 19 added the column to every ``pg_stat_*``
    view that didn't already have it — surfacing it lets callers tell
    "no lock contention" from "counters were reset 5 seconds ago").
    Reported as the source ``text`` cast so callers don't need a
    timezone-aware datetime to interpret it.
    """

    lock_type: str
    acquires: int
    waits: int
    wait_time_us: int
    stats_reset: str | None = None


@dataclass(frozen=True)
class RecoveryStatRow:
    """One row from ``pg_stat_recovery`` — typically a single-row view
    that summarises the standby's replay progress.

    ``replay_lsn`` is the standby's current replay LSN (string form);
    ``replay_lag_seconds`` is the apparent lag from primary derived
    from the last-applied-record timestamp. ``startup_state`` reports
    the standby's startup-process status.

    ``stats_reset`` is the timestamp of the last ``pg_stat_reset()``
    call against this view (see :class:`LockStatRow` for the
    rationale).
    """

    replay_lsn: str | None
    replay_lag_seconds: float | None
    last_replayed_at: str | None
    startup_state: str | None
    stats_reset: str | None = None


@dataclass(frozen=True)
class LockHotspot:
    """One advisor recommendation row.

    ``reason`` is a stable identifier the agent can react to:

    * ``high_wait_time`` — cumulative wait_time_us >= 1 second.
    * ``contention_dominant`` — high wait_time AND high wait_count.
    * ``high_wait_count`` — high count but low cumulative time
      (short-but-frequent waits — usually app-side hot-row contention).
    * ``low_contention`` — included only when nothing crosses the
      thresholds; reports the busiest lock type for context.
    """

    lock_type: str
    waits: int
    wait_time_us: int
    reason: str
    suggested_followup: str


@dataclass(frozen=True)
class LockHotspotsResult:
    """Roll-up of :func:`analyze_lock_hotspots`."""

    available: bool
    server_version_num: int
    detail: str
    hotspots: list[LockHotspot] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared probes
# ---------------------------------------------------------------------------


async def _server_version(driver: SqlDriver) -> tuple[int, str]:
    """Return ``(server_version_num, server_version)`` in one round trip."""
    rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::int AS ver_num, current_setting('server_version') AS ver",
        force_readonly=True,
    )
    if not rows:
        return 0, ""
    cells = rows[0].cells
    return int(cells.get("ver_num") or 0), str(cells.get("ver") or "")


async def _view_present(driver: SqlDriver, view_name: str) -> bool:
    """Check whether a system view exists. Parameter-bound — safe."""
    rows = await driver.execute_query(
        "SELECT 1 AS present FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'pg_catalog' AND c.relname = %s",
        params=[view_name],
        force_readonly=True,
    )
    return bool(rows)


# ---------------------------------------------------------------------------
# Status — never raises
# ---------------------------------------------------------------------------


async def get_pg19_stats_status(driver: SqlDriver) -> Pg19StatsStatus:
    """Report whether the PG 19 lock + recovery views are usable.

    Read-only; never raises. On PG < 19, or on PG 19 builds that don't
    expose the new views yet, returns ``available=False`` with a
    diagnostic pointing the agent at the legacy
    `find_blocking_chains` / `pg_stat_replication` paths.
    """
    try:
        ver_num, ver = await _server_version(driver)
    except Exception as exc:
        return Pg19StatsStatus(
            available=False,
            server_version_num=0,
            server_version="",
            has_pg_stat_lock=False,
            has_pg_stat_recovery=False,
            detail=(
                f"PG 19 stats unavailable (server version probe failed: {exc}). "
                "Fall back to find_blocking_chains / pg_stat_replication for "
                "lock + recovery observability on older servers."
            ),
        )
    if ver_num < _MIN_PG19_VERSION:
        return Pg19StatsStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            has_pg_stat_lock=False,
            has_pg_stat_recovery=False,
            detail=(
                "pg_stat_lock and pg_stat_recovery require PostgreSQL 19 or "
                "newer; this server is older. Use find_blocking_chains for "
                "per-blocker visibility and pg_stat_replication for replay "
                "lag on PG ≤ 18."
            ),
        )
    try:
        has_lock = await _view_present(driver, "pg_stat_lock")
        has_recovery = await _view_present(driver, "pg_stat_recovery")
    except Exception as exc:
        return Pg19StatsStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            has_pg_stat_lock=False,
            has_pg_stat_recovery=False,
            detail=(f"PG 19 reachable but view-presence probe failed: {exc}. Re-run after the server is back online."),
        )
    available = has_lock or has_recovery
    if not available:
        detail = (
            "PG 19 server is reachable but neither pg_stat_lock nor "
            "pg_stat_recovery is present. Early Beta build, or the views "
            "are not yet populated. Fall back to find_blocking_chains / "
            "pg_stat_replication."
        )
    else:
        present = ", ".join(v for v, ok in (("pg_stat_lock", has_lock), ("pg_stat_recovery", has_recovery)) if ok)
        detail = (
            f"PG 19 lock + recovery views available: {present}. "
            "Use read_pg_stat_lock / read_pg_stat_recovery for raw rows, "
            "or analyze_lock_hotspots for a ranked advisor view."
        )
    return Pg19StatsStatus(
        available=available,
        server_version_num=ver_num,
        server_version=ver,
        has_pg_stat_lock=has_lock,
        has_pg_stat_recovery=has_recovery,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


async def read_pg_stat_lock(driver: SqlDriver) -> list[LockStatRow]:
    """Return every row from ``pg_stat_lock``.

    Empty list on PG < 19 or when the view isn't present. The view
    aggregates per-lock-type acquire / wait counts since the most
    recent ``pg_stat_reset``.
    """
    ver_num, _ = await _server_version(driver)
    if ver_num < _MIN_PG19_VERSION:
        return []
    if not await _view_present(driver, "pg_stat_lock"):
        return []
    rows = await driver.execute_query(
        "SELECT "
        "  lock_type, "
        "  COALESCE(acquires, 0) AS acquires, "
        "  COALESCE(waits, 0) AS waits, "
        "  COALESCE(wait_time_us, 0) AS wait_time_us, "
        "  stats_reset::text AS stats_reset "
        "FROM pg_stat_lock "
        "ORDER BY wait_time_us DESC, waits DESC",
        force_readonly=True,
    )
    return [
        LockStatRow(
            lock_type=str(row.cells["lock_type"]),
            acquires=int(row.cells["acquires"]),
            waits=int(row.cells["waits"]),
            wait_time_us=int(row.cells["wait_time_us"]),
            stats_reset=row.cells.get("stats_reset"),
        )
        for row in rows or []
    ]


async def read_pg_stat_recovery(driver: SqlDriver) -> list[RecoveryStatRow]:
    """Return the row(s) from ``pg_stat_recovery``.

    Empty list on PG < 19 or when the view isn't present, or when the
    server isn't in recovery (a primary running standalone returns no
    rows). Wraps in a list for forward-compat if future PG versions
    expose per-standby breakdowns.
    """
    ver_num, _ = await _server_version(driver)
    if ver_num < _MIN_PG19_VERSION:
        return []
    if not await _view_present(driver, "pg_stat_recovery"):
        return []
    rows = await driver.execute_query(
        "SELECT "
        "  replay_lsn::text AS replay_lsn, "
        "  EXTRACT(epoch FROM replay_lag) AS replay_lag_seconds, "
        "  last_replayed_at::text AS last_replayed_at, "
        "  startup_state, "
        "  stats_reset::text AS stats_reset "
        "FROM pg_stat_recovery",
        force_readonly=True,
    )
    return [
        RecoveryStatRow(
            replay_lsn=row.cells.get("replay_lsn"),
            replay_lag_seconds=(
                float(row.cells["replay_lag_seconds"]) if row.cells.get("replay_lag_seconds") is not None else None
            ),
            last_replayed_at=row.cells.get("last_replayed_at"),
            startup_state=row.cells.get("startup_state"),
            stats_reset=row.cells.get("stats_reset"),
        )
        for row in rows or []
    ]


# ---------------------------------------------------------------------------
# Advisor — analyze_lock_hotspots
# ---------------------------------------------------------------------------


def _classify_lock(*, waits: int, wait_time_us: int) -> tuple[str, str]:
    """Map ``(waits, wait_time_us)`` to ``(reason, suggested_followup)``.

    The decision tree is deliberately conservative — we don't push
    operators toward a specific remedy because the right fix depends
    on which lock type is hot (advisory locks need app-level review;
    relation locks suggest VACUUM / DDL contention; tuple locks
    suggest hot-row UPDATEs).
    """
    high_time = wait_time_us >= _HIGH_WAIT_TIME_US
    high_count = waits >= _HIGH_WAIT_COUNT
    if high_time and high_count:
        return (
            "contention_dominant",
            "Investigate the workload generating these waits — check find_blocking_chains for the active culprits.",
        )
    if high_time:
        return (
            "high_wait_time",
            "Few but long waits — look for a long-running transaction holding the lock (pg_stat_activity.xact_start).",
        )
    if high_count:
        return (
            "high_wait_count",
            "Many short waits — usually app-side hot-row contention. "
            "Consider rate-limiting the call site or batching writes.",
        )
    return (
        "low_contention",
        "Cluster-wide lock waits are within healthy bounds; no action needed.",
    )


async def analyze_lock_hotspots(driver: SqlDriver) -> LockHotspotsResult:
    """Rank ``pg_stat_lock`` rows by wait dominance, emit reason codes.

    Read-only — never modifies state. Returns at most one hotspot per
    lock_type. When nothing crosses the thresholds we still emit one
    ``low_contention`` row for the busiest lock type so the agent has
    something to report back ("nothing to see here, but here's what's
    happening anyway").

    Empty ``hotspots`` list with a clear ``detail`` on PG < 19 or when
    pg_stat_lock isn't present.
    """
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_PG19_VERSION:
        return LockHotspotsResult(
            available=False,
            server_version_num=ver_num,
            detail=(
                f"pg_stat_lock requires PostgreSQL 19 or newer; this server "
                f"reports {ver or 'unknown'} (server_version_num={ver_num}). "
                "Use find_blocking_chains for per-blocker visibility on PG ≤ 18."
            ),
            hotspots=[],
        )
    if not await _view_present(driver, "pg_stat_lock"):
        return LockHotspotsResult(
            available=False,
            server_version_num=ver_num,
            detail=(
                "PG 19 server is reachable but pg_stat_lock is not present. "
                "Early Beta build, or the stats collector hasn't populated "
                "it yet. Fall back to find_blocking_chains."
            ),
            hotspots=[],
        )
    rows = await read_pg_stat_lock(driver)
    if not rows:
        return LockHotspotsResult(
            available=True,
            server_version_num=ver_num,
            detail="pg_stat_lock present but empty. No lock activity recorded since the last stats reset.",
            hotspots=[],
        )
    hot: list[LockHotspot] = []
    for row in rows:
        if row.waits == 0 and row.wait_time_us == 0:
            continue
        reason, followup = _classify_lock(waits=row.waits, wait_time_us=row.wait_time_us)
        if reason == "low_contention":
            continue
        hot.append(
            LockHotspot(
                lock_type=row.lock_type,
                waits=row.waits,
                wait_time_us=row.wait_time_us,
                reason=reason,
                suggested_followup=followup,
            )
        )
    if not hot:
        # Nothing crossed the threshold — still emit one informational
        # row pointing at the busiest type so the agent has context.
        top = rows[0]
        hot.append(
            LockHotspot(
                lock_type=top.lock_type,
                waits=top.waits,
                wait_time_us=top.wait_time_us,
                reason="low_contention",
                suggested_followup=("Cluster-wide lock waits are within healthy bounds; no action needed."),
            )
        )
        detail = (
            f"No lock-type crosses the contention threshold "
            f"(>= {_HIGH_WAIT_TIME_US} us cumulative wait or >= {_HIGH_WAIT_COUNT} waits). "
            "Busiest type included for context only."
        )
    else:
        detail = (
            f"{len(hot)} lock type(s) crossed the contention threshold. "
            "Pair with find_blocking_chains for the active culprits."
        )
    return LockHotspotsResult(
        available=True,
        server_version_num=ver_num,
        detail=detail,
        hotspots=hot,
    )


__all__ = [
    "LockHotspot",
    "LockHotspotsResult",
    "LockStatRow",
    "Pg19StatsError",
    "Pg19StatsStatus",
    "RecoveryStatRow",
    "analyze_lock_hotspots",
    "get_pg19_stats_status",
    "read_pg_stat_lock",
    "read_pg_stat_recovery",
]
