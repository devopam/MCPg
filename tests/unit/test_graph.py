"""Unit tests for Apache AGE graph module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver

from mcpg.config import Settings
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.database import DatabaseError
from mcpg.graph import describe_graph, list_graphs, parse_agtype
from mcpg.listen import ListenManager


def test_parse_agtype_strips_vertex_edge_path() -> None:
    # Vertex shape
    v_str = '{"id": 844424930131969, "label": "Person", "properties": {"name": "Charlie"}}::vertex'
    parsed_v = parse_agtype(v_str)
    assert isinstance(parsed_v, dict)
    assert parsed_v["id"] == 844424930131969
    assert parsed_v["label"] == "Person"
    assert parsed_v["properties"]["name"] == "Charlie"

    # Edge shape
    e_str = (
        '{"id": 1125899906842625, "label": "KNOWS", "end_id": 844424930131970, '
        '"start_id": 844424930131969, "properties": {"since": 2020}}::edge'
    )
    parsed_e = parse_agtype(e_str)
    assert isinstance(parsed_e, dict)
    assert parsed_e["label"] == "KNOWS"
    assert parsed_e["properties"]["since"] == 2020

    # Path shape
    p_str = (
        '[{"id": 844424930131969, "label": "Person", "properties": {"name": "Charlie"}}::vertex, '
        '{"id": 1125899906842625, "label": "KNOWS", "end_id": 844424930131970, '
        '"start_id": 844424930131969, "properties": {"since": 2020}}::edge, '
        '{"id": 844424930131970, "label": "Person", "properties": {"name": "Dennis"}}::vertex]::path'
    )
    parsed_p = parse_agtype(p_str)
    assert isinstance(parsed_p, list)
    assert len(parsed_p) == 3
    assert parsed_p[0]["label"] == "Person"
    assert parsed_p[1]["label"] == "KNOWS"
    assert parsed_p[2]["label"] == "Person"

    # Plain types
    assert parse_agtype(123) == 123
    assert parse_agtype("hello") == "hello"

    # Suffix substring inside string properties must be preserved
    corrupt_str = (
        '{"id": 1, "properties": {"desc": "This is a ::vertex inside string", "note": "some ::edge note"}}::vertex'
    )
    parsed_corrupt = parse_agtype(corrupt_str)
    assert isinstance(parsed_corrupt, dict)
    assert parsed_corrupt["properties"]["desc"] == "This is a ::vertex inside string"
    assert parsed_corrupt["properties"]["note"] == "some ::edge note"


async def test_list_graphs_raises_database_error_when_missing() -> None:
    fake_driver = FakeDriver(fail=True)
    fake_db = FakeDatabase(fake_driver)
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    with pytest.raises(DatabaseError, match="Apache AGE is not enabled in this database"):
        await list_graphs(context)


async def test_list_graphs_returns_graphs_when_installed() -> None:
    fake_driver = FakeDriver([{"graphid": 16555, "name": "my_graph", "namespace": "my_graph"}])
    fake_db = FakeDatabase(fake_driver)
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    graphs = await list_graphs(context)
    assert len(graphs) == 1
    assert graphs[0]["graphid"] == 16555
    assert graphs[0]["name"] == "my_graph"
    assert graphs[0]["namespace"] == "my_graph"


async def test_describe_graph_validates_inputs() -> None:
    fake_driver = FakeDriver()
    fake_db = FakeDatabase(fake_driver)
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    with pytest.raises(ValueError, match="invalid graph name"):
        await describe_graph(context, "my-graph-with-dashes")

    with pytest.raises(ValueError, match="invalid graph name"):
        await describe_graph(context, "1startwithdigit")


async def test_describe_graph_raises_error_if_not_exists() -> None:
    # No rows returned for ag_graph check
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
        await describe_graph(context, "my_graph")


async def test_describe_graph_fetches_stats() -> None:
    # Put ag_label first to prevent partial substring match of ag_graph
    fake_routing = FakeRoutingDriver(
        {
            "ag_label": [
                {"name": "Person", "kind": "v"},
                {"name": "KNOWS", "kind": "e"},
                {"name": "_ag_label_vertex", "kind": "v"},  # should be skipped
            ],
            "ag_graph": [{"name": "my_graph", "namespace": "my_graph"}],
            '"my_graph"."Person"': [{"cnt": 10}],
            '"my_graph"."KNOWS"': [{"cnt": 15}],
        }
    )
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    stats = await describe_graph(context, "my_graph")
    assert stats["name"] == "my_graph"
    assert stats["total_vertices"] == 10
    assert stats["total_edges"] == 15
    assert len(stats["vertex_labels"]) == 1
    assert stats["vertex_labels"][0]["label"] == "Person"
    assert stats["vertex_labels"][0]["count"] == 10
    assert len(stats["edge_labels"]) == 1
    assert stats["edge_labels"][0]["label"] == "KNOWS"
    assert stats["edge_labels"][0]["count"] == 15
