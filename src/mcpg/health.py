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


async def check_database_health(driver: SqlDriver) -> HealthReport:
    """Run every health check and summarise the overall status."""
    checks = [
        await check_connections(driver),
        await check_cache_hit_ratio(driver),
        await check_dead_tuples(driver),
        await check_invalid_indexes(driver),
    ]
    status = _OK if all(check.status == _OK for check in checks) else _WARNING
    return HealthReport(status=status, checks=checks)
