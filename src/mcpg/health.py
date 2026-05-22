"""Database health checks.

Each check runs a single read-only stats query and classifies the result as
``ok`` or ``warning``. ``check_database_health`` runs them all and summarises.
Authored fresh — the upstream health module was not vendored (see ADR-0001).
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# Classification thresholds.
_CONNECTION_WARN_RATIO = 0.8  # warn above 80% of max_connections
_CACHE_HIT_WARN_RATIO = 0.99  # warn below a 99% buffer cache hit ratio
_REPLICATION_LAG_WARN_BYTES = 64 * 1024 * 1024  # warn above 64 MiB of standby lag
_BLOAT_RATIO_WARN = 2.0  # warn when a table occupies 2x its estimated minimum
_BLOAT_MIN_PAGES = 128  # ignore tables smaller than ~1 MiB

_OK = "ok"
_WARNING = "warning"


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """The result of a single health check."""

    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The combined result of every health check."""

    status: str
    checks: list[HealthCheck]


async def check_connections(driver: SqlDriver) -> HealthCheck:
    """Compare active backends against ``max_connections``."""
    rows = await driver.execute_query(
        "SELECT count(*) AS used, current_setting('max_connections')::int AS max_connections FROM pg_stat_activity",
        force_readonly=True,
    )
    cells = (rows or [])[0].cells
    used, maximum = cells["used"], cells["max_connections"]
    ratio = used / maximum if maximum else 0.0
    status = _WARNING if ratio > _CONNECTION_WARN_RATIO else _OK
    return HealthCheck("connections", status, f"{used}/{maximum} connections in use")


async def check_cache_hit_ratio(driver: SqlDriver) -> HealthCheck:
    """Check the buffer cache hit ratio across the cluster."""
    rows = await driver.execute_query(
        "SELECT sum(blks_hit) AS hits, sum(blks_read) AS reads FROM pg_stat_database",
        force_readonly=True,
    )
    cells = (rows or [])[0].cells
    hits, reads = cells["hits"] or 0, cells["reads"] or 0
    total = hits + reads
    ratio = hits / total if total else 1.0
    status = _WARNING if ratio < _CACHE_HIT_WARN_RATIO else _OK
    return HealthCheck("cache_hit_ratio", status, f"{ratio:.4f} of block reads served from cache")


async def check_dead_tuples(driver: SqlDriver) -> HealthCheck:
    """Count tables with enough dead tuples to need vacuuming."""
    rows = await driver.execute_query(
        "SELECT count(*) AS bloated FROM pg_stat_user_tables "
        "WHERE n_dead_tup > 1000 AND n_dead_tup > 0.1 * GREATEST(n_live_tup, 1)",
        force_readonly=True,
    )
    bloated = (rows or [])[0].cells["bloated"]
    status = _WARNING if bloated > 0 else _OK
    return HealthCheck("dead_tuples", status, f"{bloated} tables need vacuuming")


async def check_invalid_indexes(driver: SqlDriver) -> HealthCheck:
    """Count indexes left in an invalid state (e.g. a failed build)."""
    rows = await driver.execute_query(
        "SELECT count(*) AS invalid FROM pg_index WHERE NOT indisvalid",
        force_readonly=True,
    )
    invalid = (rows or [])[0].cells["invalid"]
    status = _WARNING if invalid > 0 else _OK
    return HealthCheck("invalid_indexes", status, f"{invalid} invalid indexes")


async def check_replication_lag(driver: SqlDriver) -> HealthCheck:
    """Measure how far connected standbys trail in replaying WAL."""
    rows = await driver.execute_query(
        "SELECT count(*) AS standbys, "
        "COALESCE(max(pg_wal_lsn_diff("
        "CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() "
        "ELSE pg_current_wal_lsn() END, replay_lsn)), 0) AS max_lag_bytes "
        "FROM pg_stat_replication",
        force_readonly=True,
    )
    cells = (rows or [])[0].cells
    standbys, max_lag = cells["standbys"], int(cells["max_lag_bytes"])
    if standbys == 0:
        return HealthCheck("replication_lag", _OK, "no replication standbys connected")
    status = _WARNING if max_lag > _REPLICATION_LAG_WARN_BYTES else _OK
    return HealthCheck("replication_lag", status, f"{standbys} standby(s), max lag {max_lag} bytes")


async def check_table_bloat(driver: SqlDriver) -> HealthCheck:
    """Count tables far larger than their estimated minimum size.

    The estimate is catalog-only — ``relpages`` against a size derived from
    ``reltuples`` and the average row width — so the check stays cheap.
    """
    rows = await driver.execute_query(
        "WITH table_stats AS ("
        "SELECT c.relpages, c.reltuples, "
        "(SELECT sum(COALESCE(s.avg_width, 0)) FROM pg_stats s "
        "WHERE s.schemaname = n.nspname AND s.tablename = c.relname) AS row_width "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
        ") "
        "SELECT count(*) AS bloated FROM table_stats "
        "WHERE relpages > %s AND reltuples > 0 AND row_width > 0 "
        "AND relpages::numeric / GREATEST(ceil(reltuples * (row_width + 24) / (8192 - 24)), 1) > %s",
        params=[_BLOAT_MIN_PAGES, _BLOAT_RATIO_WARN],
        force_readonly=True,
    )
    bloated = (rows or [])[0].cells["bloated"]
    status = _WARNING if bloated > 0 else _OK
    return HealthCheck("table_bloat", status, f"{bloated} tables appear bloated")


async def check_database_health(driver: SqlDriver) -> HealthReport:
    """Run every health check and summarise the overall status."""
    checks = [
        await check_connections(driver),
        await check_cache_hit_ratio(driver),
        await check_dead_tuples(driver),
        await check_invalid_indexes(driver),
        await check_replication_lag(driver),
        await check_table_bloat(driver),
    ]
    status = _OK if all(check.status == _OK for check in checks) else _WARNING
    return HealthReport(status=status, checks=checks)
