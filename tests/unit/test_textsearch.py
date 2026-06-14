"""Tests for fuzzy text search and the fuzzy_search tool."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.textsearch import (
    FullTextMatch,
    FuzzyMatch,
    GeoMatch,
    HybridMatch,
    HybridSearchResult,
    MmrSearchResult,
    QuantizationRecommendation,
    SearchError,
    VectorMatch,
    _fuse_rrf,
    _row_key,
    _suggest_quantization,
    full_text_search,
    fuzzy_search,
    geo_search,
    hybrid_search,
    mmr_search,
    recommend_vector_quantization,
    vector_range_search,
    vector_search,
)

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_fuzzy_search_reports_unavailable_without_pg_trgm() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await fuzzy_search(driver, "app", "users", "name", "alice")  # type: ignore[arg-type]

    assert result.available is False
    assert result.matches == []


async def test_fuzzy_search_ranks_matches_by_similarity() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "similarity": [{"value": "alice", "score": 0.9}, {"value": "alicia", "score": 0.5}],
        }
    )

    result = await fuzzy_search(driver, "app", "users", "name", "alice")  # type: ignore[arg-type]

    assert result.available is True
    assert result.matches == [FuzzyMatch("alice", 0.9), FuzzyMatch("alicia", 0.5)]


async def test_fuzzy_search_binds_the_term_threshold_and_limit_as_parameters() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "similarity": []})

    await fuzzy_search(driver, "app", "users", "name", "bob", limit=7, threshold=0.4)  # type: ignore[arg-type]

    search_call = next(call for call in driver.calls if "similarity" in call[0])
    assert search_call[1] == ["bob", "bob", 0.4, 7]


@pytest.mark.parametrize("bad", ["users; DROP TABLE x", 'a"b', "1leading_digit", "has space"])
async def test_fuzzy_search_rejects_invalid_identifiers(bad: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="invalid"):
        await fuzzy_search(driver, bad, "users", "name", "alice")  # type: ignore[arg-type]


async def test_fuzzy_search_word_mode_uses_word_similarity_by_default() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "similarity": []})

    await fuzzy_search(driver, "app", "users", "name", "alice")  # type: ignore[arg-type]

    search_call = next(call for call in driver.calls if "ORDER BY" in call[0])
    assert "word_similarity(" in search_call[0]


async def test_fuzzy_search_full_mode_uses_whole_string_similarity() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "similarity": []})

    await fuzzy_search(driver, "app", "users", "name", "alice", mode="full")  # type: ignore[arg-type]

    search_call = next(call for call in driver.calls if "ORDER BY" in call[0])
    assert "word_similarity" not in search_call[0]
    assert "similarity(" in search_call[0]


async def test_fuzzy_search_rejects_an_unknown_mode() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="mode"):
        await fuzzy_search(driver, "app", "users", "name", "alice", mode="bogus")  # type: ignore[arg-type]


async def test_fuzzy_search_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "fuzzy_search",
            {"schema": "app", "table": "users", "column": "name", "term": "alice"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is False


# --- full-text search ------------------------------------------------------


async def test_full_text_search_ranks_documents() -> None:
    driver = FakeDriver([{"value": "the cat sat", "rank": 0.8}, {"value": "a cat", "rank": 0.2}])

    result = await full_text_search(driver, "app", "posts", "body", "cat")

    assert result == [FullTextMatch("the cat sat", 0.8), FullTextMatch("a cat", 0.2)]


async def test_full_text_search_binds_the_query_and_limit_as_parameters() -> None:
    driver = FakeDriver([])

    await full_text_search(driver, "app", "posts", "body", "cat or dog", limit=5)

    assert driver.calls[0][1] == ["cat or dog", "cat or dog", 5]


@pytest.mark.parametrize("bad", ["posts; DROP TABLE x", 'a"b', "1bad"])
async def test_full_text_search_rejects_invalid_identifiers(bad: str) -> None:
    with pytest.raises(SearchError, match="invalid"):
        await full_text_search(FakeDriver(), bad, "posts", "body", "cat")


async def test_full_text_search_rejects_an_invalid_text_config() -> None:
    with pytest.raises(SearchError, match="text-search config"):
        await full_text_search(FakeDriver(), "app", "posts", "body", "cat", config="en'; DROP")


async def test_full_text_search_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([{"value": "the cat sat", "rank": 0.8}]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "full_text_search",
            {"schema": "app", "table": "posts", "column": "body", "search_query": "cat"},
        )

    assert result.isError is False


# --- vector search ---------------------------------------------------------


async def test_vector_search_reports_unavailable_without_pgvector() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await vector_search(driver, "app", "docs", "embedding", [1.0, 2.0])  # type: ignore[arg-type]

    assert result.available is False
    assert result.matches == []


async def test_vector_search_returns_rows_without_the_embedding_column() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "::vector": [{"id": 1, "title": "a", "embedding": [0.1], "mcpg_distance": 0.25}],
        }
    )

    result = await vector_search(driver, "app", "docs", "embedding", [1.0, 2.0])  # type: ignore[arg-type]

    assert result.available is True
    assert result.matches == [VectorMatch(distance=0.25, row={"id": 1, "title": "a"})]


async def test_vector_search_binds_the_query_vector_and_limit() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "::vector": []})

    await vector_search(driver, "app", "docs", "embedding", [1.0, 2.5], limit=4)  # type: ignore[arg-type]

    search_call = next(call for call in driver.calls if "::vector" in call[0])
    assert search_call[1] == ["[1.0,2.5]", "[1.0,2.5]", 4]


async def test_vector_search_rejects_an_unknown_metric() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="metric"):
        await vector_search(driver, "app", "docs", "embedding", [1.0], metric="manhattan")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["docs; DROP TABLE x", 'a"b', "1bad"])
async def test_vector_search_rejects_invalid_identifiers(bad: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="invalid"):
        await vector_search(driver, bad, "docs", "embedding", [1.0])  # type: ignore[arg-type]


async def test_vector_search_rejects_a_non_finite_query_vector() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="finite"):
        await vector_search(driver, "app", "docs", "embedding", [1.0, float("nan")])  # type: ignore[arg-type]


async def test_vector_search_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "vector_search",
            {"schema": "app", "table": "docs", "column": "embedding", "query_vector": [1.0, 2.0]},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is False


# --- MMR search ------------------------------------------------------------


def _mmr_candidates() -> dict[str, list[dict[str, object]]]:
    # query is [1,0]; A is identical, B is near-duplicate of A, C is diverse.
    return {
        "pg_extension": [{"present": 1}],
        "::vector": [
            {"id": 1, "embedding": [1.0, 0.0], "mcpg_distance": 0.0},
            {"id": 2, "embedding": [0.99, 0.141], "mcpg_distance": 0.01},
            {"id": 3, "embedding": [0.0, 1.0], "mcpg_distance": 1.0},
        ],
    }


async def test_mmr_search_reports_unavailable_without_pgvector() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await mmr_search(driver, "app", "docs", "embedding", [1.0, 0.0])  # type: ignore[arg-type]

    assert result == MmrSearchResult(available=False, matches=[])


async def test_mmr_search_pure_relevance_keeps_the_near_duplicate() -> None:
    # lambda=1 ignores diversity -> top-2 by relevance: A then B.
    driver = FakeRoutingDriver(_mmr_candidates())

    result = await mmr_search(driver, "app", "docs", "embedding", [1.0, 0.0], k=2, lambda_mult=1.0)  # type: ignore[arg-type]

    assert result.available is True
    assert [m.row["id"] for m in result.matches] == [1, 2]
    assert [m.rank for m in result.matches] == [0, 1]


async def test_mmr_search_pure_diversity_swaps_in_the_distinct_row() -> None:
    # lambda=0 maximises diversity -> A first, then C (not the near-dup B).
    driver = FakeRoutingDriver(_mmr_candidates())

    result = await mmr_search(driver, "app", "docs", "embedding", [1.0, 0.0], k=2, lambda_mult=0.0)  # type: ignore[arg-type]

    assert [m.row["id"] for m in result.matches] == [1, 3]


async def test_mmr_search_drops_the_embedding_column_from_rows() -> None:
    driver = FakeRoutingDriver(_mmr_candidates())

    result = await mmr_search(driver, "app", "docs", "embedding", [1.0, 0.0], k=1)  # type: ignore[arg-type]

    assert "embedding" not in result.matches[0].row
    assert result.matches[0].row == {"id": 1}
    # First pick's relevance is cosine to an identical vector -> 1.0.
    assert result.matches[0].relevance == pytest.approx(1.0)


async def test_mmr_search_parses_bracketed_text_embeddings() -> None:
    # pgvector hands the column back as "[..]" text when no adapter is registered.
    routes = {
        "pg_extension": [{"present": 1}],
        "::vector": [
            {"id": 1, "embedding": "[1.0,0.0]", "mcpg_distance": 0.0},
            {"id": 2, "embedding": "[0.0,1.0]", "mcpg_distance": 1.0},
        ],
    }
    result = await mmr_search(FakeRoutingDriver(routes), "app", "docs", "embedding", [1.0, 0.0], k=2)  # type: ignore[arg-type]

    assert [m.row["id"] for m in result.matches] == [1, 2]


async def test_mmr_search_skips_rows_with_null_embeddings() -> None:
    # A NULL in the embedding column must not crash the whole search;
    # the row is dropped and the rest are reranked normally.
    routes = {
        "pg_extension": [{"present": 1}],
        "::vector": [
            {"id": 1, "embedding": [1.0, 0.0], "mcpg_distance": 0.0},
            {"id": 2, "embedding": None, "mcpg_distance": 0.5},
            {"id": 3, "embedding": [0.0, 1.0], "mcpg_distance": 1.0},
        ],
    }
    result = await mmr_search(FakeRoutingDriver(routes), "app", "docs", "embedding", [1.0, 0.0], k=3)  # type: ignore[arg-type]

    assert [m.row["id"] for m in result.matches] == [1, 3]


async def test_mmr_search_raises_when_an_embedding_dim_does_not_match_query() -> None:
    # A 3-dim row when the query is 2-dim is almost always a schema bug;
    # surface it clearly instead of silently scoring it via truncation.
    routes = {
        "pg_extension": [{"present": 1}],
        "::vector": [
            {"id": 1, "embedding": [1.0, 0.0], "mcpg_distance": 0.0},
            {"id": 2, "embedding": [0.1, 0.2, 0.3], "mcpg_distance": 0.5},
        ],
    }
    with pytest.raises(SearchError, match="dimension 3, expected 2"):
        await mmr_search(FakeRoutingDriver(routes), "app", "docs", "embedding", [1.0, 0.0], k=2)  # type: ignore[arg-type]


async def test_mmr_search_fetch_k_defaults_to_a_wider_pool() -> None:
    driver = FakeRoutingDriver(_mmr_candidates())

    await mmr_search(driver, "app", "docs", "embedding", [1.0, 0.0], k=3)  # type: ignore[arg-type]

    search_call = next(call for call in driver.calls if "::vector" in call[0])
    # default fetch_k = max(4*k, 20) = 20 for k=3; bound as the LIMIT param.
    assert search_call[1][-1] == 20


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"metric": "manhattan"}, "metric"),
        ({"k": 0}, "k must be"),
        ({"lambda_mult": 1.5}, "lambda_mult"),
        ({"k": 5, "fetch_k": 2}, "fetch_k"),
        # Regression for deep-review scalability P0 #1: unbounded
        # fetch_k pulls every candidate's embedding into memory and
        # runs O(pool·k) similarities in pure Python. Cap kicks at
        # 10_000.
        ({"fetch_k": 100_000}, "fetch_k must be ≤"),
        # k itself is also capped: MMR's selection loop is O(k²)
        # because each pick compares against the already-picked set.
        ({"k": 5000}, "k must be ≤"),
    ],
)
async def test_mmr_search_validates_arguments(kwargs: dict[str, object], match: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match=match):
        await mmr_search(driver, "app", "docs", "embedding", [1.0, 0.0], **kwargs)  # type: ignore[arg-type]


async def test_mmr_search_rejects_non_finite_query_vector() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="finite"):
        await mmr_search(driver, "app", "docs", "embedding", [1.0, float("inf")])  # type: ignore[arg-type]


async def test_mmr_search_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver(_mmr_candidates()))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "mmr_search" in listed
        result = await client.call_tool(
            "mmr_search",
            {"schema": "app", "table": "docs", "column": "embedding", "query_vector": [1.0, 0.0], "k": 2},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is True
    assert len(result.structuredContent["matches"]) == 2


def test_parse_embedding_handles_sequences_strings_and_empties() -> None:
    from mcpg.textsearch import _parse_embedding

    assert _parse_embedding([1, 2, 3]) == [1.0, 2.0, 3.0]
    assert _parse_embedding((0.5, 1.5)) == [0.5, 1.5]
    assert _parse_embedding("[1.0, 2.0]") == [1.0, 2.0]
    assert _parse_embedding("[]") == []
    assert _parse_embedding("   ") == []


def test_parse_embedding_rejects_unparseable_types() -> None:
    from mcpg.textsearch import _parse_embedding

    with pytest.raises(SearchError, match="could not parse embedding"):
        _parse_embedding(object())


def test_cosine_similarity_is_zero_for_a_zero_vector() -> None:
    from mcpg.textsearch import _cosine_similarity

    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_raises_on_dim_mismatch() -> None:
    # strict=True guards against silently truncating one vector. Callers
    # are expected to validate dimensions first; this is defence in depth.
    from mcpg.textsearch import _cosine_similarity

    with pytest.raises(ValueError):
        _cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


# --- geo search ------------------------------------------------------------


async def test_geo_search_reports_unavailable_without_postgis() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await geo_search(driver, "app", "places", "location", 1.0, 2.0)  # type: ignore[arg-type]

    assert result.available is False
    assert result.matches == []


async def test_geo_search_returns_rows_without_the_geometry_column() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "ST_MakePoint": [{"id": 1, "name": "cafe", "location": "POINT(...)", "mcpg_distance": 0.5}],
        }
    )

    result = await geo_search(driver, "app", "places", "location", 1.0, 2.0)  # type: ignore[arg-type]

    assert result.available is True
    assert result.matches == [GeoMatch(distance=0.5, row={"id": 1, "name": "cafe"})]


async def test_geo_search_binds_the_point_and_limit() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "ST_MakePoint": []})

    await geo_search(driver, "app", "places", "location", -73.9, 40.7, limit=5)  # type: ignore[arg-type]

    search_call = next(call for call in driver.calls if "ST_MakePoint" in call[0])
    assert search_call[1] == [-73.9, 40.7, -73.9, 40.7, 5]


async def test_geo_search_rejects_invalid_identifiers() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="invalid"):
        await geo_search(driver, "places; DROP", "places", "location", 1.0, 2.0)  # type: ignore[arg-type]


async def test_geo_search_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "geo_search",
            {
                "schema": "app",
                "table": "places",
                "column": "location",
                "longitude": -73.9,
                "latitude": 40.7,
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is False


# --- vector_range_search (11.2) -----------------------------------------


async def test_vector_range_search_reports_unavailable_without_pgvector() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await vector_range_search(driver, "app", "docs", "embedding", [1.0, 2.0], max_distance=0.5)  # type: ignore[arg-type]

    assert result.available is False
    assert result.matches == []


async def test_vector_range_search_returns_rows_within_threshold() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "::vector": [
                {"id": 1, "embedding": [0.1], "mcpg_distance": 0.12},
                {"id": 2, "embedding": [0.2], "mcpg_distance": 0.34},
            ],
        }
    )

    result = await vector_range_search(driver, "app", "docs", "embedding", [1.0, 2.0], max_distance=0.5)  # type: ignore[arg-type]

    assert result.available is True
    distances = [m.distance for m in result.matches]
    assert distances == [0.12, 0.34]
    assert all("embedding" not in m.row for m in result.matches)


async def test_vector_range_search_binds_query_vector_threshold_and_limit() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}], "::vector": []})

    await vector_range_search(driver, "app", "docs", "embedding", [1.0, 2.0], max_distance=0.3, limit=5)  # type: ignore[arg-type]

    search_call = next(call for call in driver.calls if "::vector" in call[0])
    assert search_call[1] == ["[1.0,2.0]", "[1.0,2.0]", 0.3, "[1.0,2.0]", 5]


async def test_vector_range_search_rejects_negative_distance() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="max_distance"):
        await vector_range_search(driver, "app", "docs", "embedding", [1.0], max_distance=-0.1)  # type: ignore[arg-type]


async def test_vector_range_search_rejects_non_finite_distance() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="max_distance"):
        await vector_range_search(driver, "app", "docs", "embedding", [1.0], max_distance=float("inf"))  # type: ignore[arg-type]


async def test_vector_range_search_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "vector_range_search",
            {
                "schema": "app",
                "table": "docs",
                "column": "embedding",
                "query_vector": [1.0, 2.0, 3.0],
                "max_distance": 0.5,
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None


# --- hybrid_search (11.1) ------------------------------------------------


def _make_row(cells):
    class _Row:
        pass

    row = _Row()
    row.cells = cells
    return row


def test_row_key_prefers_id_over_other_columns() -> None:
    assert _row_key({"id": 7, "name": "alpha"}) == ("id", 7)


def test_row_key_falls_back_to_other_id_suffix_when_no_plain_id() -> None:
    assert _row_key({"widget_id": 9, "name": "alpha"}) == ("widget_id", 9)


def test_row_key_falls_back_to_full_tuple_when_no_id_column() -> None:
    key = _row_key({"name": "alpha", "value": 1})
    other = _row_key({"name": "beta", "value": 1})
    assert key != other


def test_fuse_rrf_combines_two_candidate_lists() -> None:
    vec_rows = [_make_row({"id": 1, "embedding": [0.1], "mcpg_rank": 1, "mcpg_distance": 0.05})]
    fts_rows = [_make_row({"id": 1, "body": "alpha", "mcpg_rank": 1, "mcpg_rank_score": 0.9})]

    result = _fuse_rrf(vec_rows, fts_rows, "embedding", "body", rrf_k=60, limit=10)

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.vector_rank == 1 and match.fts_rank == 1
    assert match.rrf_score == pytest.approx(2.0 / 61)


def test_fuse_rrf_includes_rows_seen_in_only_one_source() -> None:
    vec_rows = [_make_row({"id": 1, "embedding": [0.1], "mcpg_rank": 1, "mcpg_distance": 0.05})]
    fts_rows = [_make_row({"id": 2, "body": "alpha", "mcpg_rank": 1, "mcpg_rank_score": 0.9})]

    result = _fuse_rrf(vec_rows, fts_rows, "embedding", "body", rrf_k=60, limit=10)

    keys = {match.row.get("id") for match in result.matches}
    assert keys == {1, 2}


def test_fuse_rrf_sorts_by_descending_rrf_score_and_respects_limit() -> None:
    vec_rows = [
        _make_row({"id": 1, "embedding": [0.1], "mcpg_rank": 1, "mcpg_distance": 0.01}),
        _make_row({"id": 2, "embedding": [0.2], "mcpg_rank": 2, "mcpg_distance": 0.02}),
    ]
    fts_rows = [_make_row({"id": 3, "body": "x", "mcpg_rank": 1, "mcpg_rank_score": 1.0})]

    result = _fuse_rrf(vec_rows, fts_rows, "embedding", "body", rrf_k=60, limit=2)

    assert len(result.matches) == 2
    scores = [m.rrf_score for m in result.matches]
    assert scores == sorted(scores, reverse=True)


async def test_hybrid_search_reports_unavailable_without_pgvector() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await hybrid_search(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "body",
        [1.0, 2.0],
        "search query",
    )

    assert isinstance(result, HybridSearchResult)
    assert result.available is False
    assert result.matches == []


async def test_hybrid_search_rejects_non_positive_candidate_pool() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="candidate_pool"):
        await hybrid_search(
            driver,  # type: ignore[arg-type]
            "app",
            "docs",
            "embedding",
            "body",
            [1.0],
            "x",
            candidate_pool=0,
        )


async def test_hybrid_search_rejects_non_positive_rrf_k() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="rrf_k"):
        await hybrid_search(
            driver,  # type: ignore[arg-type]
            "app",
            "docs",
            "embedding",
            "body",
            [1.0],
            "x",
            rrf_k=0,
        )


async def test_hybrid_search_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "hybrid_search",
            {
                "schema": "app",
                "table": "docs",
                "vector_column": "embedding",
                "text_column": "body",
                "query_vector": [1.0, 2.0, 3.0],
                "text_query": "anything",
            },
        )

    assert result.isError is False


# --- recommend_vector_quantization (11.3) -------------------------------


def test_suggest_quantization_skips_already_quantized_columns() -> None:
    assert (
        _suggest_quantization(
            schema="app",
            table="docs",
            column="embedding",
            current_type="halfvec",
            dimension=768,
            row_count=1_000_000,
        )
        is None
    )


def test_suggest_quantization_skips_small_low_dim_tables() -> None:
    assert (
        _suggest_quantization(
            schema="app",
            table="docs",
            column="embedding",
            current_type="vector",
            dimension=384,
            row_count=100,
        )
        is None
    )


def test_suggest_quantization_recommends_halfvec_for_high_dim_with_meaningful_rows() -> None:
    rec = _suggest_quantization(
        schema="app",
        table="docs",
        column="embedding",
        current_type="vector",
        dimension=768,
        row_count=50_000,
    )
    assert rec is not None
    assert rec.suggested_type == "halfvec"
    assert rec.suggested_bytes == rec.current_bytes // 2
    assert 0.49 < rec.savings_ratio < 0.51
    assert "halfvec" in rec.rationale


def test_suggest_quantization_recommends_when_total_storage_clears_threshold() -> None:
    rec = _suggest_quantization(
        schema="app",
        table="docs",
        column="embedding",
        current_type="vector",
        dimension=384,
        row_count=80_000,
    )
    assert rec is not None
    assert rec.current_bytes >= 100 * 1024 * 1024


async def test_recommend_vector_quantization_reports_empty_without_pgvector() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await recommend_vector_quantization(driver, "app")  # type: ignore[arg-type]

    assert result == []


async def test_recommend_vector_quantization_rejects_unsafe_schema_names() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(SearchError, match="invalid"):
        await recommend_vector_quantization(driver, 'app"; DROP TABLE x; --')  # type: ignore[arg-type]


async def test_recommend_vector_quantization_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("recommend_vector_quantization", {"schema": "app"})

    assert result.isError is False


def test_quantization_recommendation_dataclass_shape() -> None:
    rec = QuantizationRecommendation(
        schema="app",
        table="docs",
        column="embedding",
        dimension=768,
        row_count=10_000,
        current_type="vector",
        current_bytes=30_720_000,
        suggested_type="halfvec",
        suggested_bytes=15_360_000,
        savings_ratio=0.5,
        rationale="halfvec halves storage",
    )
    assert rec.dimension == 768
    assert rec.savings_ratio == 0.5


def test_hybrid_match_dataclass_shape() -> None:
    match = HybridMatch(
        rrf_score=0.5,
        vector_rank=1,
        fts_rank=None,
        vector_distance=0.1,
        fts_rank_score=None,
        row={"id": 1},
    )
    assert match.vector_rank == 1
    assert match.fts_rank is None
