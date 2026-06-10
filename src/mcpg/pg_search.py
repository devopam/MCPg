"""pg_search integration: observability surface (phase BM-1).

`pg_search <https://github.com/paradedb/paradedb>`_ is a PostgreSQL
extension by ParadeDB that ships a Tantivy-backed BM25 index access
method (``USING bm25``) + a `pdb.*` schema of query-building and
projection helpers. This module covers the *observability* slice —
catalog enumeration and metadata fetch for every BM25 index in the
database. Subsequent phases will add search execution (BM-2), hybrid
composition (BM-3), DDL (BM-4), and advisor + audit (BM-5).

* :func:`list_pg_search_indexes`,
  :func:`get_pg_search_index_metadata` — observability.

Reads return cleanly (empty list / raise) when the extension is not
installed, so callers can treat absence as "no BM25 in use" rather
than a hard error.

**Upstream contract notes.** The 13 ``WITH (...)`` options surfaced
on :class:`PgSearchIndexInfo` are sourced verbatim from
`pg_search/src/api/index.rs` ``IndexOptions`` (BM-0 investigation
checkpoint, see ``docs/plans/bm25-integration.md`` §2.2). Six of
them (``text_fields``, ``numeric_fields``, ``boolean_fields``,
``json_fields``, ``range_fields``, ``datetime_fields``) plus
``search_tokenizer`` are JSONB-shaped; reloptions are stored as
``text[]`` so the raw values are parsed back into Python dicts.
The two ``int`` options (``target_segment_count``,
``mutable_segment_rows``) are coerced; the rest stay as strings.
:attr:`PgSearchIndexInfo.index_options` always carries the full
parsed reloptions dict for fidelity.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

# Plain unquoted PostgreSQL identifier — same rule as turboquant /
# vector_tuning. Anything that would require delimited quoting is
# refused rather than parsed out of an agent string.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class PgSearchError(Exception):
    """Raised when a pg_search operation cannot complete."""


def _validate_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise PgSearchError(f"invalid {kind} name: {name!r}")


# Per §2.2 of the BM-0 investigation checkpoint, the ``bm25`` AM
# accepts these 13 reloptions. Grouped here so the parser can route
# each value through the right coercion.
_JSONB_OPTIONS: frozenset[str] = frozenset(
    {
        "text_fields",
        "numeric_fields",
        "boolean_fields",
        "json_fields",
        "range_fields",
        "datetime_fields",
        "search_tokenizer",
    }
)
_INT_OPTIONS: frozenset[str] = frozenset({"target_segment_count", "mutable_segment_rows"})
# Remaining options (``key_field``, ``layer_sizes``,
# ``background_layer_sizes``, ``sort_by``) are plain text and pass
# through unchanged.


@dataclass(frozen=True, slots=True)
class PgSearchIndexInfo:
    """A BM25 index and the reloptions ``pg_class`` reports for it.

    Catalog-level fields (:attr:`schema`, :attr:`index`,
    :attr:`table`, :attr:`columns`) come from
    ``pg_index`` / ``pg_class`` / ``pg_attribute``; :attr:`columns`
    is the ordered list of attribute names the index covers (a BM25
    index can index one or many columns).

    The typed ``WITH (...)`` accessors below surface the 13 documented
    `bm25` options. JSONB-shaped options are parsed back from the
    text-array reloptions storage into Python dicts; integer options
    are coerced to int; text options pass through. Anything not on
    the documented list lives in :attr:`index_options` unchanged so
    future-added options stay reachable without a code change.
    """

    schema: str
    index: str
    table: str
    columns: list[str] = field(default_factory=list)
    # Documented bm25 WITH options, surfaced as typed accessors.
    key_field: str | None = None
    text_fields: dict[str, Any] = field(default_factory=dict)
    numeric_fields: dict[str, Any] = field(default_factory=dict)
    boolean_fields: dict[str, Any] = field(default_factory=dict)
    json_fields: dict[str, Any] = field(default_factory=dict)
    range_fields: dict[str, Any] = field(default_factory=dict)
    datetime_fields: dict[str, Any] = field(default_factory=dict)
    layer_sizes: str | None = None
    background_layer_sizes: str | None = None
    target_segment_count: int | None = None
    mutable_segment_rows: int | None = None
    sort_by: str | None = None
    search_tokenizer: dict[str, Any] = field(default_factory=dict)
    # Full parsed reloptions dict — always includes every key in the
    # raw text[], whether or not it's surfaced as a typed attribute.
    index_options: dict[str, Any] = field(default_factory=dict)


# --- SQL --------------------------------------------------------------------

# `array_agg(... ORDER BY ord)` against `unnest(indkey)` yields the
# attribute names in indexed-column order. A BM25 index can cover one
# or many columns, so we expose the full list rather than only
# indkey[0] (which is what TQ does, since turboquant indexes a single
# vector column by construction).
_LIST_INDEXES_SQL = """
SELECT
    n.nspname AS schema,
    i.relname AS index,
    t.relname AS table,
    (
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM unnest(ix.indkey::int[]) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
    ) AS columns,
    i.reloptions AS reloptions
