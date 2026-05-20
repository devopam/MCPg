"""Tests for index recommendations and the recommend_indexes tool."""

from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.indexing import IndexRecommendation, recommend_indexes
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_recommend_indexes_maps_candidate_tables() -> None:
    driver = FakeDriver([{"schemaname": "app", "relname": "orders", "seq_scan": 5000, "n_live_tup": 250000}])

    result = await recommend_indexes(driver)

    assert result == [
        IndexRecommendation(
            schema="app",
            table="orders",
            seq_scans=5000,
            live_tuples=250000,
            reason="large table read mostly by sequential scan",
        )
    ]


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
