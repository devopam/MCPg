"""Integration tests for Apache AGE graph tools against a live PostgreSQL database."""

from collections.abc import AsyncIterator

import pytest

from mcpg.config import AccessMode, Settings
from mcpg.context import AppContext
from mcpg.cypher import run_cypher
from mcpg.database import Database
from mcpg.graph import describe_graph, list_graphs
from mcpg.graph_diagram import generate_graph_diagram
from mcpg.graph_mgmt import create_graph, drop_graph


@pytest.fixture
async def age_extension_check(connected_database: Database) -> None:
    """Verify Apache AGE extension is installed in the test database, or skip the tests."""
    driver = connected_database.driver()
    # Ensure age extension is created in pg
    try:
        await driver.execute_query("CREATE EXTENSION IF NOT EXISTS age CASCADE;", force_readonly=False)
        await driver.execute_query("LOAD 'age';")
    except Exception as exc:
        pytest.skip(f"Apache AGE extension is not available or could not be loaded: {exc}")


@pytest.fixture
async def integration_graph(
    connected_database: Database,
    database_url: str,
    age_extension_check: None,
) -> AsyncIterator[str]:
    """Provide a throwaway graph space for integration testing."""
    graph_name = "it_graph"
    settings = Settings(
        database_url=database_url,
        access_mode=AccessMode.UNRESTRICTED,
        allow_ddl=True,
    )
    context = AppContext(
        settings=settings,
        database=connected_database,
        listen_manager=None,  # type: ignore[arg-type]
        cursor_manager=None,  # type: ignore[arg-type]
    )

    # Teardown any leftover graph space
    try:
        await drop_graph(context, graph_name, cascade=True)
    except Exception:
        pass

    # Create fresh graph
    await create_graph(context, graph_name)

    try:
        yield graph_name
    finally:
        # Cleanup
        try:
            await drop_graph(context, graph_name, cascade=True)
        except Exception:
            pass


async def test_graph_lifecycle_and_cypher_queries(
    connected_database: Database,
    database_url: str,
    integration_graph: str,
) -> None:
    graph_name = integration_graph
    settings = Settings(
        database_url=database_url,
        access_mode=AccessMode.UNRESTRICTED,
        allow_ddl=True,
    )
    context = AppContext(
        settings=settings,
        database=connected_database,
        listen_manager=None,  # type: ignore[arg-type]
        cursor_manager=None,  # type: ignore[arg-type]
    )

    # 1. list_graphs should contain our integration_graph
    graphs = await list_graphs(context)
    assert any(g["name"] == graph_name for g in graphs)

    # 2. create nodes
    c_res = await run_cypher(context, graph_name, "CREATE (a:Person {name: 'Charlie', age: 30}) RETURN a")
    assert c_res["row_count"] == 1
    assert c_res["columns"] == ["a"]
    node = c_res["rows"][0]["a"]
    assert node["label"] == "Person"
    assert node["properties"]["name"] == "Charlie"
    assert node["properties"]["age"] == 30

    # Create Dennis
    d_res = await run_cypher(context, graph_name, "CREATE (b:Person {name: 'Dennis', age: 31}) RETURN b")
    assert d_res["row_count"] == 1

    # 3. Create relationship KNOWS between them
    r_res = await run_cypher(
        context,
        graph_name,
        "MATCH (a:Person {name: 'Charlie'}), (b:Person {name: 'Dennis'}) "
        "CREATE (a)-[r:KNOWS {since: 2021}]->(b) "
        "RETURN r",
    )
    assert r_res["row_count"] == 1
    edge = r_res["rows"][0]["r"]
    assert edge["label"] == "KNOWS"
    assert edge["properties"]["since"] == 2021

    # 4. Describe the graph
    desc = await describe_graph(context, graph_name)
    assert desc["name"] == graph_name
    assert desc["total_vertices"] == 2
    assert desc["total_edges"] == 1

    # Check vertex labels
    person_label_stats = next(x for x in desc["vertex_labels"] if x["label"] == "Person")
    assert person_label_stats["count"] == 2

    # Check edge labels
    knows_label_stats = next(x for x in desc["edge_labels"] if x["label"] == "KNOWS")
    assert knows_label_stats["count"] == 1

    # 5. Generate graph diagram
    diag_res = await generate_graph_diagram(context, graph_name)
    assert diag_res["graph_name"] == graph_name
    mermaid = diag_res["mermaid"]
    assert "flowchart TD" in mermaid
    assert "subgraph Person_nodes [Person Nodes]" in mermaid
    assert "v" in mermaid  # contains node IDs
    assert "KNOWS" in mermaid  # contains edge relationship
