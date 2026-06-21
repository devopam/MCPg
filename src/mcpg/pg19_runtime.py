"""PG 19 runtime toggles — online data checksums + on-demand logical replication.

PG 19 introduces two restart-free operational toggles that were previously
"plan a maintenance window" tasks:

* **Online data checksums** — flip ``data_checksums`` on or off without
  bouncing the cluster. Previously required ``initdb -k`` (only at
  cluster creation) or the ``pg_checksums`` offline tool. PG 19 ships
  ``pg_enable_data_checksums()`` / ``pg_disable_data_checksums()`` SQL
  functions that toggle the GUC and rewrite affected pages
  incrementally.
* **On-demand logical replication** — flipping ``wal_level`` to
  ``logical`` no longer requires a restart in PG 19; ``ALTER SYSTEM SET
  wal_level = 'logical'`` + ``pg_reload_conf()`` takes effect for new
  WAL traffic. The change is detectable via the new ``effective_wal_level``
  preset GUC.

This module wraps both as MCPg tools:

* ``get_data_checksums_status`` / ``enable_data_checksums`` /
  ``disable_data_checksums`` — checksum toggles.
* ``get_logical_replication_status`` / ``enable_logical_replication_on_demand``
  — wal_level toggle.

Backward compatibility
----------------------
The module is additive. PG ≤ 18 operators continue using the offline
``pg_checksums`` shell-out + planned-restart wal_level flip — both
status probes return ``available=False`` with a guidance string when
the server is older.

Security posture
----------------
Both write surfaces dispatch through :meth:`Database.run_unmanaged`
because ``ALTER SYSTEM`` requires the autocommit path. The SQL payloads
contain no caller-supplied identifiers — every value is a literal
constant — so there's no injection surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.database import Database

# PG 19 ships both toggles. The version-num probe is the boundary —
# no extension to install.
_MIN_PG19_RUNTIME_VERSION = 190000

# Logical-replication wal_level values. Used to assert post-toggle state.
_LOGICAL_WAL_LEVEL = "logical"
_VALID_WAL_LEVELS = frozenset({"minimal", "replica", "logical"})


class Pg19RuntimeError(Exception):
    """Raised when a PG 19 runtime-toggle operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataChecksumsStatus:
    """Reports whether online checksum toggling is usable + the current state.

    ``available`` is True when ``server_version_num`` >= 190000.
    ``enabled`` is the current value of the ``data_checksums`` GUC
    (None when the probe failed). ``detail`` is a guidance string
    pointing the agent at the offline ``pg_checksums`` path on PG ≤ 18.
    """

    available: bool
    server_version_num: int
    server_version: str
    enabled: bool | None
    detail: str


@dataclass(frozen=True, slots=True)
class ToggleDataChecksumsResult:
    """Outcome of `enable_data_checksums` / `disable_data_checksums`.

    ``was_enabled`` is the value before the toggle; ``now_enabled`` is
    the value after. When the toggle was a no-op (already in the
    desired state) ``was_enabled == now_enabled`` and ``changed=False``.
    """

    was_enabled: bool | None
    now_enabled: bool
    changed: bool
    toggle_sql: str


@dataclass(frozen=True, slots=True)
class LogicalReplicationStatus:
    """Reports whether on-demand logical replication is usable + the current state.

    ``wal_level`` and ``effective_wal_level`` may differ: the former is
    the configured value (post-ALTER SYSTEM), the latter is what the
    server is actually emitting (the PG 19 distinction). When they
    diverge it's usually because a reload hasn't happened yet.
    """

    available: bool
    server_version_num: int
    server_version: str
    wal_level: str | None
    effective_wal_level: str | None
    max_replication_slots: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class EnableLogicalReplicationOnDemandResult:
    """Outcome of `enable_logical_replication_on_demand`.

    ``previous_wal_level`` is the configured value before the change;
    ``new_wal_level`` is the configured value after. ``requires_restart``
    is True on PG ≤ 18 (where the toggle still needs a restart);
    False on PG 19+ (where ``pg_reload_conf()`` is enough). The
    function refuses to run on PG ≤ 18 — operators see a ``Pg19RuntimeError``
    pointing them at the documented restart path.
    """

    previous_wal_level: str | None
    new_wal_level: str
    requires_restart: bool
    detail: str


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


async def _bool_setting(driver: SqlDriver, name: str) -> bool | None:
    """Return a Postgres boolean GUC's current value, or None on failure.

    Postgres returns the GUC value as a string ('on' / 'off' / 'true' /
    'false') — we normalise to bool here.
    """
    rows = await driver.execute_query(
        "SELECT current_setting(%s, true) AS val",
        params=[name],
        force_readonly=True,
    )
    if not rows:
        return None
    raw = rows[0].cells.get("val")
    if raw is None:
        return None
    return str(raw).strip().lower() in {"on", "true", "yes", "1"}


async def _string_setting(driver: SqlDriver, name: str) -> str | None:
    """Return a Postgres string GUC's current value, or None on failure."""
    rows = await driver.execute_query(
        "SELECT current_setting(%s, true) AS val",
        params=[name],
        force_readonly=True,
    )
    if not rows:
        return None
    raw = rows[0].cells.get("val")
    return None if raw is None else str(raw)


