"""Unit tests for Apache AGE graph visualizer module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeRoutingDriver

from mcpg.config import AccessMode, Settings
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.graph import GraphError
from mcpg.graph_diagram import generate_graph_diagram
from mcpg.listen import ListenManager
from mcpg.policy import Capability


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

    with pytest.raises(GraphError, match="invalid graph name"):
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

    with pytest.raises(GraphError, match="graph 'my_graph' does not exist"):
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

    # The per-label vertex/edge fetch queries must run READ ONLY.
    vertex_calls = [c for c in fake_routing.calls if 'FROM "my_graph"."Person"' in c[0]]
    edge_calls = [c for c in fake_routing.calls if 'FROM "my_graph"."KNOWS"' in c[0]]
    assert vertex_calls and vertex_calls[0][2] is True
    assert edge_calls and edge_calls[0][2] is True


async def test_generate_graph_diagram_rejects_malicious_label_name() -> None:
    """A label name read back from ag_catalog.ag_label is not identifier-safe.

    generate_graph_diagram interpolates the label name directly into an
    f-string SQL query (``FROM "{graph_name}"."{tbl}"``) and into the
    generated Mermaid text, so a label such as
    ``Person"; DROP TABLE secrets; --`` must be rejected up front rather
    than silently reaching either sink.
    """
    malicious_name = 'Person"; DROP TABLE secrets; --'
    fake_routing = FakeRoutingDriver(
        {
            "ag_label": [
                {"name": malicious_name, "kind": "v"},
            ],
            "ag_graph": [{"name": "my_graph"}],
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

    with pytest.raises(GraphError, match="invalid label name"):
        await generate_graph_diagram(context, "my_graph")

    # No query built from the malicious label name may have reached the driver.
    assert not any("DROP TABLE" in str(call[0]) for call in fake_routing.calls)


async def test_generate_graph_diagram_enforces_access_mode_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_graph_diagram already gates on Capability.READ (graph_diagram.py:44);
    this is a regression test confirming it stays wired to check_permission,
    mirroring the equivalent test added for graph.describe_graph.
    """
    calls: list[tuple[Capability, AccessMode]] = []

    def _spy(capability: Capability, access_mode: AccessMode) -> None:
        calls.append((capability, access_mode))

    monkeypatch.setattr("mcpg.graph_diagram.check_permission", _spy)

    fake_routing = FakeRoutingDriver(
        {
            "ag_label": [],
            "ag_graph": [{"name": "my_graph"}],
        }
    )
    fake_db = FakeDatabase(fake_routing)  # type: ignore[arg-type]
    url = "postgresql://localhost/db"
    settings = Settings(database_url=url, access_mode=AccessMode.READ_ONLY)
    context = AppContext(
        settings=settings,
        database=fake_db,  # type: ignore[arg-type]
        listen_manager=ListenManager(url),
        cursor_manager=CursorManager(url),
    )

    await generate_graph_diagram(context, "my_graph")

    assert calls == [(Capability.READ, AccessMode.READ_ONLY)]
