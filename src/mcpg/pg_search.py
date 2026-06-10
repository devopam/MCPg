"""pg_search integration: observability + search execution (phases BM-1, BM-2).

`pg_search <https://github.com/paradedb/paradedb>`_ is a PostgreSQL
extension by ParadeDB that ships a Tantivy-backed BM25 index access
method (``USING bm25``) + a `pdb.*` schema of query-building and
projection helpers. This module covers the *observability* and
*search-execution* slices — catalog enumeration, metadata fetch,
keyword search, more-like-this, and query-string parsing. Subsequent
phases will add hybrid composition (BM-3), DDL (BM-4), and advisor +
audit (BM-5).

* :func:`list_pg_search_indexes`,
  :func:`get_pg_search_index_metadata` — observability.
* :func:`pg_search_run`, :func:`pg_search_more_like_this`,
  :func:`pg_search_parse_query` — search execution.

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
    params: list[Any] = [query]
    if snippet_field_validated is not None:
        select_parts.append(
            f"pdb.snippets({_TABLE_ALIAS}.{_pg_quote_ident(snippet_field_validated)}, "
            f"%s, %s, %s, NULL, NULL, '{_SNIPPET_DEFAULT_SORT_BY}') AS snippets"
        )
        params.extend([snippet_start_tag, snippet_end_tag, snippet_max_num_chars])
    params.append(limit)

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


async def pg_search_more_like_this(
    driver: SqlDriver,
    schema: str,
    table: str,
    document_id: Any,
    key_field: str,
    *,
    limit: int,
) -> list[PgSearchHit]:
    """Find rows similar to the row identified by ``document_id``.

    Wraps ``pdb.more_like_this(anyelement, ...)`` with only the row
    reference arg — the eight tuning knobs (min/max doc frequency,
    term frequency, query terms, word length, boost factor,
    stopwords) are documented as out-of-scope for BM-2 (see
    `docs/plans/bm25-integration.md` §6) and stay at upstream's
    defaults. The seed row is loaded from the same table via a
    correlated subquery so the wrapper takes ``document_id`` as an
    opaque key value rather than a row tuple.

    Built SQL::

        SELECT t."<key>", pdb.score(t) AS score
        FROM "<schema>"."<table>" AS t
        WHERE t @@@ (
            SELECT pdb.more_like_this(seed)
            FROM "<schema>"."<table>" AS seed
            WHERE seed."<key>" = <document_id>
        )
        ORDER BY pdb.score(t) DESC
        LIMIT <limit>

    Raises:
        PgSearchError: extension missing, identifier validation
            fails, or limit out of bounds.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(key_field, "key_field")
    _validate_limit(limit)

    if not await extension_installed(driver, "pg_search"):
        raise PgSearchError("pg_search extension is not installed in this database")

    qualified_table = f"{_pg_quote_ident(schema)}.{_pg_quote_ident(table)}"
    quoted_key = _pg_quote_ident(key_field)

    sql = (
        f"SELECT {_TABLE_ALIAS}.{quoted_key} AS id, pdb.score({_TABLE_ALIAS}) AS score "
        f"FROM {qualified_table} AS {_TABLE_ALIAS} "
        f"WHERE {_TABLE_ALIAS} @@@ ("
        f"SELECT pdb.more_like_this(seed) "
        f"FROM {qualified_table} AS seed "
        f"WHERE seed.{quoted_key} = %s"
        f") "
        f"ORDER BY pdb.score({_TABLE_ALIAS}) DESC "
        f"LIMIT %s"
    )
    rows = await driver.execute_query(sql, params=[document_id, limit], force_readonly=True)
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
