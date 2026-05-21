"""Tests for index recommendations and the recommend_indexes tool."""

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.indexing import IndexRecommendation, IndexSuggestion, recommend_indexes
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


def _row(column: str, data_type: str) -> dict[str, object]:
    """One (candidate table x column) row for the orders table."""
    return {
        "schemaname": "app",
        "relname": "orders",
        "seq_scan": 5000,
        "n_live_tup": 250000,
        "column_name": column,
        "data_type": data_type,
    }


async def test_recommend_indexes_groups_columns_into_one_table_recommendation() -> None:
    driver = FakeDriver([_row("id", "integer"), _row("payload", "jsonb")])

    result = await recommend_indexes(driver)

    assert result == [
        IndexRecommendation(
            schema="app",
            table="orders",
            seq_scans=5000,
            live_tuples=250000,
            reason="large table read mostly by sequential scan",
            suggestions=[IndexSuggestion("payload", "gin", "GIN supports jsonb containment and key lookups")],
        )
    ]


@pytest.mark.parametrize("data_type", ["text", "character varying", "character"])
async def test_recommend_indexes_suggests_trigram_gin_for_text_columns(data_type: str) -> None:
    result = await recommend_indexes(FakeDriver([_row("name", data_type)]))

    assert result[0].suggestions == [
        IndexSuggestion("name", "gin_trgm", "trigram GIN (pg_trgm) accelerates LIKE/ILIKE pattern search")
    ]


async def test_recommend_indexes_suggests_gin_for_array_columns() -> None:
    result = await recommend_indexes(FakeDriver([_row("tags", "ARRAY")]))

    assert result[0].suggestions == [IndexSuggestion("tags", "gin", "GIN supports array membership queries")]


async def test_recommend_indexes_makes_no_suggestions_for_plain_scalar_columns() -> None:
    result = await recommend_indexes(FakeDriver([_row("id", "integer")]))

    assert result[0].suggestions == []


async def test_recommend_indexes_returns_empty_when_nothing_qualifies() -> None:
    assert await recommend_indexes(FakeDriver([])) == []


async def test_recommend_indexes_binds_the_threshold_as_a_parameter() -> None:
    driver = FakeDriver([])

    await recommend_indexes(driver, min_live_tuples=500)

    assert driver.calls[0][1] == [500]


async def test_recommend_indexes_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("recommend_indexes", {})

    assert result.isError is False
