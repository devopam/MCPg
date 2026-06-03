"""I/O stats reader — wraps ``pg_stat_io`` (PostgreSQL 16+).

``pg_stat_io`` reports cumulative I/O activity per ``(backend_type,
object, context)`` triple — reads, writes, extends, evictions, fsyncs.
Useful for spotting buffer-cache misses (high reads from "relation"
with low hit ratio) and write amplification (high extend counts on
small tables).

The view first shipped in PostgreSQL 16. On 14 / 15 the reader
returns ``available=False`` rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver


@dataclass(frozen=True, slots=True)
class IOStatRow:
    """One row from ``pg_stat_io``.

    Columns are the PG-16 surface, with nulls preserved as ``None``
    (some combinations of backend_type / context don't track every
    counter). Counts are cumulative since stats were last reset.
    """

    backend_type: str
    object: str
    context: str
    reads: int | None
    read_bytes: int | None
    read_time_ms: float | None
    writes: int | None
    write_bytes: int | None
    write_time_ms: float | None
    writebacks: int | None
    extends: int | None
    extend_bytes: int | None
    hits: int | None
    evictions: int | None
    reuses: int | None
    fsyncs: int | None


@dataclass(frozen=True, slots=True)
class IOStatsReport:
    """Aggregate result of :func:`read_pg_stat_io`.

    ``available`` is ``False`` on PG <16 where the view doesn't exist.
    ``server_version`` echoes ``current_setting('server_version_num')``
    so the agent can confirm the unavailability cause.
    """

    available: bool
    server_version: int
    rows: list[IOStatRow]


async def _server_version_num(driver: SqlDriver) -> int:
    rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::int AS v",
        force_readonly=True,
    )
    if not rows:
        return 0
    return int(rows[0].cells["v"])


async def read_pg_stat_io(driver: SqlDriver) -> IOStatsReport:
    """Read the ``pg_stat_io`` view.

    Returns ``available=False`` on PostgreSQL versions older than 16
    (where the view doesn't exist). Otherwise emits one
    :class:`IOStatRow` per ``(backend_type, object, context)`` triple
    the server tracks.
    """
    version = await _server_version_num(driver)
    if version < 160000:
        return IOStatsReport(available=False, server_version=version, rows=[])

    rows = await driver.execute_query(
        "SELECT backend_type, object, context, "
        "       reads, read_bytes, read_time, "
        "       writes, write_bytes, write_time, "
        "       writebacks, extends, extend_bytes, "
        "       hits, evictions, reuses, fsyncs "
        "FROM pg_stat_io "
        "ORDER BY backend_type, object, context",
        force_readonly=True,
    )
    return IOStatsReport(
        available=True,
        server_version=version,
        rows=[
            IOStatRow(
                backend_type=str(row.cells["backend_type"]),
                object=str(row.cells["object"]),
                context=str(row.cells["context"]),
                reads=_maybe_int(row.cells.get("reads")),
                read_bytes=_maybe_int(row.cells.get("read_bytes")),
                read_time_ms=_maybe_float(row.cells.get("read_time")),
                writes=_maybe_int(row.cells.get("writes")),
                write_bytes=_maybe_int(row.cells.get("write_bytes")),
                write_time_ms=_maybe_float(row.cells.get("write_time")),
                writebacks=_maybe_int(row.cells.get("writebacks")),
                extends=_maybe_int(row.cells.get("extends")),
                extend_bytes=_maybe_int(row.cells.get("extend_bytes")),
                hits=_maybe_int(row.cells.get("hits")),
                evictions=_maybe_int(row.cells.get("evictions")),
                reuses=_maybe_int(row.cells.get("reuses")),
                fsyncs=_maybe_int(row.cells.get("fsyncs")),
            )
            for row in rows or []
        ],
    )


def _maybe_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)  # type: ignore[call-overload,no-any-return]


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BufferCacheSummaryReport:
    """High-level shared buffer cache usage summary."""

    available: bool
    total_buffers: int | None = None
    free_buffers: int | None = None
    used_buffers: int | None = None
    dirty_buffers: int | None = None
    average_usage_count: float | None = None


@dataclass(frozen=True, slots=True)
class BufferCacheRelationRow:
    """Buffer cache usage for one database relation (table or index)."""

    schema_name: str
    relation_name: str
    relation_kind: str
    buffered_blocks: int
    buffered_bytes: int
    buffer_percent: float | None
    percent_of_relation_buffered: float | None
    average_usage_count: float | None
    dirty_pages: int


@dataclass(frozen=True, slots=True)
class BufferCacheRelationsReport:
    """Relations taking up space in the shared buffer cache."""

    available: bool
    relations: list[BufferCacheRelationRow]


_RELKIND_MAP = {
    "r": "table",
    "i": "index",
    "S": "sequence",
    "t": "toast table",
    "v": "view",
    "m": "materialized view",
    "c": "composite type",
    "f": "foreign table",
    "p": "partitioned table",
    "I": "partitioned index",
}


def _map_relkind(kind: str | None) -> str:
    if not kind:
        return "unknown"
    return _RELKIND_MAP.get(kind, f"unknown ({kind})")


async def read_pg_buffercache_summary(driver: SqlDriver) -> BufferCacheSummaryReport:
    """Get a high-level summary of the shared buffer cache state.

    Requires the ``pg_buffercache`` extension. Returns ``available=False`` if not installed.
    """
    from mcpg.extensions import extension_installed

    if not await extension_installed(driver, "pg_buffercache"):
        return BufferCacheSummaryReport(available=False)

    rows = await driver.execute_query(
        "SELECT count(*) AS total_buffers, "
        "       sum(case when relfilenode IS NULL then 1 else 0 end) AS free_buffers, "
        "       sum(case when relfilenode IS NOT NULL then 1 else 0 end) AS used_buffers, "
        "       sum(case when isdirty then 1 else 0 end) AS dirty_buffers, "
        "       avg(usagecount) AS average_usage_count "
        "FROM pg_buffercache",
        force_readonly=True,
    )
    if not rows:
        return BufferCacheSummaryReport(available=True)

    row = rows[0]
    return BufferCacheSummaryReport(
        available=True,
        total_buffers=_maybe_int(row.cells.get("total_buffers")),
        free_buffers=_maybe_int(row.cells.get("free_buffers")),
        used_buffers=_maybe_int(row.cells.get("used_buffers")),
        dirty_buffers=_maybe_int(row.cells.get("dirty_buffers")),
        average_usage_count=_maybe_float(row.cells.get("average_usage_count")),
    )


async def read_pg_buffercache_relations(
    driver: SqlDriver,
    *,
    schema: str | None = None,
    limit: int = 100,
) -> BufferCacheRelationsReport:
    """Get the list of relations taking up the most space in the shared buffer cache.

    Requires the ``pg_buffercache`` extension. Returns ``available=False`` if not installed.
    """
    from mcpg.extensions import extension_installed

    if not await extension_installed(driver, "pg_buffercache"):
        return BufferCacheRelationsReport(available=False, relations=[])

    query = (
        "SELECT n.nspname AS schema_name, "
        "       c.relname AS relation_name, "
        "       c.relkind AS relation_kind, "
        "       count(*) AS buffered_blocks, "
        "       count(*) * 8192 AS buffered_bytes, "
        "       round(100.0 * count(*) / (SELECT nullif(setting::integer, 0) "
        "                                 FROM pg_settings WHERE name='shared_buffers'), 2) AS buffer_percent, "
        "       round(100.0 * count(*) * 8192 / nullif(pg_relation_size(c.oid), 0), 2) "
        "         AS percent_of_relation_buffered, "
        "       avg(usagecount) AS average_usage_count, "
        "       sum(case when isdirty then 1 else 0 end) AS dirty_pages "
        "FROM pg_buffercache b "
        "JOIN pg_class c ON b.relfilenode = pg_relation_filenode(c.oid) "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_database d ON (b.reldatabase = d.oid AND d.datname = current_database()) "
    )

    params: list[object] = []
    if schema is not None:
        query += "WHERE n.nspname = %s "
        params.append(schema)

    query += "GROUP BY n.nspname, c.oid, c.relname, c.relkind ORDER BY count(*) DESC LIMIT %s"
    params.append(limit)

    rows = await driver.execute_query(
        query,
        params=params,
        force_readonly=True,
    )

    relations = [
        BufferCacheRelationRow(
            schema_name=str(row.cells["schema_name"]),
            relation_name=str(row.cells["relation_name"]),
            relation_kind=_map_relkind(str(row.cells.get("relation_kind"))),
            buffered_blocks=_maybe_int(row.cells.get("buffered_blocks")) or 0,
            buffered_bytes=_maybe_int(row.cells.get("buffered_bytes")) or 0,
            buffer_percent=_maybe_float(row.cells.get("buffer_percent")),
            percent_of_relation_buffered=_maybe_float(row.cells.get("percent_of_relation_buffered")),
            average_usage_count=_maybe_float(row.cells.get("average_usage_count")),
            dirty_pages=_maybe_int(row.cells.get("dirty_pages")) or 0,
        )
        for row in rows or []
    ]

    return BufferCacheRelationsReport(available=True, relations=relations)
