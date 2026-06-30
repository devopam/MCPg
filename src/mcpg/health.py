"""Database health checks.

Each check runs a single read-only stats query and classifies the result as
``ok`` or ``warning``. ``check_database_health`` runs them all and summarises.
Authored fresh — the upstream health module was not vendored (see ADR-0001).
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

# Classification thresholds.
_CONNECTION_WARN_RATIO = 0.8  # warn above 80% of max_connections
_CACHE_HIT_WARN_RATIO = 0.99  # warn below a 99% buffer cache hit ratio
_REPLICATION_LAG_WARN_BYTES = 64 * 1024 * 1024  # warn above 64 MiB of standby lag
_BLOAT_RATIO_WARN = 2.0  # warn when a table occupies 2x its estimated minimum
_BLOAT_MIN_PAGES = 128  # ignore tables smaller than ~1 MiB

_OK = "ok"
_WARNING = "warning"


@dataclass(frozen=True)
class HealthCheck:
    """The result of a single health check."""

    name: str
    status: str
    detail: str


@dataclass(frozen=True)
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


# --- analyze_table_bloat (roadmap 2.7) -------------------------------

# Default cap on how many tables / indexes the report lists. The query
# scans the whole schema; the cap bounds the payload, not the scan.
DEFAULT_BLOAT_LIMIT = 20

# Tuple-header overhead per row used by the catalog estimate (the same
# 24-byte figure check_table_bloat uses) and the per-page fill budget.
_FILLED_PAGE_BYTES = 8192 - 24
_ROW_HEADER_BYTES = 24


@dataclass(frozen=True)
class IndexBloat:
    """Estimated bloat for a single index.

    ``est_bloat_pct`` is the share of the index that the estimate
    considers wasted: 0 means the index is at (or below) its estimated
    minimum size, higher means more slack. In ``pgstattuple`` mode it is
    derived from ``pgstatindex.avg_leaf_density``; otherwise it is a
    catalog estimate from ``relpages`` against a reltuples-derived floor.
    """

    schema: str
    table: str
    index: str
    est_bloat_pct: float
    index_bytes: int
    detail: str


@dataclass(frozen=True)
class TableBloat:
    """Estimated bloat + dead-tuple pressure for a single table.

    ``est_bloat_pct`` is the catalog (or ``pgstattuple``) estimate of the
    wasted share of the heap. ``dead_tuple_pct`` is the live-stats
    dead-tuple ratio from ``pg_stat_user_tables`` — a separate signal
    (recently-dead tuples a VACUUM would reclaim) that does not always
    move with the structural bloat estimate.
    """

    schema: str
    table: str
    est_bloat_pct: float
    dead_tuple_pct: float
    n_dead_tup: int
    n_live_tup: int
    table_bytes: int
    detail: str


@dataclass(frozen=True)
class TableBloatReport:
    """The outcome of :func:`analyze_table_bloat`.

    ``method`` is ``"estimate"`` (catalog-only) or ``"pgstattuple"``
    (precise, requires the extension). ``tables`` and ``indexes`` are each
    sorted worst-first by estimated bloat and capped at the requested
    ``limit``. ``available`` is ``False`` only when the driver itself
    failed.
    """

    available: bool
    schema: str
    method: str
    tables: list[TableBloat]
    indexes: list[IndexBloat]
    detail: str


# Catalog estimate of table bloat. Mirrors check_table_bloat's formula:
# relpages against ceil(reltuples*(row_width+24)/(8192-24)). Joins
# pg_stat_user_tables for dead/live tuples and pg_total_relation_size for
# the byte size. Schema is parametrised.
_TABLE_BLOAT_ESTIMATE_SQL = """
WITH t AS (
    SELECT
        n.nspname AS schema,
        c.relname AS table,
        c.oid AS relid,
        c.relpages,
        c.reltuples,
        (SELECT sum(COALESCE(s.avg_width, 0)) FROM pg_stats s
         WHERE s.schemaname = n.nspname AND s.tablename = c.relname) AS row_width
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r' AND n.nspname = %s
)
SELECT
    t.schema,
    t."table",
    GREATEST(0.0,
        CASE
            WHEN t.reltuples > 0 AND t.row_width > 0 AND t.relpages > 0
            THEN (1.0 - (ceil(t.reltuples * (t.row_width + %s) / %s) / t.relpages::numeric)) * 100.0
            ELSE 0.0
        END
    ) AS est_bloat_pct,
    COALESCE(st.n_dead_tup, 0) AS n_dead_tup,
    COALESCE(st.n_live_tup, 0) AS n_live_tup,
    (COALESCE(st.n_dead_tup, 0)::numeric
        / GREATEST(COALESCE(st.n_live_tup, 0), 1)) * 100.0 AS dead_tuple_pct,
    pg_total_relation_size(t.relid) AS table_bytes
FROM t
LEFT JOIN pg_stat_user_tables st ON st.relid = t.relid
"""


# Per-index catalog estimate: relpages against a floor derived from the
# index's own reltuples (one row pointer per tuple, ~16 bytes, packed into
# the per-page budget). A crude but cheap upper-bound on healthy size.
_INDEX_BLOAT_ESTIMATE_SQL = """
SELECT
    n.nspname AS schema,
    t.relname AS "table",
    c.relname AS index,
    GREATEST(0.0,
        CASE
            WHEN c.reltuples > 0 AND c.relpages > 0
            THEN (1.0 - (ceil(c.reltuples * 16 / %s) / c.relpages::numeric)) * 100.0
            ELSE 0.0
        END
    ) AS est_bloat_pct,
    pg_relation_size(c.oid) AS index_bytes
