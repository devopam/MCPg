"""Apache AGE Graph Management.

This module provides tools for creating and dropping graph spaces,
gated under the DDL capability.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from mcpg.context import AppContext
from mcpg.database import DatabaseError
from mcpg.graph import GraphError
from mcpg.policy import Capability, check_permission

logger = logging.getLogger(__name__)


class GraphMgmtResult(TypedDict):
    """The result of a graph management operation."""

    graph_name: str
    status: str
    message: str


async def create_graph(context: AppContext, graph_name: str) -> GraphMgmtResult:
    """Create a new property graph space in the database.

    Args:
        graph_name: The name of the new graph to create.
    """
    # 1. Validate name
    if not graph_name.replace("_", "").isalnum() or graph_name[0].isdigit():
        raise GraphError(f"invalid graph name: {graph_name!r}")

    # 2. Gate under DDL permission
    check_permission(Capability.DDL, context.settings.access_mode)

    driver = context.database.driver()

    # 3. Load AGE and check if graph already exists
    await driver.execute_query("LOAD 'age';")
    try:
        exists_rows = await driver.execute_query(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s;",
            [graph_name],
        )
    except Exception as exc:
        raise DatabaseError("Apache AGE is not enabled in this database. Ensure CREATE EXTENSION age is run.") from exc

    if exists_rows:
        return GraphMgmtResult(
            graph_name=graph_name,
            status="exists",
            message=f"Graph {graph_name!r} already exists.",
        )

    # 4. Create the graph
    try:
        await driver.execute_query("SELECT create_graph(%s);", [graph_name])
    except Exception as exc:
        raise DatabaseError(f"failed to create graph {graph_name!r}: {exc}") from exc

    return GraphMgmtResult(
        graph_name=graph_name,
        status="created",
        message=f"Graph {graph_name!r} has been created successfully.",
    )


async def drop_graph(context: AppContext, graph_name: str, cascade: bool = True) -> GraphMgmtResult:
    """Delete a property graph space, dropping all its nodes, edges, and schemas.

    Args:
        graph_name: The name of the graph to delete.
        cascade: If True (default), drops all associated schemas and tables.
    """
    # 1. Validate name
    if not graph_name.replace("_", "").isalnum() or graph_name[0].isdigit():
        raise GraphError(f"invalid graph name: {graph_name!r}")

    # 2. Gate under DDL permission
    check_permission(Capability.DDL, context.settings.access_mode)

    driver = context.database.driver()

    # 3. Load AGE and check if graph exists
    await driver.execute_query("LOAD 'age';")
    try:
        exists_rows = await driver.execute_query(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s;",
            [graph_name],
        )
    except Exception as exc:
        raise DatabaseError("could not query graph metadata") from exc

    if not exists_rows:
        return GraphMgmtResult(
            graph_name=graph_name,
            status="not_found",
            message=f"Graph {graph_name!r} does not exist.",
        )

    # 4. Drop the graph
    try:
        # drop_graph takes (name, cascade) parameters
        await driver.execute_query("SELECT drop_graph(%s, %s);", [graph_name, cascade])
    except Exception as exc:
        raise DatabaseError(f"failed to drop graph {graph_name!r}: {exc}") from exc

    return GraphMgmtResult(
        graph_name=graph_name,
        status="dropped",
        message=f"Graph {graph_name!r} has been deleted successfully.",
    )
