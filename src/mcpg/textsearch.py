"""Fuzzy text search via the ``pg_trgm`` extension.

``fuzzy_search`` ranks a column's values by trigram similarity to a search
term. The extension is optional; when it is not installed the result
degrades gracefully with ``available=False``.

Schema/table/column are SQL identifiers and cannot be parameterised, so each
is validated against a strict identifier pattern and double-quoted before
being placed in the query. The search term is always a bound parameter.
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


def _identifier(name: str, kind: str) -> str:
    """Validate a SQL identifier and return it double-quoted."""
    if not _IDENTIFIER.match(name):
        raise SearchError(f"invalid {kind} name: {name!r}")
    return f'"{name}"'


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

    relation = f"{_identifier(schema, 'schema')}.{_identifier(table, 'table')}"
    col = _identifier(column, "column")
    rows = await driver.execute_query(
        f"SELECT {col} AS value, similarity({col}, %s) AS score "
        f"FROM {relation} WHERE similarity({col}, %s) >= %s "
        f"ORDER BY score DESC LIMIT %s",
        params=[term, term, threshold, limit],
        force_readonly=True,
    )
    matches = [FuzzyMatch(value=str(row.cells["value"]), score=row.cells["score"]) for row in rows or []]
    return FuzzySearchResult(available=True, matches=matches)
