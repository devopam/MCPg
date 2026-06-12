"""pg_search integration: full BM25 surface (phases BM-1 through BM-5).

`pg_search <https://github.com/paradedb/paradedb>`_ is a PostgreSQL
extension by ParadeDB that ships a Tantivy-backed BM25 index access
method (``USING bm25``) + a `pdb.*` schema of query-building and
projection helpers. This module covers the full extension surface —
observability, search execution, hybrid composition, DDL, and the
advisor + audit-scorecard category.

* :func:`list_pg_search_indexes`,
  :func:`get_pg_search_index_metadata` — observability (BM-1).
* :func:`pg_search_run`, :func:`pg_search_more_like_this`,
  :func:`pg_search_parse_query` — search execution (BM-2).
* :func:`hybrid_bm25_vector_search` — BM25 + pgvector RRF fusion
  (BM-3).
* :func:`create_pg_search_index`, :func:`reindex_pg_search_index`
  — DDL (BM-4).
* :func:`recommend_pg_search_maintenance`,
  :func:`audit_pg_search_indexes` — rule-table advisor + scorecard
  category adapter (BM-5).

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

The BM-2 surface is grounded in two upstream source files:

* `pg_search/src/bootstrap/` — ``pdb.score(anyelement) → float4``,
  ``pdb.parse(text, bool, bool) → pdb.query``,
  ``pdb.more_like_this(anyelement, jsonb, int4, int4, int4, int4,
  int4, int4, float4, text[]) → pdb.query``.
* `pg_search/src/postgres/customscan/basescan/projections/snippet.rs`
  (paradedb/paradedb@``8bb9a64``) — ``pdb.snippet(anyelement, text,
  text, int4, int4, int4) → text`` and ``pdb.snippets(anyelement,
  text, text, int4, int4, int4, text) → text[]``. The wrapper uses
  the multi-snippet form (``pdb.snippets``).

The snippet functions are pgrx ``#[pg_extern]`` stubs that only
produce highlights when invoked inside a ``pg_search``-driven SELECT
(the planner rewrites them during custom-scan projection). Callers
should only request ``return_snippets=True`` in conjunction with a
``@@@`` predicate, which is how :func:`pg_search_run` already wires
them up.
"""

from __future__ import annotations

import datetime as _datetime
import json
import math
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.database import Database
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


# --- BM-2: search execution ------------------------------------------------