FROM pg_class c
JOIN pg_index i ON i.indexrelid = c.oid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'i' AND n.nspname = %s
"""


# Precise table bloat via pgstattuple — dead_tuple_percent + free_percent
# sum to the share of the heap not occupied by live tuples.
_TABLE_BLOAT_PRECISE_SQL = """
WITH t AS (
    SELECT n.nspname AS schema, c.relname AS table, c.oid AS relid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r' AND n.nspname = %s
)
SELECT
    t.schema,
    t."table",
    GREATEST(0.0, ps.dead_tuple_percent + ps.free_percent) AS est_bloat_pct,
    COALESCE(st.n_dead_tup, 0) AS n_dead_tup,
    COALESCE(st.n_live_tup, 0) AS n_live_tup,
    ps.dead_tuple_percent AS dead_tuple_pct,
    pg_total_relation_size(t.relid) AS table_bytes
FROM t
CROSS JOIN LATERAL pgstattuple(t.relid) ps
LEFT JOIN pg_stat_user_tables st ON st.relid = t.relid
"""


# Precise btree-index bloat via pgstatindex — 100 - avg_leaf_density is the
# wasted share of leaf pages. Non-btree indexes (where pgstatindex errors)
# are skipped by the relam filter.
_INDEX_BLOAT_PRECISE_SQL = """
SELECT
    n.nspname AS schema,
    t.relname AS "table",
    c.relname AS index,
    GREATEST(0.0, 100.0 - psi.avg_leaf_density) AS est_bloat_pct,
    pg_relation_size(c.oid) AS index_bytes
FROM pg_class c
JOIN pg_index i ON i.indexrelid = c.oid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_am am ON am.oid = c.relam
CROSS JOIN LATERAL pgstatindex(c.oid) psi
WHERE c.relkind = 'i' AND am.amname = 'btree' AND n.nspname = %s
"""


async def analyze_table_bloat(
    driver: SqlDriver,
    schema: str,
    *,
    limit: int = DEFAULT_BLOAT_LIMIT,
    precise: bool = False,
) -> TableBloatReport:
    """Rank a schema's tables and indexes by estimated bloat, worst first.

    By default uses a cheap catalog-only estimate (``relpages`` vs a size
    derived from ``reltuples`` and the average row width) plus the
    dead-tuple ratio from ``pg_stat_user_tables``. When ``precise=True``
    **and** the ``pgstattuple`` extension is installed, switches to
    ``pgstattuple`` / ``pgstatindex`` for an exact (but I/O-heavy) read;
    if the extension is absent it transparently falls back to the
    estimate and reports ``method="estimate"``.

    Read-only. ``available`` is ``False`` only when the driver fails;
    an empty schema yields empty lists with ``available=True``.

    Raises:
        ValueError: When ``limit`` is not positive.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")

    method = "estimate"
    use_precise = False
    if precise:
        use_precise = await extension_installed(driver, "pgstattuple")
        method = "pgstattuple" if use_precise else "estimate"

    try:
        if use_precise:
            table_rows = await driver.execute_query(_TABLE_BLOAT_PRECISE_SQL, params=[schema], force_readonly=True)
            index_rows = await driver.execute_query(_INDEX_BLOAT_PRECISE_SQL, params=[schema], force_readonly=True)
        else:
            table_rows = await driver.execute_query(
                _TABLE_BLOAT_ESTIMATE_SQL,
                params=[schema, _ROW_HEADER_BYTES, _FILLED_PAGE_BYTES],
                force_readonly=True,
            )
            index_rows = await driver.execute_query(
                _INDEX_BLOAT_ESTIMATE_SQL,
                params=[_FILLED_PAGE_BYTES, schema],
                force_readonly=True,
            )
    except Exception as exc:
        return TableBloatReport(
            available=False,
            schema=schema,
            method=method,
            tables=[],
            indexes=[],
            detail=f"bloat analysis failed: {exc}",
        )

    tables = [
        TableBloat(
            schema=cells["schema"],
            table=cells["table"],
            est_bloat_pct=float(cells["est_bloat_pct"]),
            dead_tuple_pct=float(cells["dead_tuple_pct"]),
            n_dead_tup=int(cells["n_dead_tup"]),
            n_live_tup=int(cells["n_live_tup"]),
            table_bytes=int(cells["table_bytes"] or 0),
            detail=f"{float(cells['est_bloat_pct']):.1f}% estimated bloat",
        )
        for cells in (row.cells for row in table_rows or [])
    ]
    indexes = [
        IndexBloat(
            schema=cells["schema"],
            table=cells["table"],
            index=cells["index"],
            est_bloat_pct=float(cells["est_bloat_pct"]),
            index_bytes=int(cells["index_bytes"] or 0),
            detail=f"{float(cells['est_bloat_pct']):.1f}% estimated bloat",
        )
        for cells in (row.cells for row in index_rows or [])
    ]

    tables.sort(key=lambda t: t.est_bloat_pct, reverse=True)
    indexes.sort(key=lambda i: i.est_bloat_pct, reverse=True)

    return TableBloatReport(
        available=True,
        schema=schema,
        method=method,
        tables=tables[:limit],
        indexes=indexes[:limit],
        detail=f"{len(tables)} tables, {len(indexes)} indexes analysed ({method})",
    )


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