async def _int_setting(driver: SqlDriver, name: str) -> int | None:
    """Return a Postgres integer GUC's current value, or None on failure."""
    raw = await _string_setting(driver, name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Data checksums — status + toggles
# ---------------------------------------------------------------------------


async def get_data_checksums_status(driver: SqlDriver) -> DataChecksumsStatus:
    """Report whether online checksum toggling is usable + the current state.

    Read-only; never raises. On PG ≤ 18 returns ``available=False`` with
    a diagnostic pointing the agent at the offline ``pg_checksums``
    shell-out path.
    """
    try:
        ver_num, ver = await _server_version(driver)
    except Exception as exc:
        return DataChecksumsStatus(
            available=False,
            server_version_num=0,
            server_version="",
            enabled=None,
            detail=(
                f"data_checksums status unavailable (version probe failed: {exc}). "
                "Re-run after the server is back online."
            ),
        )
    if ver_num < _MIN_PG19_RUNTIME_VERSION:
        # Even on PG ≤ 18 we can still report whether checksums are on
        # — that's a static cluster attribute set at initdb time. Useful
        # context for the agent even though the toggle isn't available.
        try:
            enabled = await _bool_setting(driver, "data_checksums")
        except Exception:
            enabled = None
        return DataChecksumsStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            enabled=enabled,
            detail=(
                "Online data_checksums toggle requires PostgreSQL 19 or newer; "
                "this server is older. Use the offline `pg_checksums` tool "
                "during a maintenance window to flip the setting."
            ),
        )
    try:
        enabled = await _bool_setting(driver, "data_checksums")
    except Exception as exc:
        return DataChecksumsStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            enabled=None,
            detail=(f"PG 19 reachable but data_checksums probe failed: {exc}. Re-run after the server is back online."),
        )
    state = "enabled" if enabled else "disabled"
    detail = (
        f"Online data_checksums toggle is available. Currently {state}. "
        "Call enable_data_checksums / disable_data_checksums to change."
    )
    return DataChecksumsStatus(
        available=True,
        server_version_num=ver_num,
        server_version=ver,
        enabled=enabled,
        detail=detail,
    )


async def _toggle_data_checksums(database: Database, *, enable: bool) -> ToggleDataChecksumsResult:
    """Shared implementation for `enable_data_checksums` / `disable_data_checksums`."""
    driver = database.driver()
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_PG19_RUNTIME_VERSION:
        raise Pg19RuntimeError(
            f"Online data_checksums toggle requires PostgreSQL 19 or newer; "
            f"this server reports {ver or 'unknown'} (server_version_num={ver_num}). "
            "Use the offline `pg_checksums` tool during a maintenance window."
        )
    was_enabled = await _bool_setting(driver, "data_checksums")
    if was_enabled is enable:
        # Already in desired state — emit a no-op result so the agent
        # can report "nothing to do" without re-querying.
        return ToggleDataChecksumsResult(
            was_enabled=was_enabled,
            now_enabled=enable,
            changed=False,
            toggle_sql=f"-- data_checksums already {'enabled' if enable else 'disabled'}; no-op",
        )
    # PG 19's online toggle functions: pg_enable_data_checksums() /
    # pg_disable_data_checksums(). No arguments, no caller-supplied
    # identifiers — pure constant SQL.
    toggle_sql = "SELECT pg_enable_data_checksums()" if enable else "SELECT pg_disable_data_checksums()"
    try:
        await database.run_unmanaged(toggle_sql)
    except Exception as exc:
        raise Pg19RuntimeError(f"data_checksums toggle failed: {exc}") from exc
    return ToggleDataChecksumsResult(
        was_enabled=was_enabled,
        now_enabled=enable,
        changed=True,
        toggle_sql=toggle_sql,
    )


async def enable_data_checksums(database: Database) -> ToggleDataChecksumsResult:
    """Turn data_checksums on without restarting the cluster.

    Calls PG 19's ``pg_enable_data_checksums()`` SQL function. The
    function returns immediately; the rewrite of affected pages
    happens in the background via the new ``data_checksum_worker``.
    No-op (with ``changed=False``) when checksums are already on.

    Requires PG 19+. Raises ``Pg19RuntimeError`` on older versions; the
    error message points the caller at the offline ``pg_checksums``
    path.
    """
    return await _toggle_data_checksums(database, enable=True)


async def disable_data_checksums(database: Database) -> ToggleDataChecksumsResult:
    """Turn data_checksums off without restarting the cluster.

    Calls PG 19's ``pg_disable_data_checksums()`` SQL function. No-op
    (with ``changed=False``) when checksums are already off.

    Requires PG 19+. Raises ``Pg19RuntimeError`` on older versions.
    """
    return await _toggle_data_checksums(database, enable=False)


# ---------------------------------------------------------------------------
# Logical replication — status + on-demand toggle
# ---------------------------------------------------------------------------