def _pg_quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier the way ``format('%I')`` would.

    Inputs reaching this helper have already passed
    :func:`_validate_identifier`, so they're plain unquoted names.
    Wrapping in double quotes (with doubled internal quotes) keeps
    the rendered SQL legal even for caller-supplied names that
    happen to share a keyword.
    """
    return '"' + name.replace('"', '""') + '"'


def _validate_positive_int(name: str, value: int, *, allow_zero: bool = False) -> None:
    """Bounded integer validation that rejects the bool-as-int trap.

    ``bool`` is a subclass of ``int`` in Python; accepting ``True``
    as a positive int would let a caller bug slip through silently.
    """
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        op = ">=" if allow_zero else ">"
        raise PgSearchError(f"{name} must be an int {op} 0; got {value!r}")


def _validate_bool(value: bool, kind: str) -> None:
    if not isinstance(value, bool):
        raise PgSearchError(f"{kind} must be a bool; got {value!r}")


@dataclass(frozen=True, slots=True)
class PgSearchHit:
    """A single row returned by :func:`pg_search_run` or :func:`pg_search_more_like_this`.

    :attr:`id` is the value of the caller-supplied ``key_field``
    column (kept as ``Any`` so int / uuid / text primary keys all
    pass through). :attr:`score` is the BM25 score from
    ``pdb.score(t)``. :attr:`snippets` is empty unless the caller
    requested ``return_snippets=True``; when populated, it carries
    the multi-snippet array ``pdb.snippets`` produces for the
    designated ``snippet_field``.
    """

    id: Any
    score: float
    snippets: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PgSearchParsedQuery:
    """The string form of a parsed ``pdb.query``.

    ``pdb.parse`` returns the opaque ``pdb.query`` type; casting to
    ``text`` is the canonical way to inspect what the parser made of
    a query string. Useful for debugging unexpected hit counts (e.g.
    "did the parser interpret my phrase as a phrase or as two
    independent terms?").
    """

    parsed: str


# Snippet-projection defaults match the pgrx source exactly. Defining
# them in one place keeps the SQL builder and the public function
# signature in lockstep, and surfaces them as constants for callers
# building wrappers on top.
_SNIPPET_DEFAULT_START_TAG = "<b>"
_SNIPPET_DEFAULT_END_TAG = "</b>"
_SNIPPET_DEFAULT_MAX_NUM_CHARS = 150
_SNIPPET_DEFAULT_SORT_BY = "score"

# Safety bounds for limit args. The upstream functions accept any
# int4, but exposing unbounded LIMIT through MCPg makes accidental
# server-side OOMs trivial. 10_000 mirrors the cap used elsewhere in
# the codebase for paginated reads.
_LIMIT_MIN, _LIMIT_MAX = 1, 10_000


def _validate_limit(value: int) -> None:
    """``limit`` must be a sane positive int within MCPg's pagination cap."""
    if not isinstance(value, int) or isinstance(value, bool) or not _LIMIT_MIN <= value <= _LIMIT_MAX:
        raise PgSearchError(f"limit must be an int in [{_LIMIT_MIN}..{_LIMIT_MAX}]; got {value!r}")


def _validate_columns_arg(columns: list[str] | None) -> str | None:
    """Validate the ``columns`` arg and pick the single search target.

    BM-2 supports two search shapes:

    * ``columns=None`` → search the whole BM25 index
      (``WHERE alias @@@ query``). This is the default; the index can
      cover one or many columns.
    * ``columns=[one_col]`` → restrict the search to a single field
      (``WHERE alias.col @@@ query``).

    Multi-column search requires the ``pdb.parse`` per-field config
    JSON which is deferred to a follow-up phase (the eight
    ``pdb.more_like_this`` tuning args are deferred for the same
    reason — see ``docs/plans/bm25-integration.md`` §6). Passing
    more than one column raises rather than silently picking the
    first.

    Returns the chosen single-column identifier (already validated),
    or ``None`` for the whole-table form.
    """
    if columns is None:
        return None
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise PgSearchError(f"columns must be list[str] or None; got {columns!r}")
    if len(columns) == 0:
        raise PgSearchError("columns must be None or a non-empty list; pass None to search the whole index")
    if len(columns) > 1:
        raise PgSearchError(
            "multi-column search is not yet supported in BM-2 — needs the pdb.parse per-field "
            "config JSON, deferred to a follow-up phase. Pass a single column or None."
        )
    column = columns[0]
    _validate_identifier(column, "column")
    return column


# Table alias used in every BM-2 SQL builder. Pulled out so the
# `pdb.score(t)` and `t @@@ ...` references stay in sync with the
# `FROM` clause without copy-paste drift.
_TABLE_ALIAS = "t"


def _bm25_predicate(column: str | None) -> str:
    """Render the search-target side of the ``@@@`` predicate."""
    return f"{_TABLE_ALIAS}.{_pg_quote_ident(column)}" if column else _TABLE_ALIAS


async def pg_search_run(
    driver: SqlDriver,
    schema: str,
    table: str,
    query: str,
    key_field: str,
    *,
    columns: list[str] | None = None,
    limit: int,
    return_snippets: bool = False,
    snippet_field: str | None = None,
    snippet_start_tag: str = _SNIPPET_DEFAULT_START_TAG,
    snippet_end_tag: str = _SNIPPET_DEFAULT_END_TAG,
    snippet_max_num_chars: int = _SNIPPET_DEFAULT_MAX_NUM_CHARS,
) -> list[PgSearchHit]:
    """Run a BM25 keyword search against the named table.

    Builds and executes::

        SELECT t."<key_field>", pdb.score(t) AS score [, pdb.snippets(...)]
        FROM "<schema>"."<table>" AS t
        WHERE <search_target> @@@ <query>
        ORDER BY pdb.score(t) DESC
        LIMIT <limit>

    where ``<search_target>`` is ``t`` (whole index) or
    ``t."<col>"`` (single-column form), per the ``columns`` arg's
    validation in :func:`_validate_columns_arg`.

    The ``key_field`` arg is the caller's primary-key column (matches
    the ``key_field`` reloption set on the BM25 index — discoverable
    via :func:`list_pg_search_indexes`). It is returned as
    :attr:`PgSearchHit.id` and is the only projection MCPg surfaces
    by default; richer projections are a follow-up phase.

    When ``return_snippets=True``, ``snippet_field`` is required and
    must name a single ``text`` column the BM25 index covers. The
    wrapper projects ``pdb.snippets(t."<snippet_field>", start_tag,
    end_tag, max_num_chars, NULL, NULL, 'score') → text[]`` and
    surfaces the array as :attr:`PgSearchHit.snippets`.

    Raises:
        PgSearchError: extension missing, identifier validation
            fails, limit out of bounds, multi-column search
            requested, or ``return_snippets=True`` without a
            ``snippet_field``.

    Security:
        ``snippet_start_tag`` / ``snippet_end_tag`` are bound as
        PostgreSQL string literals via ``%s`` parameters — there is
        no SQL-injection surface. The defaults match upstream's
        (``<b>`` / ``</b>``) for HTML rendering, and most callers
        will leave them at the defaults or set them to other
        rendering-context tags. If a caller forwards
        attacker-controlled values and a downstream consumer
        renders the returned :attr:`PgSearchHit.snippets` as HTML
        without escaping the surrounding match text, those tags
        become a cross-site scripting (XSS) vector. **MCPg returns
        the raw ``text[]`` from PostgreSQL; output escaping is the
        renderer's responsibility.** Treat the snippet strings as
        untrusted at the rendering layer.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(key_field, "key_field")
    _validate_bool(return_snippets, "return_snippets")
    _validate_limit(limit)
    if not isinstance(query, str):
        raise PgSearchError(f"query must be str; got {type(query).__name__}")
    column = _validate_columns_arg(columns)

    snippet_field_validated: str | None = None
    if return_snippets:
        if snippet_field is None:
            raise PgSearchError("return_snippets=True requires a snippet_field — the text column to highlight")
        _validate_identifier(snippet_field, "snippet_field")
        if not isinstance(snippet_start_tag, str) or not isinstance(snippet_end_tag, str):
            raise PgSearchError("snippet_start_tag / snippet_end_tag must be str")
        _validate_positive_int("snippet_max_num_chars", snippet_max_num_chars)
        snippet_field_validated = snippet_field
    elif snippet_field is not None:
        raise PgSearchError("snippet_field is only valid when return_snippets=True")

    if not await extension_installed(driver, "pg_search"):
        raise PgSearchError("pg_search extension is not installed in this database")

    qualified_table = f"{_pg_quote_ident(schema)}.{_pg_quote_ident(table)}"
    search_target = _bm25_predicate(column)

    # Bind params: query, [snippet args if any], limit. Snippet bind
    # order follows pdb.snippets' positional arg list (start_tag,
    # end_tag, max_num_chars). The NULL/NULL/sort_by tail uses
    # literals — no caller input flows there.
    select_parts = [
        f"{_TABLE_ALIAS}.{_pg_quote_ident(key_field)} AS id",
        f"pdb.score({_TABLE_ALIAS}) AS score",
    ]
    # Build params in placeholder order. Snippet placeholders appear
    # in the SELECT projection (rendered first), so their bind values
    # must come before `query` (WHERE) and `limit` (LIMIT).
    params: list[Any] = []
    if snippet_field_validated is not None:
        select_parts.append(
            f"pdb.snippets({_TABLE_ALIAS}.{_pg_quote_ident(snippet_field_validated)}, "
            f"%s, %s, %s, NULL, NULL, '{_SNIPPET_DEFAULT_SORT_BY}') AS snippets"
        )
        params.extend([snippet_start_tag, snippet_end_tag, snippet_max_num_chars])
    params.extend([query, limit])

    sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {qualified_table} AS {_TABLE_ALIAS} "
        f"WHERE {search_target} @@@ %s "
        f"ORDER BY pdb.score({_TABLE_ALIAS}) DESC "
        f"LIMIT %s"
    )
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    return [
        PgSearchHit(
            id=row.cells["id"],
            score=float(row.cells["score"]),
            snippets=_normalize_snippets(row.cells.get("snippets")),
        )
        for row in rows or []
    ]


def _normalize_snippets(value: Any) -> list[str]:
    """Coerce ``pdb.snippets``' return to ``list[str]``.

    psycopg returns PG ``text[]`` as a Python list; drivers that
    hand back ``None`` (no snippets generated for this row) or any
    other shape fall through to ``[]`` rather than raising — the
    snippet projection is best-effort, not a hit-correctness signal.
    """
    if isinstance(value, list):
        return [str(s) for s in value if s is not None]
    return []


# Bounds for pdb.more_like_this int4 tuning args. int4 ranges are
# [0, 2^31-1] but realistic doc/term frequency thresholds and word
# lengths sit well below that — keeping the cap at 1_000_000_000
# matches the spirit of pg_search_run's _LIMIT_MAX, just bumped to
# accommodate corpus-scale int signals.
_MLT_INT_MIN, _MLT_INT_MAX = 0, 1_000_000_000


def _validate_mlt_int(name: str, value: int) -> None:
    """``min/max_doc_frequency``, ``min/max_term_frequency``,
    ``max_query_terms``, ``min/max_word_length`` validation.

    Bool-as-int rejected explicitly (a stray True would coerce to 1
    silently and skew the upstream MLT computation)."""
    if not isinstance(value, int) or isinstance(value, bool) or not _MLT_INT_MIN <= value <= _MLT_INT_MAX:
        raise PgSearchError(f"{name} must be an int in [{_MLT_INT_MIN}..{_MLT_INT_MAX}]; got {value!r}")


def _validate_mlt_boost(value: float) -> None:
    """``boost_factor`` is float4 — accept any finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PgSearchError(f"boost_factor must be a finite number; got {value!r}")
    fval = float(value)
    if math.isnan(fval) or math.isinf(fval):
        raise PgSearchError(f"boost_factor must be finite; got {value!r}")


def _validate_mlt_stop_words(value: list[str]) -> None:
    """``stop_words`` is text[] — list[str] mapped to PG ``text[]``."""
    if not isinstance(value, list) or not all(isinstance(w, str) for w in value):
        raise PgSearchError(f"stop_words must be list[str]; got {value!r}")


def _validate_mlt_fields(value: dict[str, Any]) -> None:
    """``fields`` is jsonb — dict serialized to JSON for binding."""
    if not isinstance(value, dict):
        raise PgSearchError(f"fields must be a dict (serialized to JSONB); got {type(value).__name__}")


async def pg_search_more_like_this(
    driver: SqlDriver,
    schema: str,
    table: str,
    document_id: Any,
    key_field: str,
    *,
    limit: int,
    fields: dict[str, Any] | None = None,
    min_doc_frequency: int | None = None,
    max_doc_frequency: int | None = None,
    min_term_frequency: int | None = None,
    max_query_terms: int | None = None,
    min_word_length: int | None = None,
    max_word_length: int | None = None,
    boost_factor: float | None = None,
    stop_words: list[str] | None = None,
) -> list[PgSearchHit]:
    """Find rows similar to the row identified by ``document_id``.

    Wraps ``pdb.more_like_this(anyelement, fields jsonb,
    min_doc_frequency int4, max_doc_frequency int4,
    min_term_frequency int4, max_query_terms int4,
    min_word_length int4, max_word_length int4, boost_factor float4,
    stop_words text[]) → pdb.query`` (verbatim signature from
    `docs/plans/bm25-integration.md` §2.1). The seed row is loaded
    from the same table via a correlated subquery so the wrapper
    takes ``document_id`` as an opaque key value rather than a row
    tuple.

    Every tuning arg is optional. When omitted, the wrapper does not
    mention it in the SQL — upstream's defaults apply. Args that
    *are* supplied use named-arg syntax (``name => %s``) so the bind
    list stays minimal and each value goes through a parameter
    rather than getting spliced into SQL.

    Built SQL (no tuning args)::

        SELECT t."<key>", pdb.score(t) AS score
        FROM "<schema>"."<table>" AS t
        WHERE t @@@ (
            SELECT pdb.more_like_this(seed)
            FROM "<schema>"."<table>" AS seed
            WHERE seed."<key>" = <document_id>
        )
        ORDER BY pdb.score(t) DESC
        LIMIT <limit>

    With tuning args, the inner ``pdb.more_like_this(seed)`` call
    grows extra ``, name => %s`` pairs in caller order.

    Raises:
        PgSearchError: extension missing, identifier validation
            fails, limit out of bounds, or any tuning-arg type /
            bounds check fails.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(key_field, "key_field")
    _validate_limit(limit)

    # Build the tuning-arg pieces. Order matters for the final params
    # list: we append (name, bound_value) tuples to mlt_args so the
    # SQL fragment renders in the same order as the bind list.
    mlt_args: list[tuple[str, Any, str]] = []  # (name, value, type_cast_suffix)
    if fields is not None:
        _validate_mlt_fields(fields)
        mlt_args.append(("fields", json.dumps(fields, sort_keys=True), "::jsonb"))
    for arg_name, arg_value in (
        ("min_doc_frequency", min_doc_frequency),
        ("max_doc_frequency", max_doc_frequency),
        ("min_term_frequency", min_term_frequency),
        ("max_query_terms", max_query_terms),
        ("min_word_length", min_word_length),
        ("max_word_length", max_word_length),
    ):
        if arg_value is not None:
            _validate_mlt_int(arg_name, arg_value)
            mlt_args.append((arg_name, arg_value, ""))
    if boost_factor is not None:
        _validate_mlt_boost(boost_factor)
        mlt_args.append(("boost_factor", float(boost_factor), "::real"))
    if stop_words is not None:
        _validate_mlt_stop_words(stop_words)
        mlt_args.append(("stop_words", stop_words, "::text[]"))

    if not await extension_installed(driver, "pg_search"):
        raise PgSearchError("pg_search extension is not installed in this database")

    qualified_table = f"{_pg_quote_ident(schema)}.{_pg_quote_ident(table)}"
    quoted_key = _pg_quote_ident(key_field)

    mlt_named_args = "".join(f", {name} => %s{cast}" for name, _, cast in mlt_args)
    mlt_param_values = [value for _, value, _ in mlt_args]

    sql = (
        f"SELECT {_TABLE_ALIAS}.{quoted_key} AS id, pdb.score({_TABLE_ALIAS}) AS score "
        f"FROM {qualified_table} AS {_TABLE_ALIAS} "
        f"WHERE {_TABLE_ALIAS} @@@ ("
        f"SELECT pdb.more_like_this(seed{mlt_named_args}) "
        f"FROM {qualified_table} AS seed "
        f"WHERE seed.{quoted_key} = %s"
        f") "
        f"ORDER BY pdb.score({_TABLE_ALIAS}) DESC "
        f"LIMIT %s"
    )
    # Bind order matches placeholder order: tuning args appear inside
    # the inner SELECT (rendered first), then the seed key in the
    # inner WHERE, then the outer LIMIT.
    params: list[Any] = [*mlt_param_values, document_id, limit]
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    return [PgSearchHit(id=row.cells["id"], score=float(row.cells["score"])) for row in rows or []]


async def pg_search_parse_query(
    driver: SqlDriver,
    query_string: str,
    *,
    lenient: bool = False,
    conjunction_mode: bool = False,
) -> PgSearchParsedQuery:
    """Surface the canonical text form of a parsed ``pdb.query`` for debugging.

    Calls ``pdb.parse(query_string, lenient, conjunction_mode)`` and
    casts the resulting opaque ``pdb.query`` value to ``text``. Useful
    for diagnosing surprising hit counts — e.g. "did the parser
    interpret my phrase as a phrase or as two independent terms?".

    Note ``conjunction_mode`` exists in the upstream signature even
    though the v2 blog only documented the first two args (BM-0 §2.1
    finding). When ``True``, the parser treats space-separated terms
    as AND-joined rather than OR-joined.

    Raises:
        PgSearchError: extension missing, or ``query_string`` is not
            a string.
    """
    if not isinstance(query_string, str):
        raise PgSearchError(f"query_string must be str; got {type(query_string).__name__}")
    _validate_bool(lenient, "lenient")
    _validate_bool(conjunction_mode, "conjunction_mode")

    if not await extension_installed(driver, "pg_search"):
        raise PgSearchError("pg_search extension is not installed in this database")

    rows = await driver.execute_query(
        "SELECT pdb.parse(%s, %s, %s)::text AS parsed",
        params=[query_string, lenient, conjunction_mode],
        force_readonly=True,
    )
    if not rows:
        # Defensive — pdb.parse always returns a row, but a fake
        # driver under test might not. Returning an empty parsed
        # form is more useful than raising.
        return PgSearchParsedQuery(parsed="")
    parsed = rows[0].cells.get("parsed")
    return PgSearchParsedQuery(parsed=str(parsed) if parsed is not None else "")


# --- BM-3: hybrid BM25 + pgvector search -----------------------------------
#
# Arithmetic grounded in two upstream sources (BM-0 §2.5 follow-up,
# 2026-06-10):
#
# * Blog: "Hybrid Search in PostgreSQL: The Missing Manual"
#   (2025-10-22, paradedb.com) — canonical narrative.
# * `tests/tests/documentation.rs::hybrid_search` — source-of-truth
#   test that the deprecated docs page used to embed.
#
# Both agree on Reciprocal Rank Fusion (RRF) — `sum(1.0 / (k + rank))`
# over per-source ranks, no min/max normalization of raw scores. There
# is **no** `paradedb.score_hybrid` / `paradedb.rank_hybrid` helper in
# v2; operators write the CTE inline. The blog form (UNION ALL +
# GROUP BY SUM) is simpler than the test's FULL OUTER JOIN form; the
# arithmetic is identical. MCPg ships the UNION ALL form.


# pgvector distance-operator allowlist. RRF arithmetic operates on
# ranks (not raw scores), so all three operators yield well-formed
# hybrid scores without any per-operator normalization. The choice
# only changes which vectors come back as the top-K from the vector
# leg; the fusion math is operator-agnostic.
_PGVECTOR_DISTANCE_OPS: frozenset[str] = frozenset({"<=>", "<->", "<#>"})


@dataclass(frozen=True, slots=True)
class HybridHit:
    """A single row returned by :func:`hybrid_bm25_vector_search`.

    :attr:`id` is the caller-supplied ``key_field`` value. :attr:`score`
    is the summed RRF score across both legs. :attr:`bm25_rank` and
    :attr:`vector_rank` carry the row's per-leg ranks for transparency
    — either can be ``None`` if the row only appeared in one leg's
    top-K (RRF naturally extends partial-coverage rows by treating
    the absent leg's contribution as zero).
    """

    id: Any
    score: float
    bm25_rank: int | None = None
    vector_rank: int | None = None


# RRF defaults — both upstream sources (blog + documentation.rs)
# converge on these literals. Exposed publicly so the FastMCP tool
# wrapper can keep its signature in sync with the API without
# re-typing the literals.
RRF_DEFAULT_K = 60
HYBRID_DEFAULT_WEIGHT = 1.0
HYBRID_DEFAULT_PER_LEG_LIMIT = 20
HYBRID_DEFAULT_DISTANCE_OP = "<=>"


def _validate_weight(name: str, value: float) -> None:
    """Weights must be finite non-negative reals.

    Allowing negative weights would invert one leg's ranking — almost
    certainly a caller bug. NaN / inf would corrupt the SUM aggregation
    silently. The bool-as-int / bool-as-float trap is caught by
    explicitly rejecting bools before the numeric check.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PgSearchError(f"{name} must be a non-negative finite number; got {value!r}")
    fval = float(value)
    if math.isnan(fval) or math.isinf(fval) or fval < 0:
        raise PgSearchError(f"{name} must be a non-negative finite number; got {value!r}")


def _validate_distance_op(op: str) -> None:
    if op not in _PGVECTOR_DISTANCE_OPS:
        expected = ", ".join(sorted(_PGVECTOR_DISTANCE_OPS))
        raise PgSearchError(f"distance_op must be one of {{{expected}}}; got {op!r}")


def _format_vector_literal(query_vector: list[float] | str) -> str:
    """Serialize a Python list to pgvector text format ``[v1,v2,...]``.

    Pre-formatted strings pass through. Mirrors the same shape the
    turboquant query wrappers accept.
    """
    if isinstance(query_vector, str):
        return query_vector
    if not isinstance(query_vector, list):
        raise PgSearchError(f"query_vector must be list[float] or str; got {type(query_vector).__name__}")
    return "[" + ",".join(str(float(v)) for v in query_vector) + "]"


async def hybrid_bm25_vector_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    query_text: str,
    query_vector: list[float] | str,
    key_field: str,
    vector_column: str,
    bm25_columns: list[str] | None = None,
    distance_op: str = HYBRID_DEFAULT_DISTANCE_OP,
    k: int = RRF_DEFAULT_K,
    bm25_weight: float = HYBRID_DEFAULT_WEIGHT,
    vector_weight: float = HYBRID_DEFAULT_WEIGHT,
    per_leg_limit: int = HYBRID_DEFAULT_PER_LEG_LIMIT,
    final_limit: int,
) -> list[HybridHit]:
    """Combine a BM25 search and a pgvector search via Reciprocal Rank Fusion.

    Builds and executes the canonical v2 hybrid pattern documented
    by ParadeDB in the 2025-10-22 "Hybrid Search Missing Manual"
    blog post and pinned in
    ``tests/tests/documentation.rs::hybrid_search``::

        WITH
        bm25_leg AS (
            SELECT t."<key>",
                   ROW_NUMBER() OVER (ORDER BY pdb.score(t) DESC) AS rank
            FROM "<schema>"."<table>" AS t
            WHERE <search_target> @@@ %s
            ORDER BY pdb.score(t) DESC
            LIMIT %s
        ),
        vector_leg AS (
            SELECT t."<key>",
                   ROW_NUMBER() OVER (ORDER BY t."<vec>" <op> %s::vector) AS rank
            FROM "<schema>"."<table>" AS t
            ORDER BY t."<vec>" <op> %s::vector
            LIMIT %s
        ),
        fused AS (
            SELECT id,
                   <bm25_weight> * 1.0 / (<k> + rank) AS score,
                   rank AS bm25_rank,
                   NULL::int AS vector_rank
            FROM bm25_leg
            UNION ALL
            SELECT id,
                   <vector_weight> * 1.0 / (<k> + rank) AS score,
                   NULL::int AS bm25_rank,
                   rank AS vector_rank
            FROM vector_leg
        )
        SELECT id,
               SUM(score) AS score,
               MAX(bm25_rank) AS bm25_rank,
               MAX(vector_rank) AS vector_rank
        FROM fused
        GROUP BY id
        ORDER BY score DESC
        LIMIT %s

    RRF is operator-agnostic: ``distance_op`` (``<=>`` cosine,
    ``<->`` L2, ``<#>`` negative inner product) only affects which
    vectors land in the vector leg's top-K, not the fusion arithmetic.
    Defaults match upstream's demonstrated form (cosine,
    ``k = 60``, equal weights, ``per_leg_limit = 20``).

    :data:`bm25_columns` mirrors :func:`pg_search_run`'s
    ``columns`` semantics: ``None`` searches the whole BM25 index;
    ``[one_col]`` restricts the BM25 leg to a single field;
    multi-column raises. The vector leg always uses the
    caller-supplied ``vector_column``.

    Composes naturally with the RAG efficiency suite — once the
    operator picks knobs for the vector leg via
    ``analyze_vector_search_efficiency``, the same knobs feed this
    wrapper's :data:`distance_op` and :data:`per_leg_limit`.

    Raises:
        PgSearchError: extension missing, identifier validation
            fails, multi-column BM25 search requested, invalid
            distance_op, non-finite weight, or any limit/k out of
            bounds.

    References:
        * https://www.paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual
          (retrieved 2026-06-10)
        * paradedb/paradedb@main ``tests/tests/documentation.rs``
          function ``hybrid_search``.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(key_field, "key_field")
    _validate_identifier(vector_column, "vector_column")
    _validate_distance_op(distance_op)
    if not isinstance(query_text, str):
        raise PgSearchError(f"query_text must be str; got {type(query_text).__name__}")
    bm25_column = _validate_columns_arg(bm25_columns)
    _validate_positive_int("k", k)
    _validate_weight("bm25_weight", bm25_weight)
    _validate_weight("vector_weight", vector_weight)
    _validate_limit(per_leg_limit)
    _validate_limit(final_limit)
    vector_literal = _format_vector_literal(query_vector)

    if not await extension_installed(driver, "pg_search"):
        raise PgSearchError("pg_search extension is not installed in this database")
    # The vector leg renders `<op> %s::vector`, which only resolves
    # when pgvector is installed. Fail fast with a clear message so
    # callers don't see a confusing PostgreSQL "type 'vector' does
    # not exist" error.
    if not await extension_installed(driver, "vector"):
        raise PgSearchError(
            "pgvector extension is not installed in this database — "
            "hybrid_bm25_vector_search needs both pg_search and pgvector"
        )

    qualified_table = f"{_pg_quote_ident(schema)}.{_pg_quote_ident(table)}"
    quoted_key = _pg_quote_ident(key_field)
    quoted_vec = _pg_quote_ident(vector_column)
    bm25_target = f"{_TABLE_ALIAS}.{_pg_quote_ident(bm25_column)}" if bm25_column else _TABLE_ALIAS

    # k, weights, and per-leg limits are validated numerics, so
    # rendering them as literals is safe and keeps the bind list to
    # just the per-call values (query_text, vector x 2, final_limit).
    sql = (
        f"WITH "
        f"bm25_leg AS ("
        f" SELECT {_TABLE_ALIAS}.{quoted_key} AS id,"
        f" ROW_NUMBER() OVER (ORDER BY pdb.score({_TABLE_ALIAS}) DESC) AS rank"
        f" FROM {qualified_table} AS {_TABLE_ALIAS}"
        f" WHERE {bm25_target} @@@ %s"
        f" ORDER BY pdb.score({_TABLE_ALIAS}) DESC"
        f" LIMIT {per_leg_limit}"
        f"),"
        f" vector_leg AS ("
        f" SELECT {_TABLE_ALIAS}.{quoted_key} AS id,"
        f" ROW_NUMBER() OVER (ORDER BY {_TABLE_ALIAS}.{quoted_vec} {distance_op} %s::vector) AS rank"
        f" FROM {qualified_table} AS {_TABLE_ALIAS}"
        f" ORDER BY {_TABLE_ALIAS}.{quoted_vec} {distance_op} %s::vector"
        f" LIMIT {per_leg_limit}"
        f"),"
        f" fused AS ("
        f" SELECT id,"
        f" {bm25_weight} * 1.0 / ({k} + rank) AS score,"
        f" rank AS bm25_rank,"
        f" NULL::int AS vector_rank"
        f" FROM bm25_leg"
        f" UNION ALL"
        f" SELECT id,"
        f" {vector_weight} * 1.0 / ({k} + rank) AS score,"
        f" NULL::int AS bm25_rank,"
        f" rank AS vector_rank"
        f" FROM vector_leg"
        f") "
        f"SELECT id, SUM(score) AS score,"
        f" MAX(bm25_rank) AS bm25_rank,"
        f" MAX(vector_rank) AS vector_rank "
        f"FROM fused "
        f"GROUP BY id "
        f"ORDER BY score DESC "
        f"LIMIT %s"
    )
    params: list[Any] = [query_text, vector_literal, vector_literal, final_limit]
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    return [
        HybridHit(
            id=row.cells["id"],
            score=float(row.cells["score"]),
            bm25_rank=_maybe_int(row.cells.get("bm25_rank")),
            vector_rank=_maybe_int(row.cells.get("vector_rank")),
        )
        for row in rows or []
    ]


# --- BM-4: DDL --------------------------------------------------------------
#
# All 13 reloptions surfaced on PgSearchIndexInfo are settable here. The
# 7 JSONB-shaped options (text_fields, numeric_fields, boolean_fields,
# json_fields, range_fields, datetime_fields, search_tokenizer) accept
# Python dicts and serialize via json.dumps. The 2 int options
# (target_segment_count, mutable_segment_rows) and 3 text options
# (layer_sizes, background_layer_sizes, sort_by) pass through with type
# validation. key_field is required.

# Option-type routing for the WITH clause builder. Aligned with the BM-1
# parser's groupings so a future addition only touches one place.
_BM25_TEXT_OPTIONS: tuple[str, ...] = (
    "key_field",
    "layer_sizes",
    "background_layer_sizes",
    "sort_by",
)
_BM25_INT_OPTIONS_DDL: tuple[str, ...] = (
    "target_segment_count",
    "mutable_segment_rows",
)
_BM25_JSONB_OPTIONS_DDL: tuple[str, ...] = (
    "text_fields",
    "numeric_fields",
    "boolean_fields",
    "json_fields",
    "range_fields",
    "datetime_fields",
    "search_tokenizer",
)

# Safety bounds — guards, not claims about what upstream accepts.
# Mirror turboquant's stance: bound the surface so a single typo
# doesn't push a billion-row int into PG without warning.
_TARGET_SEGMENT_COUNT_MIN, _TARGET_SEGMENT_COUNT_MAX = 1, 100_000
_MUTABLE_SEGMENT_ROWS_MIN, _MUTABLE_SEGMENT_ROWS_MAX = 1, 100_000_000


@dataclass(frozen=True, slots=True)
class CreatePgSearchIndexResult:
    """Outcome of a :func:`create_pg_search_index` call.

    The rendered ``create_sql`` is preserved verbatim for auditability
    — every identifier in it has already passed through
    :func:`_pg_quote_ident`, every JSONB value through
    :func:`json.dumps` + PG literal escaping, and every int / text
    value through its own bounds or type check.
    """

    schema: str
    table: str
    columns: list[str]
    index_name: str
    key_field: str
    options: dict[str, Any]
    concurrently: bool
    create_sql: str
    started_at: str
    completed_at: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ReindexPgSearchResult:
    """Outcome of a :func:`reindex_pg_search_index` call."""

    schema: str
    index: str
    concurrently: bool
    reindex_sql: str
    started_at: str
    completed_at: str
    duration_seconds: float


def _validate_int_option_bounded(name: str, value: int, lo: int, hi: int) -> None:
    """Validate a bounded int reloption. Bool-as-int rejected explicitly."""
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise PgSearchError(f"{name} must be an int in [{lo}..{hi}]; got {value!r}")


def _validate_text_option(name: str, value: str) -> None:
    """Validate a text reloption is a non-empty string.

    Layer-size strings (``layer_sizes`` / ``background_layer_sizes``)
    expect comma-separated ints per upstream's grammar, but we let
    upstream reject malformed shapes with its own (informative)
    error message. We only enforce ``str`` here.
    """
    if not isinstance(value, str) or not value:
        raise PgSearchError(f"{name} must be a non-empty str; got {value!r}")


def _validate_jsonb_option(name: str, value: dict[str, Any]) -> None:
    """JSONB reloption must be a dict; the contents are upstream's contract."""
    if not isinstance(value, dict):
        raise PgSearchError(f"{name} must be a dict (serialized to JSONB); got {type(value).__name__}")


def _pg_quote_literal(text: str) -> str:
    """Quote a PostgreSQL string literal the way ``format('%L')`` would."""
    return "'" + text.replace("'", "''") + "'"


def _render_option(name: str, value: Any) -> str:
    """Render a single ``key = value`` pair for the WITH clause.

    * Text options → PG single-quote-escaped literal.
    * Int options → bare digit literal (already type-checked, can't
      escape SQL).
    * JSONB options → ``json.dumps`` + single-quote-escaped literal.
    """
    if name in _BM25_TEXT_OPTIONS:
        return f"{name} = {_pg_quote_literal(value)}"
    if name in _BM25_INT_OPTIONS_DDL:
        return f"{name} = {int(value)}"
    if name in _BM25_JSONB_OPTIONS_DDL:
        return f"{name} = {_pg_quote_literal(json.dumps(value, sort_keys=True))}"
    raise PgSearchError(f"unknown reloption {name!r} reached the renderer")  # pragma: no cover


def _utc_iso_now() -> str:
    return _datetime.datetime.now(_datetime.UTC).isoformat().replace("+00:00", "Z")


# Pre-flight: confirm the named index actually uses the bm25 access
# method before issuing REINDEX. Without this check, the call could
# probe arbitrary catalogs via PostgreSQL's error messages.
_ASSERT_IS_BM25_SQL = """
SELECT 1
FROM pg_index ix
JOIN pg_class i  ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = i.relnamespace
JOIN pg_am am ON am.oid = i.relam
WHERE am.amname = 'bm25' AND n.nspname = %s AND i.relname = %s
"""


async def create_pg_search_index(
    database: Database,
    schema: str,
    table: str,
    columns: list[str],
    index_name: str,
    key_field: str,
    *,
    text_fields: dict[str, Any] | None = None,
    numeric_fields: dict[str, Any] | None = None,
    boolean_fields: dict[str, Any] | None = None,
    json_fields: dict[str, Any] | None = None,
    range_fields: dict[str, Any] | None = None,
    datetime_fields: dict[str, Any] | None = None,
    layer_sizes: str | None = None,
    background_layer_sizes: str | None = None,
    target_segment_count: int | None = None,
    mutable_segment_rows: int | None = None,
    sort_by: str | None = None,
    search_tokenizer: dict[str, Any] | None = None,
    concurrently: bool = True,
) -> CreatePgSearchIndexResult:
    """Build and execute ``CREATE INDEX … USING bm25``.

    All 13 documented bm25 reloptions are exposed as kwargs (see
    BM-0 §2.2). The 7 JSONB-shaped options accept Python dicts and
    are serialized via :func:`json.dumps`; the 2 int options and 3
    text options pass through type-aware validation.
    ``key_field`` is required by upstream.

    Identifier safety: every schema / table / column / index-name /
    key-field string goes through :func:`_validate_identifier` +
    :func:`_pg_quote_ident`. The full rendered statement is
    preserved in :attr:`CreatePgSearchIndexResult.create_sql` for
    auditability.

    The statement runs on an autocommit connection via
    :meth:`Database.run_unmanaged` because ``CREATE INDEX
    CONCURRENTLY`` cannot run inside a transaction block.

    Raises:
        PgSearchError: extension not installed, any identifier fails
            validation, any option fails its bounds / type check, or
            the underlying DDL fails.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(index_name, "index_name")
    _validate_identifier(key_field, "key_field")
    if not isinstance(columns, list) or not columns:
        raise PgSearchError("columns must be a non-empty list of identifiers")
    for col in columns:
        if not isinstance(col, str):
            raise PgSearchError(f"every column must be str; got {type(col).__name__}")
        _validate_identifier(col, "column")
    if not isinstance(concurrently, bool):
        raise PgSearchError(f"concurrently must be a bool; got {concurrently!r}")

    if target_segment_count is not None:
        _validate_int_option_bounded(
            "target_segment_count",
            target_segment_count,
            _TARGET_SEGMENT_COUNT_MIN,
            _TARGET_SEGMENT_COUNT_MAX,
        )
    if mutable_segment_rows is not None:
        _validate_int_option_bounded(
            "mutable_segment_rows",
            mutable_segment_rows,
            _MUTABLE_SEGMENT_ROWS_MIN,
            _MUTABLE_SEGMENT_ROWS_MAX,
        )
    for text_name, text_value in (
        ("layer_sizes", layer_sizes),
        ("background_layer_sizes", background_layer_sizes),
        ("sort_by", sort_by),
    ):
        if text_value is not None:
            _validate_text_option(text_name, text_value)
    for jsonb_name, jsonb_value in (
        ("text_fields", text_fields),
        ("numeric_fields", numeric_fields),
        ("boolean_fields", boolean_fields),
        ("json_fields", json_fields),
        ("range_fields", range_fields),
        ("datetime_fields", datetime_fields),
        ("search_tokenizer", search_tokenizer),
    ):
        if jsonb_value is not None:
            _validate_jsonb_option(jsonb_name, jsonb_value)

    if not await extension_installed(database.driver(), "pg_search"):
        raise PgSearchError("pg_search extension is not installed in this database")

    # Build the WITH clause. ``key_field`` always first for readability;
    # the rest in their declaration order so the rendered SQL is
    # deterministic and audit logs are diffable across runs.
    options: dict[str, Any] = {"key_field": key_field}
    if text_fields is not None:
        options["text_fields"] = text_fields
    if numeric_fields is not None:
        options["numeric_fields"] = numeric_fields
    if boolean_fields is not None:
        options["boolean_fields"] = boolean_fields
    if json_fields is not None:
        options["json_fields"] = json_fields
    if range_fields is not None:
        options["range_fields"] = range_fields
    if datetime_fields is not None:
        options["datetime_fields"] = datetime_fields
    if layer_sizes is not None:
        options["layer_sizes"] = layer_sizes
    if background_layer_sizes is not None:
        options["background_layer_sizes"] = background_layer_sizes
    if target_segment_count is not None:
        options["target_segment_count"] = target_segment_count
    if mutable_segment_rows is not None:
        options["mutable_segment_rows"] = mutable_segment_rows
    if sort_by is not None:
        options["sort_by"] = sort_by
    if search_tokenizer is not None:
        options["search_tokenizer"] = search_tokenizer

    with_clause = ", ".join(_render_option(n, v) for n, v in options.items())
    concurrently_clause = " CONCURRENTLY" if concurrently else ""
    qualified_table = f"{_pg_quote_ident(schema)}.{_pg_quote_ident(table)}"
    quoted_columns = ", ".join(_pg_quote_ident(c) for c in columns)

    sql = (
        f"CREATE INDEX{concurrently_clause} {_pg_quote_ident(index_name)} "
        f"ON {qualified_table} "
        f"USING bm25 ({quoted_columns}) "
        f"WITH ({with_clause})"
    )

    started_at = _utc_iso_now()
    started_mono = _time.monotonic()
    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        raise PgSearchError(f"CREATE INDEX failed: {exc}") from exc
    duration = _time.monotonic() - started_mono
    completed_at = _utc_iso_now()

    return CreatePgSearchIndexResult(
        schema=schema,
        table=table,
        columns=list(columns),
        index_name=index_name,
        key_field=key_field,
        options=options,
        concurrently=concurrently,
        create_sql=sql,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(duration, 6),
    )


async def reindex_pg_search_index(
    database: Database,
    schema: str,
    index: str,
    *,
    concurrently: bool = True,
) -> ReindexPgSearchResult:
    """Run ``REINDEX INDEX [CONCURRENTLY] schema.index``.

    Same pre-flight pattern as :func:`turboquant.reindex_turboquant_index`:
    confirm the named index actually uses the ``bm25`` access method
    before running, so the call can't be turned into a way to probe
    arbitrary catalogs via PostgreSQL's error messages.

    Runs on an autocommit connection because ``REINDEX CONCURRENTLY``
    cannot run inside a transaction block.

    Raises:
        PgSearchError: extension not installed, identifier fails
            validation, or the named index is not a BM25 index.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(index, "index")
    if not isinstance(concurrently, bool):
        raise PgSearchError(f"concurrently must be a bool; got {concurrently!r}")

    driver = database.driver()
    if not await extension_installed(driver, "pg_search"):
        raise PgSearchError("pg_search extension is not installed in this database")

    preflight = await driver.execute_query(_ASSERT_IS_BM25_SQL, params=[schema, index], force_readonly=True)
    if not preflight:
        raise PgSearchError(f"index {schema}.{index} is not a BM25 index (or does not exist); refusing to REINDEX")

    concurrently_clause = " CONCURRENTLY" if concurrently else ""
    qualified = f"{_pg_quote_ident(schema)}.{_pg_quote_ident(index)}"
    sql = f"REINDEX INDEX{concurrently_clause} {qualified}"

    started_at = _utc_iso_now()
    started_mono = _time.monotonic()
    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        raise PgSearchError(f"REINDEX INDEX failed: {exc}") from exc
    duration = _time.monotonic() - started_mono
    completed_at = _utc_iso_now()

    return ReindexPgSearchResult(
        schema=schema,
        index=index,
        concurrently=concurrently,
        reindex_sql=sql,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(duration, 6),
    )


# --- BM-5: advisor + audit category -----------------------------------------
#
# pg_search exposes a thinner "health" surface than turboquant: there's
# no equivalent of ``tq_index_metadata`` / ``delta_health.merge_recommended``
# that flags maintenance need from upstream itself. The advisor signals
# below are sourced from the documented reloption inventory (BM-0 §2.2)
# and the upstream contract that ``key_field`` is required at CREATE
# INDEX time. As pg_search adds runtime telemetry the rule table grows
# here.


@dataclass(frozen=True, slots=True)
class PgSearchAdvisorFinding:
    """A single rule-table hit produced by :func:`recommend_pg_search_maintenance`.

    ``code`` is the stable identifier — ``severity`` and the
    human-readable ``evidence`` / ``suggested_action`` may evolve, but
    ``code`` is the contract callers script against.
    """

    code: str
    severity: str  # GOOD / WARNING / CRITICAL
    schema: str
    index: str
    evidence: str
    suggested_action: str


# Rule codes — stable identifiers. Keep them documented + grep-able
# from the docstrings above.
_RULE_MISSING_KEY_FIELD = "missing_key_field"
_RULE_NO_FIELD_CONFIGS = "no_field_configs"


def _finding_missing_key_field(info: PgSearchIndexInfo) -> PgSearchAdvisorFinding | None:
    # upstream enforces key_field at CREATE INDEX time, but the catalog
    # might still drift (manual reloption edits, restore from a damaged
    # backup, etc.). A BM25 index without key_field can't satisfy
    # queries — surface it loudly.
    if info.key_field is not None:
        return None
    qualified = f"{_pg_quote_ident(info.schema)}.{_pg_quote_ident(info.index)}"
    return PgSearchAdvisorFinding(
        code=_RULE_MISSING_KEY_FIELD,
        severity="CRITICAL",
        schema=info.schema,
        index=info.index,
        evidence=(
            f"BM25 index {info.schema}.{info.index} has no key_field reloption. "
            "key_field is required by upstream — this index will not function."
        ),
        suggested_action=(f"DROP INDEX {qualified}; -- and recreate via create_pg_search_index with key_field set"),
    )


def _finding_no_field_configs(info: PgSearchIndexInfo) -> PgSearchAdvisorFinding | None:
    # All six *_fields reloptions empty → the index falls back to
    # upstream's default tokenization and type-handling for every
    # indexed column. That's legal but rarely intentional once an
    # operator has more than one indexed column.
    has_any_config = any(
        (
            info.text_fields,
            info.numeric_fields,
            info.boolean_fields,
            info.json_fields,
            info.range_fields,
            info.datetime_fields,
        )
    )
    if has_any_config:
        return None
    return PgSearchAdvisorFinding(
        code=_RULE_NO_FIELD_CONFIGS,
        severity="WARNING",
        schema=info.schema,
        index=info.index,
        evidence=(
            f"BM25 index {info.schema}.{info.index} has no field-type configs "
            "(text_fields / numeric_fields / boolean_fields / json_fields / "
            "range_fields / datetime_fields). pg_search will apply default "
            "tokenization and type handling, which may not match indexed "
            "columns' actual types."
        ),
        suggested_action=(
            f"Recreate index {info.schema}.{info.index} via "
            "create_pg_search_index with explicit *_fields reloptions matching "
            "the indexed column types."
        ),
    )


_PER_INDEX_RULES = (_finding_missing_key_field, _finding_no_field_configs)


async def recommend_pg_search_maintenance(driver: SqlDriver) -> list[PgSearchAdvisorFinding]:
    """Walk every BM25 index and emit advisor findings.

    Returns an empty list when the extension is not installed (same
    shape as :func:`list_pg_search_indexes`). Per-index rules are
    sourced from documented signals — see the module-level comment
    above the BM-5 section for the rule-table philosophy.
    """
    if not await extension_installed(driver, "pg_search"):
        return []
    findings: list[PgSearchAdvisorFinding] = []
    for info in await list_pg_search_indexes(driver):
        for rule in _PER_INDEX_RULES:
            if (finding := rule(info)) is not None:
                findings.append(finding)
    return findings


# Score deductions by severity — single source of truth for both the
# adapter below and any external consumers. Mirrors the turboquant
# audit category exactly so the two surfaces score consistently.
_SEVERITY_DEDUCTION = {"CRITICAL": 30, "WARNING": 15, "GOOD": 0}


def _adapt_finding_to_metric(finding: PgSearchAdvisorFinding) -> Any:
    # Lazily-imported audit types kept out of the module-level imports
    # to avoid a circular import (audit re-exports tools that may pull
    # in this module). The adapter lives here so the rule-table
    # contract stays in one file.
    from mcpg.audit import MetricResult

    target = finding.index or "(cluster)"
    return MetricResult(
        name=f"pg_search:{finding.code} on {finding.schema}.{target}" if finding.index else f"pg_search:{finding.code}",
        value=finding.code,
        unit="finding",
        target="no findings",
        status=finding.severity,
        severity=3 if finding.severity == "CRITICAL" else 2 if finding.severity == "WARNING" else 0,
        evidence=finding.evidence,
        suggestion=finding.suggested_action,
    )


async def audit_pg_search_indexes(driver: SqlDriver) -> Any:
    """Scorecard adapter — returns a ``CategoryResult`` or ``None``.

    Returns ``None`` when pg_search is not installed so
    :func:`audit.audit_database` cleanly omits the category for
    deployments that don't use the extension. Otherwise produces a
    CategoryResult whose metrics are the advisor findings, with the
    standard 100-point-down scoring.
    """
    from mcpg.audit import CategoryResult

    if not await extension_installed(driver, "pg_search"):
        return None

    findings = await recommend_pg_search_maintenance(driver)

    score = 100
    metrics = []
    for finding in findings:
        score -= _SEVERITY_DEDUCTION.get(finding.severity, 0)
        metrics.append(_adapt_finding_to_metric(finding))

    score = max(0, score)
    status_label = "GOOD" if score >= 90 else ("WARNING" if score >= 70 else "CRITICAL")

    if not metrics:
        # No findings → emit a single GOOD baseline metric so the
        # scorecard surfaces "category checked, all good" rather than
        # an empty list that looks like the category didn't run.
        from mcpg.audit import MetricResult

        metrics.append(
            MetricResult(
                name="pg_search:no_findings",
                value="ok",
                unit="finding",
                target="no findings",
                status="GOOD",
                severity=0,
                evidence="All pg_search BM25 indexes pass the advisor rules.",
                suggestion="",
            )
        )

    return CategoryResult(
        category="pg_search BM25 Indexes",
        status=status_label,
        score=score,
        metrics=metrics,
    )
