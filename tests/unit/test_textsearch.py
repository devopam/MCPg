"""Tests for fuzzy text search and the fuzzy_search tool."""

import pytest
from _fakes import FakeDatabase, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.textsearch import FuzzyMatch, SearchError, fuzzy_search

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
