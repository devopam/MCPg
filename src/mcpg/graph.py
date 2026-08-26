"""Apache AGE Graph Introspection and Parsing.

This module provides tools for listing and describing property graphs,
and a high-performance, dependency-free parser for Apache AGE's custom
``agtype`` data type.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from mcpg.context import AppContext
from mcpg.database import DatabaseError
from mcpg.errors import MCPgError
from mcpg.policy import Capability, check_permission


class GraphError(MCPgError):
    """Raised on invalid input or invariant violations across the
    Apache AGE graph integration.

    Shared by :mod:`mcpg.graph`, :mod:`mcpg.cypher`,
    :mod:`mcpg.graph_mgmt`, and :mod:`mcpg.graph_diagram` — every
    public-facing wrapper raises this rather than a bare
    :class:`ValueError`, so callers get one consistent error class
    to catch (matches the ``*Error``-per-module convention every
    other surface follows: PgSearchError, TurboQuantError,
    SecretsError, …).
    """


# Catalog-derived identifiers (label/table names read back from
# ``ag_catalog.ag_label``) are NOT validated by Postgres/AGE — a quoted
# identifier can contain arbitrary characters, including embedded double
# quotes. Every such name is checked against this pattern immediately
# before it is interpolated into a SQL string, mirroring
# ``graph_projection._check_identifier``.
_LABEL_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _check_label_identifier(name: str) -> None:
    """Raise :class:`GraphError` if ``name`` is not a plain SQL identifier.

    Guards every catalog-derived label/table name (from ``ag_label``)
    before it is interpolated into an f-string SQL query. Aborts the
    whole call rather than silently skipping the offending label — same
    precedent as ``graph_projection.generate_graph_projection``, which
    raises on the first invalid catalog-derived table name instead of
    dropping it and continuing.
    """
    if not _LABEL_IDENTIFIER.match(name):
        raise GraphError(f"invalid label name {name!r}; must match [A-Za-z_][A-Za-z0-9_]*")


class GraphInfo(TypedDict):
    """Structured information about an Apache AGE graph."""

    graphid: int
    name: str
    namespace: str


class LabelStats(TypedDict):
    """Vertex or Edge label node count stats."""

    label: str
    count: int


class GraphDescription(TypedDict):
    """Detailed structural statistics and labels in a graph."""

    name: str
    vertex_labels: list[LabelStats]
    edge_labels: list[LabelStats]
    total_vertices: int
    total_edges: int


def parse_agtype(val: Any) -> Any:
    """Parse raw ``agtype`` string results into standard Python dicts/lists.

    Apache AGE returns graph elements (vertices, edges, paths) as custom
    serialized strings suffixed with type descriptors, e.g.::

        '{"id": 844424930131969, "label": "Person", "properties": {"name": "Charlie"}}::vertex'

    This helper strips those type suffixes recursively and parses the clean
    payload using standard ``json.loads``.
    """
    if isinstance(val, str):
        if val.endswith("::vertex") or val.endswith("::edge") or val.endswith("::path"):
            # Split by unescaped double quotes to safely strip suffixes only outside string literals
            parts = re.split(r'(?<!\\)"', val)
            for i in range(len(parts)):
                if i % 2 == 0:
                    parts[i] = parts[i].replace("::vertex", "").replace("::edge", "").replace("::path", "")
            clean = '"'.join(parts)
            try:
                return json.loads(clean)
            except json.JSONDecodeError:
                return val
    return val


async def list_graphs(context: AppContext) -> list[GraphInfo]:
    """List all active Apache AGE graphs in the database.

    Returns a list of graphs including their IDs, names, and schemas.
    Raises DatabaseError if the Apache AGE extension is not installed.
    """
    driver = context.database.driver()
    try:
        rows = await driver.execute_query("""
            SELECT graphid, name, namespace
            FROM ag_catalog.ag_graph;
        """)
    except Exception as exc:
        raise DatabaseError("Apache AGE is not enabled in this database. Ensure CREATE EXTENSION age is run.") from exc

    return [
        GraphInfo(
            graphid=int(row.cells["graphid"]),
            name=str(row.cells["name"]),
            namespace=str(row.cells["namespace"]),
        )
        for row in rows or []
    ]


async def describe_graph(context: AppContext, graph_name: str) -> GraphDescription:
    """Describe the schema structure, vertex labels, and edge labels of a graph.

    Args:
        graph_name: The target graph to inspect.
    """
    # Defensive quoting validation
    if not graph_name.replace("_", "").isalnum() or graph_name[0].isdigit():
        raise GraphError(f"invalid graph name: {graph_name!r}")

    # Read-only introspection — same capability gate as the other graph
    # read tools (cypher.run_cypher's read path, graph_diagram.generate_graph_diagram).
    check_permission(Capability.READ, context.settings.access_mode)

    driver = context.database.driver()

    # 1. Fetch graph metadata to ensure it exists
    try:
        graph_rows = await driver.execute_query(
            "SELECT name, namespace FROM ag_catalog.ag_graph WHERE name = %s;",
            [graph_name],
        )
    except Exception as exc:
        raise DatabaseError("could not query graph metadata") from exc

    if not graph_rows:
        raise GraphError(f"graph {graph_name!r} does not exist")

    # Load age to ensure graph functions are visible
    await driver.execute_query("LOAD 'age';")
    await driver.execute_query(f"SET search_path = {graph_name}, ag_catalog, public;")

    # 2. Get vertex labels and counts
    # Apache AGE vertices reside in tables matching label names under the graph's schema.
    # We query ag_catalog.ag_label to find vertex labels ('v' kind) vs edge labels ('e' kind).
    try:
        label_rows = await driver.execute_query(
            "SELECT name, kind FROM ag_catalog.ag_label "
            "WHERE graph = (SELECT graphid FROM ag_catalog.ag_graph WHERE name = %s);",
            [graph_name],
        )
    except Exception as exc:
        raise DatabaseError("could not query graph labels") from exc

    vertex_labels: list[LabelStats] = []
    edge_labels: list[LabelStats] = []
    total_vertices = 0
    total_edges = 0

    for row in label_rows or []:
        name = str(row.cells["name"])
        kind = str(row.cells["kind"])
        # AGE internal schemas hold vertex tables named after the labels,
        # but we must avoid internal '_ag_label_vertex' and '_ag_label_edge' tables.
        if name.startswith("_ag_label"):
            continue
        # Catalog-derived name — validated before it reaches the f-string
        # below. Raises (aborting describe_graph) rather than silently
        # skipping, so a corrupted/malicious label surfaces as an error
        # instead of a quietly wrong report.
        _check_label_identifier(name)

        try:
            # Query row counts of the backing label table under the graph's schema
            count_rows = await driver.execute_query(
                f'SELECT COALESCE(COUNT(*), 0) as cnt FROM "{graph_name}"."{name}";',
                force_readonly=True,
            )
            cnt = int(count_rows[0].cells["cnt"]) if count_rows else 0
        except Exception:
            cnt = 0

        stats = LabelStats(label=name, count=cnt)
        if kind == "v":
            vertex_labels.append(stats)
            total_vertices += cnt
        elif kind == "e":
            edge_labels.append(stats)
            total_edges += cnt

    return GraphDescription(
        name=graph_name,
        vertex_labels=sorted(vertex_labels, key=lambda x: x["label"]),
        edge_labels=sorted(edge_labels, key=lambda x: x["label"]),
        total_vertices=total_vertices,
        total_edges=total_edges,
    )
