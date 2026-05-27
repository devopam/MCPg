"""Unit tests for Apache AGE Cypher query execution module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeRoutingDriver

from mcpg.config import AccessMode, Settings
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.cypher import parse_return_columns, run_cypher
from mcpg.listen import ListenManager
from mcpg.policy import PermissionError


def test_parse_return_columns() -> None:
    assert parse_return_columns("MATCH (n) RETURN n") == ["n"]
    assert parse_return_columns("MATCH (n) RETURN n.name") == ["name"]
    assert parse_return_columns("MATCH (a)-[r]->(b) RETURN a, r, b.name AS alias_name") == ["a", "r", "alias_name"]
    # Case insensitivity
    assert parse_return_columns("match (a) return a.age as age, a.friend_count") == ["age", "friend_count"]
    # No return clause
    assert parse_return_columns("CREATE (n)") == ["result"]


async def test_run_cypher_validates_inputs() -> None:
    fake_routing = FakeRoutingDriver({})
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    with pytest.raises(ValueError, match="invalid graph name"):
        await run_cypher(context, "my-graph-with-dashes", "MATCH (n) RETURN n")


async def test_run_cypher_checks_graph_existence() -> None:
    fake_routing = FakeRoutingDriver({"ag_graph": []})
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    with pytest.raises(ValueError, match="graph 'my_graph' does not exist"):
        await run_cypher(context, "my_graph", "MATCH (n) RETURN n")


async def test_run_cypher_enforces_read_only_access_mode() -> None:
    fake_routing = FakeRoutingDriver({"ag_graph": [{"name": "my_graph"}]})
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url, access_mode=AccessMode.READ_ONLY)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    # Modifying Cypher query containing CREATE
    with pytest.raises(PermissionError, match="requires WRITE capability"):
        await run_cypher(context, "my_graph", "CREATE (n:Person {name: 'Charlie'})")


async def test_run_cypher_executes_successfully() -> None:
    fake_routing = FakeRoutingDriver(
        {
            "ag_graph": [{"name": "my_graph"}],
            "SELECT * FROM cypher": [
                {
                    "person": '{"id": 844424930131969, "label": "Person", "properties": {"name": "Charlie"}}::vertex',
                    "friend": '{"id": 844424930131970, "label": "Person", "properties": {"name": "Dennis"}}::vertex',
                }
            ],
        }
    )
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url, access_mode=AccessMode.UNRESTRICTED)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    res = await run_cypher(context, "my_graph", "MATCH (person)-[:KNOWS]->(friend) RETURN person, friend")
    assert res["row_count"] == 1
    assert res["columns"] == ["person", "friend"]
    assert res["rows"][0]["person"]["properties"]["name"] == "Charlie"
    assert res["rows"][0]["friend"]["properties"]["name"] == "Dennis"

    # Verify session setup calls occurred
    assert any("LOAD 'age'" in c[0] for c in fake_routing.calls)
    assert any("SET search_path = my_graph, ag_catalog, public" in c[0] for c in fake_routing.calls)
