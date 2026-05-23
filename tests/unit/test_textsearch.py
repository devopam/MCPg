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
    SearchError,
    VectorMatch,
    full_text_search,
    fuzzy_search,
    geo_search,
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