async def get_logical_replication_status(driver: SqlDriver) -> LogicalReplicationStatus:
    """Report whether on-demand logical replication is usable + the current state.

    Read-only; never raises. Reports both ``wal_level`` (configured) and
    ``effective_wal_level`` (actual) so the agent can tell when a reload
    hasn't happened yet. On PG ≤ 18 returns ``available=False`` with
    a diagnostic pointing the agent at the documented restart path.
    """
    try:
        ver_num, ver = await _server_version(driver)
    except Exception as exc:
        return LogicalReplicationStatus(
            available=False,
            server_version_num=0,
            server_version="",
            wal_level=None,
            effective_wal_level=None,
            max_replication_slots=None,
            detail=(
                f"Logical replication status unavailable (version probe failed: {exc}). "
                "Re-run after the server is back online."
            ),
        )
    try:
        wal_level = await _string_setting(driver, "wal_level")
        # effective_wal_level is the PG 19 preset GUC; falls back to
        # None on older servers (current_setting(..., true) returns
        # NULL when the GUC doesn't exist).
        effective = await _string_setting(driver, "effective_wal_level")
        max_slots = await _int_setting(driver, "max_replication_slots")
    except Exception as exc:
        return LogicalReplicationStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            wal_level=None,
            effective_wal_level=None,
            max_replication_slots=None,
            detail=(f"Server reachable but wal_level probe failed: {exc}. Re-run after the server is back online."),
        )
    available = ver_num >= _MIN_PG19_RUNTIME_VERSION
    if not available:
        detail = (
            "On-demand logical replication (flipping wal_level without "
            "restart) requires PostgreSQL 19 or newer; this server is older. "
            "Edit postgresql.conf and restart to flip wal_level."
        )
    elif wal_level == _LOGICAL_WAL_LEVEL and effective == _LOGICAL_WAL_LEVEL:
        detail = "wal_level is already 'logical' and effective. Logical replication slots can be created on demand."
    elif wal_level == _LOGICAL_WAL_LEVEL and effective != _LOGICAL_WAL_LEVEL:
        detail = (
            f"wal_level configured to 'logical' but effective_wal_level is "
            f"'{effective or 'unknown'}' — pg_reload_conf() not yet called, "
            "or pending a backend recycle. Call enable_logical_replication_on_demand() "
            "to force the reload."
        )
    else:
        detail = (
            f"wal_level is '{wal_level or 'unknown'}'. Call "
            "enable_logical_replication_on_demand() to flip it to 'logical' "
            "without a server restart."
        )
    return LogicalReplicationStatus(
        available=available,
        server_version_num=ver_num,
        server_version=ver,
        wal_level=wal_level,
        effective_wal_level=effective,
        max_replication_slots=max_slots,
        detail=detail,
    )


async def enable_logical_replication_on_demand(
    database: Database,
) -> EnableLogicalReplicationOnDemandResult:
    """Flip wal_level to 'logical' without restarting the cluster.

    Sequence on PG 19+:
      1. Probe current wal_level.
      2. ``ALTER SYSTEM SET wal_level = 'logical'`` (persists to
         postgresql.auto.conf).
      3. ``SELECT pg_reload_conf()`` — PG 19's on-demand wal_level
         change takes effect for new WAL traffic without a restart.
      4. Re-probe and report.

    Requires PG 19+. Raises ``Pg19RuntimeError`` on older versions where
    the wal_level flip still needs a planned restart.
    """
    driver = database.driver()
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_PG19_RUNTIME_VERSION:
        raise Pg19RuntimeError(
            "On-demand wal_level flip requires PostgreSQL 19 or newer; "
            f"this server reports {ver or 'unknown'} (server_version_num={ver_num}). "
            "Edit postgresql.conf, set wal_level = logical, and restart."
        )
    previous = await _string_setting(driver, "wal_level")
    if previous == _LOGICAL_WAL_LEVEL:
        return EnableLogicalReplicationOnDemandResult(
            previous_wal_level=previous,
            new_wal_level=_LOGICAL_WAL_LEVEL,
            requires_restart=False,
            detail="wal_level was already 'logical'; no-op.",
        )
    # No caller-supplied identifiers — the literal 'logical' is fixed.
    try:
        await database.run_unmanaged("ALTER SYSTEM SET wal_level = 'logical'")
        await database.run_unmanaged("SELECT pg_reload_conf()")
    except Exception as exc:
        raise Pg19RuntimeError(f"wal_level on-demand toggle failed: {exc}") from exc
    return EnableLogicalReplicationOnDemandResult(
        previous_wal_level=previous,
        new_wal_level=_LOGICAL_WAL_LEVEL,
        requires_restart=False,
        detail=(
            f"wal_level changed from '{previous or 'unknown'}' to 'logical' "
            "without a server restart. effective_wal_level updates on the "
            "next backend recycle for in-flight sessions."
        ),
    )


__all__ = [
    "DataChecksumsStatus",
    "EnableLogicalReplicationOnDemandResult",
    "LogicalReplicationStatus",
    "Pg19RuntimeError",
    "ToggleDataChecksumsResult",
    "disable_data_checksums",
    "enable_data_checksums",
    "enable_logical_replication_on_demand",
    "get_data_checksums_status",
    "get_logical_replication_status",
]
