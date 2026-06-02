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
class MmrMatch:
    """One MMR-reranked hit.

    ``relevance`` is the cosine similarity to the query (higher = closer).
    ``mmr_score`` is the Maximal Marginal Relevance score at the moment
    the row was selected — ``λ·relevance - (1-λ)·max_sim_to_selected``.
    ``rank`` is the 0-based selection order.
    """

    row: dict[str, Any]
    relevance: float
    mmr_score: float
    rank: int


@dataclass(frozen=True, slots=True)
class MmrSearchResult:
    """The outcome of an MMR search.

    ``available`` is false when the ``vector`` (pgvector) extension is not
    installed.
    """

    available: bool
    matches: list[MmrMatch]


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
    if mode not in _FUZZY_MODES:
        raise SearchError(f"unknown fuzzy mode: {mode!r}")
    if not await extension_installed(driver, "pg_trgm"):
        return FuzzySearchResult(available=False, matches=[])

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


# --- pgvector range search (Phase 11.2) ----------------------------------


async def vector_range_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    query_vector: list[float],
    max_distance: float,
    *,
    metric: str = DEFAULT_VECTOR_METRIC,
    limit: int = DEFAULT_LIMIT,
) -> VectorSearchResult:
    """Return every row within ``max_distance`` of ``query_vector``.

    A different query shape than :func:`vector_search` (top-k). Useful for
    de-duplication ("find duplicates within ε"), similarity gating
    ("only surface results closer than 0.3"), and clustering pre-passes.

    Results are still ordered by distance ascending and capped at
    ``limit`` so a too-loose threshold doesn't pull the entire table —
    a callee that hits ``limit`` should tighten the threshold rather
    than scroll. The same per-metric semantics apply as
    :func:`vector_search`: cosine returns 1 - cos(theta), l2 is euclidean
    distance, inner_product is negated dot product (smaller = closer).

    Raises:
        SearchError: If an identifier is invalid, the metric is unknown,
            ``query_vector`` contains a non-finite value, or
            ``max_distance`` is negative.
    """
    if not await extension_installed(driver, "vector"):
        return VectorSearchResult(available=False, matches=[])
    if metric not in _VECTOR_METRICS:
        raise SearchError(f"unknown vector metric: {metric!r}")
    if not all(math.isfinite(value) for value in query_vector):
        raise SearchError("query_vector must contain only finite numbers")
    if not math.isfinite(max_distance) or max_distance < 0:
        raise SearchError("max_distance must be a non-negative finite number")

    operator = _VECTOR_METRICS[metric]
    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    literal = "[" + ",".join(str(float(value)) for value in query_vector) + "]"
    rows = await driver.execute_query(
        f"SELECT *, {col} {operator} %s::vector AS mcpg_distance "
        f"FROM {relation} WHERE {col} {operator} %s::vector <= %s "
        f"ORDER BY {col} {operator} %s::vector LIMIT %s",
        params=[literal, literal, max_distance, literal, limit],
        force_readonly=True,
    )
    matches: list[VectorMatch] = []
    for row in rows or []:
        cells = dict(row.cells)
        distance = cells.pop("mcpg_distance")
        cells.pop(column, None)
        matches.append(VectorMatch(distance=distance, row=cells))
    return VectorSearchResult(available=True, matches=matches)


# --- MMR re-ranking (pgvector) -------------------------------------------


DEFAULT_MMR_LAMBDA = 0.5


