"""Unit tests for Apache AGE graph visualizer module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeRoutingDriver

from mcpg.config import Settings
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.graph_diagram import generate_graph_diagram
from mcpg.listen import ListenManager


async def test_generate_graph_diagram_validates_inputs() -> None:
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
        await generate_graph_diagram(context, "my-graph-with-dashes")


async def test_generate_graph_diagram_raises_error_if_not_exists() -> None:
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
        await generate_graph_diagram(context, "my_graph")


async def test_generate_graph_diagram_renders_mermaid() -> None:
    # Put ag_label first to prevent partial substring match of ag_graph
    fake_routing = FakeRoutingDriver(
        {
            "ag_label": [
                {"name": "Person", "kind": "v"},
                {"name": "KNOWS", "kind": "e"},
            ],
            "ag_graph": [{"name": "my_graph"}],
            'FROM "my_graph"."Person"': [
                {"id": 844424930131969, "props": '{"name": "Charlie"}'},
                {"id": 844424930131970, "props": '{"name": "Dennis"}'},
            ],
            'FROM "my_graph"."KNOWS"': [
                {"start_id": 844424930131969, "end_id": 844424930131970, "props": "{}"},
            ],
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

    res = await generate_graph_diagram(context, "my_graph")
    assert res["graph_name"] == "my_graph"
    mermaid = res["mermaid"]
    assert "flowchart TD" in mermaid
    assert "subgraph Person_nodes [Person Nodes]" in mermaid
    assert 'v844424930131969["Charlie"]' in mermaid
    assert 'v844424930131970["Dennis"]' in mermaid
    assert "v844424930131969 -->|KNOWS| v844424930131970" in mermaid
