"""Search tools: trigram fuzzy, full-text, pgvector k-NN, and PostGIS geo.

``fuzzy_search`` ranks values by ``pg_trgm`` trigram similarity (optional
extension). ``full_text_search`` ranks documents with PostgreSQL's built-in
``tsvector``/``tsquery`` (no extension). ``vector_search`` finds nearest rows
by ``pgvector`` distance, and ``geo_search`` finds nearest rows by PostGIS
distance to a point (both need their optional extension).

Schema/table/column names and the text-search configuration are SQL
identifiers and cannot be parameterised, so each is validated against a
strict identifier pattern before being placed in the query. Search terms and
query vectors are always bound parameters.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Default result cap and minimum trigram similarity (pg_trgm's range is 0..1).
DEFAULT_LIMIT = 10
DEFAULT_THRESHOLD = 0.3

# Fuzzy-match mode: "word" matches the term against the best word window of
# the value (good for fragments); "full" compares the whole strings.
_FUZZY_MODES = frozenset({"word", "full"})
DEFAULT_FUZZY_MODE = "word"

# Default text-search configuration for full-text search.
DEFAULT_TEXT_CONFIG = "english"

# Vector-distance metric -> pgvector operator.
_VECTOR_METRICS = {"l2": "<->", "cosine": "<=>", "inner_product": "<#>"}
DEFAULT_VECTOR_METRIC = "l2"


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


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """One vector-search hit: the row (minus the embedding) and its distance."""

    distance: float
    row: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VectorSearchResult:
    """The outcome of a vector search.

    ``available`` is false when the ``vector`` (pgvector) extension is not
    installed.
    """

    available: bool
    matches: list[VectorMatch]


@dataclass(frozen=True, slots=True)
class GeoMatch:
    """One geo-search hit: the row (minus the geometry column) and distance."""

    distance: float
    row: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GeoSearchResult:
    """The outcome of a geo search.

    ``available`` is false when the ``postgis`` extension is not installed.
    """

    available: bool
    matches: list[GeoMatch]


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
    mode: str = DEFAULT_FUZZY_MODE,
    limit: int = DEFAULT_LIMIT,
    threshold: float = DEFAULT_THRESHOLD,
) -> FuzzySearchResult:
    """Rank a text column's values by trigram similarity to ``term``.

    Requires the ``pg_trgm`` extension; when absent the result is returned
    with ``available=False`` and no matches.

    Args:
        mode: ``word`` (default) scores the term against the best-matching
            word window of each value — good for fragments and misspellings
            within longer text. ``full`` compares the whole strings.

    Raises:
        SearchError: If an identifier is invalid or ``mode`` is unknown.
    """
    if not await extension_installed(driver, "pg_trgm"):
        return FuzzySearchResult(available=False, matches=[])
    if mode not in _FUZZY_MODES:
        raise SearchError(f"unknown fuzzy mode: {mode!r}")

    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    score = f"similarity({col}, %s)" if mode == "full" else f"word_similarity(%s, {col})"
    rows = await driver.execute_query(
        f"SELECT {col} AS value, {score} AS score FROM {relation} WHERE {score} >= %s ORDER BY score DESC LIMIT %s",
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


async def vector_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    query_vector: list[float],
    *,
    metric: str = DEFAULT_VECTOR_METRIC,
    limit: int = DEFAULT_LIMIT,
) -> VectorSearchResult:
    """Find the rows nearest to ``query_vector`` by ``pgvector`` distance.

    Requires the ``vector`` extension; when absent the result is returned
    with ``available=False``. Each match's ``row`` is the full row excluding
    the embedding column itself.

    Args:
        metric: ``l2``, ``cosine``, or ``inner_product``.

    Raises:
        SearchError: If an identifier is invalid, ``metric`` is unknown, or
            ``query_vector`` contains a non-finite value.
    """
    if not await extension_installed(driver, "vector"):
        return VectorSearchResult(available=False, matches=[])
    if metric not in _VECTOR_METRICS:
        raise SearchError(f"unknown vector metric: {metric!r}")
    if not all(math.isfinite(value) for value in query_vector):
        raise SearchError("query_vector must contain only finite numbers")

    operator = _VECTOR_METRICS[metric]
    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    # pgvector accepts a bracketed text literal cast to ``vector``.
    literal = "[" + ",".join(str(float(value)) for value in query_vector) + "]"
    rows = await driver.execute_query(
        f"SELECT *, {col} {operator} %s::vector AS mcpg_distance "
        f"FROM {relation} ORDER BY {col} {operator} %s::vector LIMIT %s",
        params=[literal, literal, limit],
        force_readonly=True,
    )
    matches: list[VectorMatch] = []
    for row in rows or []:
        cells = dict(row.cells)
        distance = cells.pop("mcpg_distance")
        cells.pop(column, None)  # drop the embedding column from the result
        matches.append(VectorMatch(distance=distance, row=cells))
    return VectorSearchResult(available=True, matches=matches)


async def geo_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    longitude: float,
    latitude: float,
    *,
    limit: int = DEFAULT_LIMIT,
) -> GeoSearchResult:
    """Find the rows nearest to a point by PostGIS distance.

    Requires the ``postgis`` extension; when absent the result is returned
    with ``available=False``. The point is interpreted as lon/lat in SRID
    4326; ``distance`` is in the units of that coordinate system. Each
    match's ``row`` excludes the geometry column itself.

    Raises:
        SearchError: If a schema/table/column name is not a valid identifier.
    """
    if not await extension_installed(driver, "postgis"):
        return GeoSearchResult(available=False, matches=[])

    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    # Casting to geometry accepts both geometry and geography columns.
    point = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
    distance_expr = f"{col}::geometry <-> {point}"
    rows = await driver.execute_query(
        f"SELECT *, {distance_expr} AS mcpg_distance FROM {relation} ORDER BY {distance_expr} LIMIT %s",
        params=[longitude, latitude, longitude, latitude, limit],
        force_readonly=True,
    )
    matches: list[GeoMatch] = []
    for row in rows or []:
        cells = dict(row.cells)
        distance = cells.pop("mcpg_distance")
        cells.pop(column, None)  # drop the geometry column from the result
        matches.append(GeoMatch(distance=distance, row=cells))
    return GeoSearchResult(available=True, matches=matches)
