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