def _parse_embedding(value: Any) -> list[float]:
    """Coerce a pgvector cell into a list of floats.

    pgvector hands its column back either as a Python sequence (when the
    psycopg adapter is registered) or as a bracketed text literal like
    ``"[0.1,0.2,0.3]"`` otherwise — accept both.
    """
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    if isinstance(value, str):
        inner = value.strip().lstrip("[").rstrip("]").strip()
        if not inner:
            return []
        return [float(part) for part in inner.split(",")]
    raise SearchError(f"could not parse embedding value of type {type(value).__name__}")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is zero."""
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


async def mmr_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    query_vector: list[float],
    *,
    k: int = DEFAULT_LIMIT,
    fetch_k: int | None = None,
    lambda_mult: float = DEFAULT_MMR_LAMBDA,
    metric: str = DEFAULT_VECTOR_METRIC,
) -> MmrSearchResult:
    """Re-rank a vector search for diversity via Maximal Marginal Relevance.

    Plain k-NN tends to return near-duplicates; MMR trades a little
    relevance for variety, which usually improves the context an agent
    feeds to an LLM. The algorithm fetches ``fetch_k`` nearest candidates
    by ``metric`` (the recall pass), then greedily picks ``k`` of them
    maximising ``λ·sim(query, doc) - (1-λ)·max sim(doc, picked)``.
    ``lambda_mult=1`` is pure relevance (≈ vector_search); ``0`` is pure
    diversity. Relevance and the diversity penalty are both cosine
    similarities computed in-process over the candidate embeddings, so
    the result is independent of which ``metric`` drove the recall pass.

    Args:
        k: How many rows to return.
        fetch_k: Candidate pool size for the recall pass; defaults to
            ``max(4·k, 20)``. Larger gives MMR more to choose from at the
            cost of a wider initial scan.
        lambda_mult: Relevance/diversity trade-off in ``[0, 1]``.
        metric: ``l2``, ``cosine``, or ``inner_product`` for the recall pass.

    Raises:
        SearchError: On an invalid identifier / metric, a non-finite query
            value, or out-of-range ``k`` / ``fetch_k`` / ``lambda_mult``.
    """
    if not await extension_installed(driver, "vector"):
        return MmrSearchResult(available=False, matches=[])
    if metric not in _VECTOR_METRICS:
        raise SearchError(f"unknown vector metric: {metric!r}")
    if not all(math.isfinite(value) for value in query_vector):
        raise SearchError("query_vector must contain only finite numbers")
    if k < 1:
        raise SearchError("k must be at least 1")
    if not 0.0 <= lambda_mult <= 1.0:
        raise SearchError("lambda_mult must be between 0 and 1")
    pool = fetch_k if fetch_k is not None else max(4 * k, 20)
    if pool < k:
        raise SearchError("fetch_k must be >= k")

    operator = _VECTOR_METRICS[metric]
    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    literal = "[" + ",".join(str(float(value)) for value in query_vector) + "]"
    # Keep the embedding column this time — MMR needs candidate vectors to
    # measure their similarity to one another.
    rows = await driver.execute_query(
        f"SELECT *, {col} {operator} %s::vector AS mcpg_distance "
        f"FROM {relation} ORDER BY {col} {operator} %s::vector LIMIT %s",
        params=[literal, literal, pool],
        force_readonly=True,
    )

    candidates: list[tuple[dict[str, Any], list[float], float]] = []
    for row in rows or []:
        cells = dict(row.cells)
        cells.pop("mcpg_distance", None)
        embedding = _parse_embedding(cells.get(column))
        cells.pop(column, None)  # drop the embedding from the returned row
        relevance = _cosine_similarity(query_vector, embedding)
        candidates.append((cells, embedding, relevance))

    matches: list[MmrMatch] = []
    selected: list[list[float]] = []
    remaining = list(range(len(candidates)))
    target = min(k, len(candidates))
    while len(matches) < target:
        best_idx = -1
        best_score = -math.inf
        for idx in remaining:
            _, embedding, relevance = candidates[idx]
            diversity = max((_cosine_similarity(embedding, s) for s in selected), default=0.0)
            score = lambda_mult * relevance - (1.0 - lambda_mult) * diversity
            if score > best_score:
                best_score = score
                best_idx = idx
        cells, embedding, relevance = candidates[best_idx]
        matches.append(MmrMatch(row=cells, relevance=relevance, mmr_score=best_score, rank=len(matches)))
        selected.append(embedding)
        remaining.remove(best_idx)

    return MmrSearchResult(available=True, matches=matches)


# --- hybrid search (Phase 11.1) ------------------------------------------


@dataclass(frozen=True, slots=True)
class HybridMatch:
    """One hybrid-search hit with the per-source ranks and fused score.

    ``vector_rank`` / ``fts_rank`` are 1-indexed positions in each
    source's ranking, or ``None`` when the row didn't appear there.
    ``rrf_score`` is the reciprocal-rank-fusion score
    (Σ 1/(k+rank)) used to order results.
    """

    rrf_score: float
    vector_rank: int | None
    fts_rank: int | None
    vector_distance: float | None
    fts_rank_score: float | None
    row: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    """The outcome of a hybrid search.

    ``available`` is false when the ``vector`` extension isn't installed
    (the full-text half alone is always available since PG's tsvector is
    built in, but a hybrid call without pgvector is degenerate and the
    caller should know).
    """

    available: bool
    matches: list[HybridMatch]


# RRF's standard "k=60" smoothing constant — high enough that rank-1
# vs rank-2 isn't a huge gap, low enough to still differentiate.
_HYBRID_RRF_K = 60


async def hybrid_search(
    driver: SqlDriver,
    schema: str,
    table: str,
    vector_column: str,
    text_column: str,
    query_vector: list[float],
    text_query: str,
    *,
    metric: str = DEFAULT_VECTOR_METRIC,
    text_config: str = DEFAULT_TEXT_CONFIG,
    limit: int = DEFAULT_LIMIT,
    candidate_pool: int = 50,
    rrf_k: int = _HYBRID_RRF_K,
) -> HybridSearchResult:
    """Combine vector and full-text ranking via reciprocal-rank fusion.

    Pulls ``candidate_pool`` candidates from each source (vector k-NN
    on ``vector_column`` and ``websearch_to_tsquery`` on
    ``text_column``), fuses them with RRF
    (score = Σ 1/(``rrf_k`` + rank)), and returns the top ``limit``.
    Rows present in only one source are still included; their score
    just comes from one term.

    This is the biggest unmet need for agentic RAG: pure vector search
    misses keyword matches (proper nouns, identifiers, numbers); pure
    full-text misses semantic synonyms. RRF is the well-studied,
    parameter-free way to combine them.

    Raises:
        SearchError: When an identifier / metric / config is invalid,
            ``query_vector`` contains a non-finite value, or
            ``candidate_pool`` / ``rrf_k`` are non-positive.
    """
    if not await extension_installed(driver, "vector"):
        return HybridSearchResult(available=False, matches=[])
    if metric not in _VECTOR_METRICS:
        raise SearchError(f"unknown vector metric: {metric!r}")
    if not all(math.isfinite(value) for value in query_vector):
        raise SearchError("query_vector must contain only finite numbers")
    if candidate_pool < 1:
        raise SearchError("candidate_pool must be at least 1")
    if rrf_k < 1:
        raise SearchError("rrf_k must be at least 1")

    operator = _VECTOR_METRICS[metric]
    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    vcol = _quoted(vector_column, "column")
    tcol = _quoted(text_column, "column")
    cfg = f"'{_checked(text_config, 'text-search config')}'"
    literal = "[" + ",".join(str(float(value)) for value in query_vector) + "]"

    # Pull the vector candidates with their distance + rank.
    vec_rows = await driver.execute_query(
        f"SELECT *, {vcol} {operator} %s::vector AS mcpg_distance, "
        f"row_number() OVER (ORDER BY {vcol} {operator} %s::vector) AS mcpg_rank "
        f"FROM {relation} ORDER BY {vcol} {operator} %s::vector LIMIT %s",
        params=[literal, literal, literal, candidate_pool],
        force_readonly=True,
    )
    # Pull the FTS candidates with their ts_rank + rank.
    vector = f"to_tsvector({cfg}, {tcol})"
    tsquery = f"websearch_to_tsquery({cfg}, %s)"
    fts_rows = await driver.execute_query(
        f"SELECT *, ts_rank({vector}, {tsquery}) AS mcpg_rank_score, "
        f"row_number() OVER (ORDER BY ts_rank({vector}, {tsquery}) DESC) AS mcpg_rank "
        f"FROM {relation} WHERE {vector} @@ {tsquery} "
        f"ORDER BY mcpg_rank_score DESC LIMIT %s",
        params=[text_query, text_query, text_query, candidate_pool],
        force_readonly=True,
    )

    return _fuse_rrf(
        vec_rows or [],
        fts_rows or [],
        vector_column,
        text_column,
        rrf_k=rrf_k,
        limit=limit,
    )


def _row_key(cells: dict[str, Any]) -> tuple[Any, ...] | str:
    """Pick a stable key for matching a row across the two candidate sets.

    Prefer a single ``id`` column, then any ``*_id``-named column, then
    fall back to the frozenset of the row's items. The fallback should
    rarely fire because most pgvector tables carry a primary key.
    """
    if "id" in cells:
        return ("id", cells["id"])
    for name in cells:
        if name.endswith("_id"):
            return (name, cells[name])
    return tuple(sorted((str(k), str(v)) for k, v in cells.items()))


def _fuse_rrf(
    vec_rows: list[Any],
    fts_rows: list[Any],
    vector_column: str,
    text_column: str,
    *,
    rrf_k: int,
    limit: int,
) -> HybridSearchResult:
    """Combine two candidate lists by reciprocal-rank fusion."""

    by_key: dict[Any, HybridMatch] = {}

    def merge(
        key: Any,
        cells: dict[str, Any],
        *,
        vec_rank: int | None,
        fts_rank: int | None,
        vec_distance: float | None,
        fts_score: float | None,
    ) -> None:
        existing = by_key.get(key)
        if existing is None:
            score = 0.0
            if vec_rank is not None:
                score += 1.0 / (rrf_k + vec_rank)
            if fts_rank is not None:
                score += 1.0 / (rrf_k + fts_rank)
            by_key[key] = HybridMatch(
                rrf_score=score,
                vector_rank=vec_rank,
                fts_rank=fts_rank,
                vector_distance=vec_distance,
                fts_rank_score=fts_score,
                row=cells,
            )
            return
        score = existing.rrf_score
        new_vec_rank = existing.vector_rank
        new_fts_rank = existing.fts_rank
        new_vec_dist = existing.vector_distance
        new_fts_score = existing.fts_rank_score
        if vec_rank is not None and existing.vector_rank is None:
            score += 1.0 / (rrf_k + vec_rank)
            new_vec_rank = vec_rank
            new_vec_dist = vec_distance
        if fts_rank is not None and existing.fts_rank is None:
            score += 1.0 / (rrf_k + fts_rank)
            new_fts_rank = fts_rank
            new_fts_score = fts_score
        by_key[key] = HybridMatch(
            rrf_score=score,
            vector_rank=new_vec_rank,
            fts_rank=new_fts_rank,
            vector_distance=new_vec_dist,
            fts_rank_score=new_fts_score,
            row=existing.row,
        )

    # Strip BOTH the vector and text columns from the per-row key on
    # every branch — the _row_key fallback (sorted cell tuple) only
    # merges the two halves if their key inputs are identical.
    for row in vec_rows:
        cells = dict(row.cells)
        rank = cells.pop("mcpg_rank")
        distance = cells.pop("mcpg_distance")
        cells.pop(vector_column, None)
        cells.pop(text_column, None)
        key = _row_key(cells)
        merge(key, cells, vec_rank=int(rank), fts_rank=None, vec_distance=distance, fts_score=None)

    for row in fts_rows:
        cells = dict(row.cells)
        rank = cells.pop("mcpg_rank")
        score = cells.pop("mcpg_rank_score")
        cells.pop(text_column, None)
        cells.pop(vector_column, None)
        key = _row_key(cells)
        merge(key, cells, vec_rank=None, fts_rank=int(rank), vec_distance=None, fts_score=score)

    fused = sorted(by_key.values(), key=lambda m: m.rrf_score, reverse=True)[:limit]
    return HybridSearchResult(available=True, matches=fused)


# --- vector-quantization advisor (Phase 11.3) ----------------------------


@dataclass(frozen=True, slots=True)
class QuantizationRecommendation:
    """Advice for converting a vector column to a more compact type.

    Per-column estimates of current bytes vs the alternative, plus the
    rough cost saving and a one-line rationale.
    """

    schema: str
    table: str
    column: str
    dimension: int
    row_count: int
    current_type: str
    current_bytes: int
    suggested_type: str
    suggested_bytes: int
    savings_ratio: float
    rationale: str


# pgvector storage per element:
#   vector(N)   → 4 bytes per element (float4)
#   halfvec(N)  → 2 bytes per element (float16) — pgvector v0.7+
#   bit(N)      → 1 bit per element (vector of 0/1)
_TYPE_BYTES_PER_ELEMENT: dict[str, float] = {
    "vector": 4.0,
    "halfvec": 2.0,
    "bit": 1.0 / 8,
}
# Tuple-overhead per row in bytes — header + length prefix for the
# varlena. Doesn't change between types so we omit it from the savings
# calc, but document the assumption.


def _suggest_quantization(
    *,
    schema: str,
    table: str,
    column: str,
    current_type: str,
    dimension: int,
    row_count: int,
) -> QuantizationRecommendation | None:
    """Decide whether to recommend a more compact vector type.

    The thresholds are intentionally conservative — quantization trades
    storage for a small recall hit, so we only flag the column when
    the win is large enough to justify the migration cost. v1
    heuristic:

    - Already non-``vector`` (halfvec / bit) → no recommendation; the
      user already picked something compact.
    - Estimated table footprint < 100 MiB → low value; skip.
    - Dimension ≥ 768 → recommend ``halfvec`` (2x saving with minimal
      recall loss for high-dim embeddings).
    - Dimension < 768 and row_count x dimension x 4B ≥ 500 MiB →
      recommend ``halfvec`` too; the absolute saving justifies it.
    - ``bit`` is suggested only when the user opts into a much higher
      recall hit (we leave it as a documented advanced option and do
      not auto-recommend in v1).
    """
    if current_type not in {"vector"}:
        return None  # already quantized; nothing to do
    current_bytes_per_row = dimension * _TYPE_BYTES_PER_ELEMENT["vector"]
    current_bytes = int(current_bytes_per_row * row_count)
    if current_bytes < 100 * 1024 * 1024 and not (dimension >= 768 and row_count >= 10_000):
        return None
    # Recommend halfvec.
    suggested_bytes_per_row = dimension * _TYPE_BYTES_PER_ELEMENT["halfvec"]
    suggested_bytes = int(suggested_bytes_per_row * row_count)
    savings_ratio = 1 - suggested_bytes / current_bytes if current_bytes else 0.0
    rationale = (
        f"halfvec halves storage with negligible recall loss for d={dimension} "
        f"embeddings; saves ~{savings_ratio * 100:.0f}% on this column. "
        "Requires pgvector v0.7+."
    )
    return QuantizationRecommendation(
        schema=schema,
        table=table,
        column=column,
        dimension=dimension,
        row_count=row_count,
        current_type=current_type,
        current_bytes=current_bytes,
        suggested_type="halfvec",
        suggested_bytes=suggested_bytes,
        savings_ratio=savings_ratio,
        rationale=rationale,
    )


async def recommend_vector_quantization(
    driver: SqlDriver,
    schema: str,
) -> list[QuantizationRecommendation]:
    """Scan ``schema`` for ``vector(N)`` columns whose storage could shrink.

    Returns one recommendation per column where switching to a more
    compact pgvector type (``halfvec`` in v1) would yield a meaningful
    saving on the table's estimated footprint. Skips columns that are
    already non-``vector`` and small tables where the absolute saving
    doesn't justify the migration.

    Requires ``vector`` (pgvector) installed; when absent, returns
    an empty list. Schema name is validated; only plain identifiers
    accepted.
    """
    _checked(schema, "schema")
    if not await extension_installed(driver, "vector"):
        return []

    # Find every (table, column) whose type matches the pgvector family.
    # Restrict the base type via the typname so we don't pick up PG's
    # built-in ``bit(N)`` type (which is unrelated to pgvector). pgvector's
    # types are ``vector`` / ``halfvec`` / ``sparsevec``. atttypmod carries
    # the dimension directly — pgvector stores it un-adjusted (no -4
    # varlena header subtraction).
    rows = await driver.execute_query(
        "SELECT c.table_name, c.column_name, "
        "       t.typname AS base_type, "
        "       a.atttypmod AS dimension "
        "FROM information_schema.columns c "
        "JOIN pg_catalog.pg_attribute a ON a.attname = c.column_name "
        "JOIN pg_catalog.pg_class cls ON cls.oid = a.attrelid AND cls.relname = c.table_name "
        "JOIN pg_catalog.pg_namespace n ON n.oid = cls.relnamespace AND n.nspname = c.table_schema "
        "JOIN pg_catalog.pg_type t ON t.oid = a.atttypid "
        "WHERE c.table_schema = %s "
        "AND t.typname IN ('vector', 'halfvec', 'sparsevec') "
        "AND a.atttypmod > 0 "
        "ORDER BY c.table_name, c.column_name",
        params=[schema],
        force_readonly=True,
    )
    recommendations: list[QuantizationRecommendation] = []
    for row in rows or []:
        table = str(row.cells["table_name"])
        column = str(row.cells["column_name"])
        base_type = str(row.cells["base_type"])
        dimension = int(row.cells["dimension"])
        # Live row count for the table (no estimates — small price for
        # accurate advice).
        count_rows = await driver.execute_query(
            f"SELECT count(*) AS n FROM {_quoted(schema, 'schema')}.{_quoted(table, 'table')}",
            force_readonly=True,
        )
        row_count = int(count_rows[0].cells["n"]) if count_rows else 0
        rec = _suggest_quantization(
            schema=schema,
            table=table,
            column=column,
            current_type=base_type,
            dimension=dimension,
            row_count=row_count,
        )
        if rec is not None:
            recommendations.append(rec)
    return recommendations


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
