"""pg_turboquant read advisors.

`pg_turboquant <https://github.com/mayflower/pg_turboquant>`_ is a
PostgreSQL extension providing a custom ANN index access method
(``USING turboquant``) over pgvector ``vector`` / ``halfvec`` columns.
This module exposes the extension's read-only observability surface:

* :func:`list_turboquant_indexes` — every turboquant index in the
  database, joined with its ``tq_index_metadata`` payload.
* :func:`get_turboquant_index_metadata` — the metadata for one index.
* :func:`get_turboquant_heap_stats` — exact heap row count for one
  index.
* :func:`get_turboquant_last_scan_stats` — the backend-local JSON
  describing the most recent turboquant scan.

All four functions return cleanly (empty list / ``None``) when the
extension is not installed, so callers can treat absence as "no
turboquant in use" rather than a hard error.

**Upstream contract assumptions.** Upstream documents
``tq_last_scan_stats()`` as returning JSON. The other functions are
documented by the README only at the prose level
("reports algorithm version, quantizer family, …") — this module
treats them as returning JSON / JSONB as well, parses the documented
keys defensively (with ``.get()``), and preserves the raw payload in
:attr:`TurboQuantIndexInfo.raw_metadata` so any unanticipated fields
remain accessible to downstream advisors. The
``tq_recommended_query_knobs(...)`` advisor is **not** wrapped here:
its upstream signature is not documented at the field level yet, and
we'd rather skip a tool than ship one with a guessed signature. It is
expected to land in a follow-up once the signature is pinned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

# Plain unquoted PostgreSQL identifier — matches the rule used by
# vector_tuning. Anything that would require delimited quoting at the
# catalog level is refused rather than parsed out of an agent string.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class TurboQuantError(Exception):
    """Raised when a pg_turboquant operation cannot complete."""


def _quoted(name: str, kind: str) -> str:
    if not _IDENTIFIER.match(name):
        raise TurboQuantError(f"invalid {kind} name: {name!r}")
    return f'"{name}"'


@dataclass(frozen=True, slots=True)
class TurboQuantIndexInfo:
    """A turboquant index and the metadata `tq_index_metadata` reports for it.

    Documented keys are surfaced as typed fields; the full upstream
    payload is preserved in :attr:`raw_metadata` so callers can still
    reach unanticipated fields.
    """

    schema: str
    index: str
    table: str
    column: str
    algorithm_version: str | None
    quantizer_family: str | None
    residual_sketch_kind: str | None
    fast_path_eligible: bool | None
    capability_flags: list[str] = field(default_factory=list)
    delta_state: str | None = None
    maintenance_recommended: bool | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurboQuantHeapStats:
    """Exact heap row count for a turboquant index."""

    schema: str
    index: str
    row_count: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurboQuantLastScanStats:
    """The most recent scan's diagnostic JSON, parsed defensively.

    ``raw`` always holds the full upstream payload; the named fields
    are convenience extractions for the documented keys.
    """

    raw: dict[str, Any]
    score_mode: str | None
    simd_kernel: str | None
    pages_scanned: int | None
    pages_pruned: int | None


# --- SQL --------------------------------------------------------------------

# Joining catalog tables in one trip is cheaper than walking indexes and
# fetching metadata one-by-one, so we splice the regclass argument from
# the catalog row itself.
_LIST_INDEXES_SQL = """
SELECT
    n.nspname                                  AS schema,
    i.relname                                  AS index,
    t.relname                                  AS table,
    a.attname                                  AS column,
    tq_index_metadata(i.oid::regclass)::jsonb  AS metadata
FROM pg_index ix
JOIN pg_class i           ON i.oid = ix.indexrelid
JOIN pg_class t           ON t.oid = ix.indrelid
JOIN pg_namespace n       ON n.oid = i.relnamespace
JOIN pg_am am             ON am.oid = i.relam
LEFT JOIN pg_attribute a  ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
WHERE am.amname = 'turboquant'
ORDER BY n.nspname, i.relname
"""

_FETCH_ONE_INDEX_SQL = """
SELECT
    n.nspname                                  AS schema,
    i.relname                                  AS index,
    t.relname                                  AS table,
    a.attname                                  AS column,
    tq_index_metadata(i.oid::regclass)::jsonb  AS metadata
