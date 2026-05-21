"""Text search: trigram fuzzy matching (``pg_trgm``) and built-in full-text.

``fuzzy_search`` ranks values by trigram similarity (needs the optional
``pg_trgm`` extension). ``full_text_search`` ranks documents with PostgreSQL's
built-in ``tsvector``/``tsquery`` (no extension required).

Schema/table/column names and the text-search configuration are SQL
identifiers and cannot be parameterised, so each is validated against a
strict identifier pattern before being placed in the query. Search terms are
always bound parameters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Default result cap and minimum trigram similarity (pg_trgm's range is 0..1).
DEFAULT_LIMIT = 10
DEFAULT_THRESHOLD = 0.3

# Default text-search configuration for full-text search.
DEFAULT_TEXT_CONFIG = "english"


class SearchError(Exception):
    """Raised when a search request is invalid."""


@dataclass(frozen=True, slots=True)
class FuzzyMatch:
    """One fuzzy-search hit, with its trigram similarity score."""

    value: str
    score: float


@dataclass(frozen=True, slots=True)
class FuzzySearchResult:
    """The outcome of a fuzzy search.

    ``available`` is false when the ``pg_trgm`` extension is not installed.
    """

    available: bool
    matches: list[FuzzyMatch]


@dataclass(frozen=True, slots=True)
class FullTextMatch:
    """One full-text-search hit, with its ``ts_rank`` score."""

    value: str
    rank: float


def _checked(name: str, kind: str) -> str:
    """Validate a SQL identifier and return it unchanged, or raise."""
    if not _IDENTIFIER.match(name):
        raise SearchError(f"invalid {kind} name: {name!r}")
    return name


def _quoted(name: str, kind: str) -> str:
    """Validate a SQL identifier and return it double-quoted."""
    return f'"{_checked(name, kind)}"'


async def fuzzy_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    term: str,
    *,
    limit: int = DEFAULT_LIMIT,
    threshold: float = DEFAULT_THRESHOLD,
) -> FuzzySearchResult:
    """Rank a text column's values by trigram similarity to ``term``.

    Requires the ``pg_trgm`` extension; when absent the result is returned
    with ``available=False`` and no matches.

    Raises:
        SearchError: If a schema/table/column name is not a valid identifier.
    """
    if not await extension_installed(driver, "pg_trgm"):
        return FuzzySearchResult(available=False, matches=[])

    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    rows = await driver.execute_query(
        f"SELECT {col} AS value, similarity({col}, %s) AS score "
        f"FROM {relation} WHERE similarity({col}, %s) >= %s "
        f"ORDER BY score DESC LIMIT %s",
        params=[term, term, threshold, limit],
        force_readonly=True,
    )
    matches = [FuzzyMatch(value=str(row.cells["value"]), score=row.cells["score"]) for row in rows or []]
    return FuzzySearchResult(available=True, matches=matches)


async def full_text_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    search_query: str,
    *,
    config: str = DEFAULT_TEXT_CONFIG,
    limit: int = DEFAULT_LIMIT,
) -> list[FullTextMatch]:
    """Rank a text column's documents against a full-text query.

    Uses PostgreSQL's built-in ``tsvector``/``tsquery`` — no extension
    required. ``search_query`` accepts web-search syntax (quoted phrases,
    ``or``, ``-`` exclusion) via ``websearch_to_tsquery``.

    Raises:
        SearchError: If a schema/table/column or ``config`` name is not a
            valid identifier.
    """
    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    # config is identifier-validated, so it is safe inside a string literal.
    cfg = f"'{_checked(config, 'text-search config')}'"
    vector = f"to_tsvector({cfg}, {col})"
    tsquery = f"websearch_to_tsquery({cfg}, %s)"
    rows = await driver.execute_query(
        f"SELECT {col} AS value, ts_rank({vector}, {tsquery}) AS rank "
        f"FROM {relation} WHERE {vector} @@ {tsquery} "
        f"ORDER BY rank DESC LIMIT %s",
        params=[search_query, search_query, limit],
        force_readonly=True,
    )
    return [FullTextMatch(value=str(row.cells["value"]), rank=row.cells["rank"]) for row in rows or []]
