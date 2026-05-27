"""Apache AGE Graph Schema Visualisation.

This module provides the ``generate_graph_diagram`` tool to render
a property graph's topology and nodes visually as a Mermaid diagram.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from mcpg.context import AppContext
from mcpg.database import DatabaseError
from mcpg.policy import Capability, check_permission

logger = logging.getLogger(__name__)


class DiagramResult(TypedDict):
    """The result of graph diagram generation."""
    graph_name: str
    mermaid: str


async def generate_graph_diagram(
    context: AppContext,
    graph_name: str,
    limit: int = 50,
) -> DiagramResult:
    """Generate a Mermaid flowchart diagram representing nodes and relationships in a graph.

    Args:
        graph_name: The property graph to visualize.
        limit: Hard limit of vertices/edges to render to prevent huge diagram sizes.
    """
    # 1. Validate name
    if not graph_name.replace("_", "").isalnum() or graph_name[0].isdigit():
        raise ValueError(f"invalid graph name: {graph_name!r}")

    # 2. Check read permission
    check_permission(Capability.READ, context.settings.access_mode)

    driver = context.database.driver()

    # 3. Verify graph exists
    try:
        graph_rows = await driver.execute_query(
            "SELECT name FROM ag_catalog.ag_graph WHERE name = %s;",
            [graph_name],
        )
    except Exception as exc:
        raise DatabaseError("could not query graph metadata") from exc

    if not graph_rows:
        raise ValueError(f"graph {graph_name!r} does not exist")

    await driver.execute_query("LOAD 'age';")
    await driver.execute_query(f"SET search_path = {graph_name}, ag_catalog, public;")

    # 4. Fetch labels in the graph
    try:
        label_rows = await driver.execute_query(
            "SELECT name, kind FROM ag_catalog.ag_label "
            "WHERE graph = (SELECT graphid FROM ag_catalog.ag_graph WHERE name = %s);",
            [graph_name],
        )
    except Exception as exc:
        raise DatabaseError("could not query graph labels") from exc

    # Map label names
    vertex_tables: list[str] = []
    edge_tables: list[str] = []
    for row in label_rows or []:
        name = str(row.cells["name"])
        kind = str(row.cells["kind"])
        if name.startswith("_ag_label"):
            continue
        if kind == "v":
            vertex_tables.append(name)
        elif kind == "e":
            edge_tables.append(name)

    # 5. Fetch vertices
    nodes: list[dict[str, Any]] = []
    for tbl in vertex_tables:
        if len(nodes) >= limit:
            break
        try:
            v_rows = await driver.execute_query(
                f'SELECT id, properties::text as props FROM "{graph_name}"."{tbl}" LIMIT %s;',
                [limit - len(nodes)],
            )
            for vr in v_rows or []:
                raw_props = vr.cells.get("props") or "{}"
                props = json.loads(str(raw_props))
                nodes.append({
                    "id": int(vr.cells["id"]),
                    "label": tbl,
                    "name": props.get("name") or props.get("title") or f"id:{vr.cells['id']}",
                })
        except Exception as exc:
            logger.warning("failed to fetch vertices from label %s: %s", tbl, exc)

    # 6. Fetch edges
    edges: list[dict[str, Any]] = []
    for tbl in edge_tables:
        if len(edges) >= limit:
            break
        try:
            e_rows = await driver.execute_query(
                f'SELECT start_id, end_id, properties::text as props FROM "{graph_name}"."{tbl}" LIMIT %s;',
                [limit - len(edges)],
            )
            for er in e_rows or []:
                edges.append({
                    "start_id": int(er.cells["start_id"]),
                    "end_id": int(er.cells["end_id"]),
                    "label": tbl,
                })
        except Exception as exc:
            logger.warning("failed to fetch edges from label %s: %s", tbl, exc)

    # 7. Render Mermaid Flowchart
    lines = ["flowchart TD"]

    # Render nodes: v<id>["Label: Name"]
    # Group by label to style them in subgraphs
    labels_groups: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        labels_groups.setdefault(n["label"], []).append(n)

    # Render vertices
    for label, group in labels_groups.items():
        lines.append(f"  subgraph {label}_nodes [{label} Nodes]")
        for n in group:
            name_escaped = str(n["name"]).replace('"', '\\"')
            lines.append(f'    v{n["id"]}["{name_escaped}"]')
        lines.append("  end")

    # Render directed edge arrows
    # Only render if both endpoints exist in our fetched vertex set
    node_ids = {n["id"] for n in nodes}
    for e in edges:
        if e["start_id"] in node_ids and e["end_id"] in node_ids:
            lines.append(f'  v{e["start_id"]} -->|{e["label"]}| v{e["end_id"]}')

    mermaid = "\n".join(lines)
    return DiagramResult(
        graph_name=graph_name,
        mermaid=mermaid,
    )