FROM pg_index ix
JOIN pg_class i           ON i.oid = ix.indexrelid
JOIN pg_class t           ON t.oid = ix.indrelid
JOIN pg_namespace n       ON n.oid = i.relnamespace
JOIN pg_am am             ON am.oid = i.relam
LEFT JOIN pg_attribute a  ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
WHERE am.amname = 'turboquant' AND n.nspname = %s AND i.relname = %s
"""

_HEAP_STATS_SQL = """
SELECT tq_index_heap_stats(format('%I.%I', %s, %s)::regclass)::jsonb AS stats
"""

_LAST_SCAN_SQL = "SELECT tq_last_scan_stats()::jsonb AS stats"


# --- helpers ---------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a JSONB-shaped value to a plain dict.

    psycopg returns JSONB as a parsed Python value; protect against
    drivers that hand back the raw text by being lenient here.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            import json

            decoded = json.loads(value)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _index_info_from_row(row_cells: dict[str, Any]) -> TurboQuantIndexInfo:
    metadata = _as_dict(row_cells.get("metadata"))
    return TurboQuantIndexInfo(
        schema=row_cells["schema"],
        index=row_cells["index"],
        table=row_cells["table"],
        column=row_cells.get("column") or "",
        algorithm_version=metadata.get("algorithm_version"),
        quantizer_family=metadata.get("quantizer_family"),
        residual_sketch_kind=metadata.get("residual_sketch_kind"),
        fast_path_eligible=metadata.get("fast_path_eligible"),
        capability_flags=_as_str_list(metadata.get("capability_flags")),
        delta_state=metadata.get("delta_state"),
        maintenance_recommended=metadata.get("maintenance_recommended"),
        raw_metadata=metadata,
    )


# --- public API ------------------------------------------------------------


async def list_turboquant_indexes(driver: SqlDriver) -> list[TurboQuantIndexInfo]:
    """List every turboquant index plus its `tq_index_metadata` payload.

    Returns an empty list when the extension is not installed.
    """
    if not await extension_installed(driver, "pg_turboquant"):
        return []
    rows = await driver.execute_query(_LIST_INDEXES_SQL, force_readonly=True)
    return [_index_info_from_row(row.cells) for row in rows or []]


async def get_turboquant_index_metadata(driver: SqlDriver, schema: str, index: str) -> TurboQuantIndexInfo:
    """Fetch the metadata payload for a single turboquant index.

    Identifier validation (``_IDENTIFIER``) runs before any SQL is built,
    so the schema / index strings cannot drive arbitrary catalog lookups.

    Raises:
        TurboQuantError: extension is not installed, the schema / index
            name is not a plain identifier, or no turboquant index with
            that name exists.
    """
    _quoted(schema, "schema")
    _quoted(index, "index")
    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")
    rows = await driver.execute_query(_FETCH_ONE_INDEX_SQL, params=[schema, index], force_readonly=True)
    if not rows:
        raise TurboQuantError(f"no turboquant index named {schema}.{index} found")
    return _index_info_from_row(rows[0].cells)


async def get_turboquant_heap_stats(driver: SqlDriver, schema: str, index: str) -> TurboQuantHeapStats:
    """Fetch the exact heap row count for a single turboquant index.

    Raises:
        TurboQuantError: extension is not installed or the identifier
            fails validation.
    """
    _quoted(schema, "schema")
    _quoted(index, "index")
    if not await extension_installed(driver, "pg_turboquant"):
        raise TurboQuantError("pg_turboquant extension is not installed in this database")
    rows = await driver.execute_query(_HEAP_STATS_SQL, params=[schema, index], force_readonly=True)
    if not rows:
        raise TurboQuantError(f"tq_index_heap_stats returned no row for {schema}.{index}")
    stats = _as_dict(rows[0].cells.get("stats"))
    row_count = stats.get("row_count")
    if row_count is None:
        # Some upstream versions report 'rows' instead — fall back rather
        # than fail when the alternate key is the only one present.
        row_count = stats.get("rows")
    return TurboQuantHeapStats(
        schema=schema,
        index=index,
        row_count=int(row_count) if row_count is not None else 0,
        raw=stats,
    )


async def get_turboquant_last_scan_stats(driver: SqlDriver) -> TurboQuantLastScanStats | None:
    """Return the backend-local diagnostic JSON for the most recent scan.

    Returns ``None`` when the extension is absent or no turboquant scan
    has run on this backend yet (upstream returns SQL ``NULL`` in that
    case).
    """
    if not await extension_installed(driver, "pg_turboquant"):
        return None
    rows = await driver.execute_query(_LAST_SCAN_SQL, force_readonly=True)
    if not rows:
        return None
    raw = _as_dict(rows[0].cells.get("stats"))
    if not raw:
        return None
    pages_scanned = raw.get("pages_scanned")
    pages_pruned = raw.get("pages_pruned")
    return TurboQuantLastScanStats(
        raw=raw,
        score_mode=raw.get("score_mode"),
        simd_kernel=raw.get("simd_kernel"),
        pages_scanned=int(pages_scanned) if pages_scanned is not None else None,
        pages_pruned=int(pages_pruned) if pages_pruned is not None else None,
    )