FROM pg_index ix
JOIN pg_class i      ON i.oid = ix.indexrelid
JOIN pg_class t      ON t.oid = ix.indrelid
JOIN pg_namespace n  ON n.oid = i.relnamespace
JOIN pg_am am        ON am.oid = i.relam
WHERE am.amname = 'bm25'
ORDER BY n.nspname, i.relname
"""

_FETCH_ONE_INDEX_SQL = """
SELECT
    n.nspname AS schema,
    i.relname AS index,
    t.relname AS table,
    (
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM unnest(ix.indkey::int[]) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
    ) AS columns,
    i.reloptions AS reloptions
FROM pg_index ix
JOIN pg_class i      ON i.oid = ix.indexrelid
JOIN pg_class t      ON t.oid = ix.indrelid
JOIN pg_namespace n  ON n.oid = i.relnamespace
JOIN pg_am am        ON am.oid = i.relam
WHERE am.amname = 'bm25' AND n.nspname = %s AND i.relname = %s
"""


# --- helpers ---------------------------------------------------------------


def _as_json_dict(raw: str) -> dict[str, Any]:
    """Decode a JSONB-valued reloption into a dict.

    Reloptions are stored as ``text[]`` — JSONB values come back as
    their textual JSON serialization. Anything that fails to decode
    or doesn't decode to a dict yields ``{}`` so a misshapen option
    doesn't break the whole metadata read.
    """
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_reloptions(raw: Any) -> dict[str, Any]:
    """Parse a ``pg_class.reloptions`` text[] into a typed dict.

    PG stores reloptions as ``text[]`` of ``key=value`` strings. The
    13 documented bm25 options are routed through type-aware
    coercion (JSON → dict for the seven JSONB-shaped options, ``int``
    for the two integer options, plain string for the remaining
    four). Anything else passes through as its raw string so future
    upstream additions stay reachable via :attr:`index_options`.
    Malformed entries (no ``=``, empty key) are skipped silently.
    """
    if not isinstance(raw, list):
        return {}
    parsed: dict[str, Any] = {}
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, _, value = item.partition("=")
        if not key:
            continue
        if key in _JSONB_OPTIONS:
            parsed[key] = _as_json_dict(value)
        elif key in _INT_OPTIONS:
            coerced = _maybe_int(value)
            if coerced is not None:
                parsed[key] = coerced
        else:
            parsed[key] = value
    return parsed


def _columns_from_cell(cell: Any) -> list[str]:
    """Normalize the ``columns`` cell into a list of attribute names.

    psycopg returns PG ``text[]`` as a Python list. ``None`` (no rows
    matched in the indkey subquery, e.g. an expression-only index) is
    normalized to ``[]``. Anything else also falls back to ``[]``
    rather than raising — the upstream column list is informational,
    not a contract.
    """
    if isinstance(cell, list):
        return [str(c) for c in cell if c is not None]
    return []


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int | None:
    # Bools are an ``int`` subclass; reject them explicitly so a
    # parser glitch that yielded ``True`` doesn't masquerade as 1.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _index_info_from_row(row_cells: dict[str, Any]) -> PgSearchIndexInfo:
    options = _parse_reloptions(row_cells.get("reloptions"))
    return PgSearchIndexInfo(
        schema=row_cells["schema"],
        index=row_cells["index"],
        table=row_cells["table"],
        columns=_columns_from_cell(row_cells.get("columns")),
        key_field=_as_str(options.get("key_field")),
        text_fields=_as_dict(options.get("text_fields")),
        numeric_fields=_as_dict(options.get("numeric_fields")),
        boolean_fields=_as_dict(options.get("boolean_fields")),
        json_fields=_as_dict(options.get("json_fields")),
        range_fields=_as_dict(options.get("range_fields")),
        datetime_fields=_as_dict(options.get("datetime_fields")),
        layer_sizes=_as_str(options.get("layer_sizes")),
        background_layer_sizes=_as_str(options.get("background_layer_sizes")),
        target_segment_count=_as_int(options.get("target_segment_count")),
        mutable_segment_rows=_as_int(options.get("mutable_segment_rows")),
        sort_by=_as_str(options.get("sort_by")),
        search_tokenizer=_as_dict(options.get("search_tokenizer")),
        index_options=options,
    )


# --- public API ------------------------------------------------------------


async def list_pg_search_indexes(driver: SqlDriver) -> list[PgSearchIndexInfo]:
    """List every BM25 index plus its parsed reloptions.

    Returns an empty list when the ``pg_search`` extension is not
    installed.
    """
    if not await extension_installed(driver, "pg_search"):
        return []
    rows = await driver.execute_query(_LIST_INDEXES_SQL, force_readonly=True)
    return [_index_info_from_row(row.cells) for row in rows or []]


async def get_pg_search_index_metadata(driver: SqlDriver, schema: str, index: str) -> PgSearchIndexInfo:
    """Fetch the parsed reloptions for a single BM25 index.

    Identifier validation runs before any SQL is built so the schema
    / index strings cannot drive arbitrary catalog lookups.

    Raises:
        PgSearchError: extension is not installed, the schema / index
            name is not a plain identifier, or no BM25 index with that
            name exists.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(index, "index")
    if not await extension_installed(driver, "pg_search"):
        raise PgSearchError("pg_search extension is not installed in this database")
    rows = await driver.execute_query(_FETCH_ONE_INDEX_SQL, params=[schema, index], force_readonly=True)
    if not rows:
        raise PgSearchError(f"no BM25 index named {schema}.{index} found")
    return _index_info_from_row(rows[0].cells)
