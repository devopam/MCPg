"""Apache AGE Cypher Query Execution.

This module provides the ``run_cypher`` tool, allowing agents to execute
native openCypher graph queries, parsing the results dynamically.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

from mcpg.context import AppContext
from mcpg.database import DatabaseError
from mcpg.graph import parse_agtype
from mcpg.policy import Capability, check_permission

logger = logging.getLogger(__name__)

# Pattern to extract the return clause from Cypher
_RETURN_PATTERN = re.compile(r"\bRETURN\b\s+(.*)$", re.IGNORECASE | re.DOTALL)


class CypherResult(TypedDict):
    """The result of a Cypher query execution."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


def parse_return_columns(cypher_query: str) -> list[str]:
    """Parse the openCypher query to dynamically extract return column names.

    E.g.::

        "MATCH (a)-[r]->(b) RETURN a, r, b.name AS name" -> ["a", "r", "name"]
    """
    match = _RETURN_PATTERN.search(cypher_query)
    if not match:
        return ["result"]

    raw_clause = match.group(1).strip()
    columns: list[str] = []

    # Simple split by comma, ignoring nested commas inside brackets is ideal but
    # for most Cypher return clauses a direct split is highly reliable.
    parts = raw_clause.split(",")
    for idx, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue

        # Check for AS alias
        as_match = re.search(r"\bAS\b\s+([A-Za-z_][A-Za-z0-9_]*)", part, re.IGNORECASE)
        if as_match:
            columns.append(as_match.group(1))
        else:
            # Extract the variable name (e.g. "a.name" -> "name", "a" -> "a")
            clean_name = part.split(".")[-1].strip()
            # Retain only valid identifier characters
            clean_name = re.sub(r"[^A-Za-z0-9_]", "", clean_name)
            if clean_name and not clean_name[0].isdigit():
                columns.append(clean_name)
            else:
                columns.append(f"col_{idx}")

    return columns if columns else ["result"]


async def run_cypher(
    context: AppContext,
    graph_name: str,
    cypher_query: str,
) -> CypherResult:
    """Execute an openCypher query on a specific graph database.

    Args:
        graph_name: The name of the target graph space.
        cypher_query: The openCypher statement to execute (e.g. MATCH ... RETURN).
    """
    # 1. Defensive validation of graph_name
    if not graph_name.replace("_", "").isalnum() or graph_name[0].isdigit():
        raise ValueError(f"invalid graph name: {graph_name!r}")

    # 2. Safety / Write checks: Cypher statements can modify graph state.
    # We inspect if the Cypher query contains modifying keywords as single words.
    query_upper = cypher_query.upper()
    is_write = any(re.search(rf"\b{w}\b", query_upper) for w in ("CREATE", "SET", "DELETE", "REMOVE", "MERGE"))

    if is_write:
        check_permission(Capability.WRITE, context.settings.access_mode)
    else:
        check_permission(Capability.READ, context.settings.access_mode)

    driver = context.database.driver()

    # 3. Verify graph exists
    try:
        graph_rows = await driver.execute_query(
            "SELECT name FROM ag_catalog.ag_graph WHERE name = %s;",
            [graph_name],
        )
    except Exception as exc:
        raise DatabaseError("could not verify graph existence") from exc

    if not graph_rows:
        raise ValueError(f"graph {graph_name!r} does not exist")

    # 4. Load AGE and set search path at session-level
    await driver.execute_query("LOAD 'age';")
    await driver.execute_query(f"SET search_path = {graph_name}, ag_catalog, public;")

    # 5. Parse return columns and build PostgreSQL cypher(...) SQL statement
    columns = parse_return_columns(cypher_query)
    columns_clause = ", ".join(f'"{col}" agtype' for col in columns)

    # We dollar-quote the Cypher query to prevent SQL syntax clashes
    sql = f"""
        SELECT * FROM cypher('{graph_name}', $$
            {cypher_query}
        $$) as ({columns_clause});
    """

    # 6. Execute the query and parse agtype columns
    try:
        rows = await driver.execute_query(sql)
    except Exception as exc:
        raise DatabaseError(f"Cypher execution failed: {exc}") from exc

    parsed_rows: list[dict[str, Any]] = []
    for row in rows or []:
        parsed_row: dict[str, Any] = {}
        for col in columns:
            raw_val = row.cells.get(col)
            parsed_row[col] = parse_agtype(raw_val)
        parsed_rows.append(parsed_row)

    return CypherResult(
        columns=columns,
        rows=parsed_rows,
        row_count=len(parsed_rows),
    )
