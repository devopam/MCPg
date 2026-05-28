"""Apache AGE Cypher Query Execution.

This module provides the ``run_cypher`` tool, allowing agents to execute
native openCypher graph queries, parsing the results dynamically.
"""

from __future__ import annotations

import asyncio
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

    # 4. Parse return columns and run the cypher() call on a DEDICATED
    #    pool checkout so ``SET LOCAL search_path`` auto-resets at the
    #    end of the explicit transaction and does NOT leak into the
    #    next caller's connection state. Earlier code issued ``LOAD
    #    'age'`` and ``SET search_path`` as separate ``execute_query``
    #    calls; each landed on a different pool connection AND left
    #    ``search_path`` mutated on the original — a confused-deputy
    #    risk for any subsequent unqualified-identifier query.
    columns = parse_return_columns(cypher_query)
    columns_clause = ", ".join(f'"{col}" agtype' for col in columns)

    try:
        parsed_rows = await _execute_cypher_in_isolated_connection(
            driver=driver,
            graph_name=graph_name,
            cypher_query=cypher_query,
            columns=columns,
            columns_clause=columns_clause,
        )
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(f"Cypher execution failed: {exc}") from exc

    return CypherResult(
        columns=columns,
        rows=parsed_rows,
        row_count=len(parsed_rows),
    )


async def _execute_cypher_in_isolated_connection(
    *,
    driver: Any,
    graph_name: str,
    cypher_query: str,
    columns: list[str],
    columns_clause: str,
) -> list[dict[str, Any]]:
    """Run LOAD AGE + SET LOCAL search_path + cypher() on one checkout.

    ``graph_name`` and ``columns_clause`` are pre-validated by the
    caller, so inlining them into SQL is safe. ``cypher_query`` reaches
    the database via a bound parameter on the ``cypher()`` call — that
    keeps the openCypher string out of the SQL parser entirely.
    """
    # Test fakes don't expose pool primitives — fall back to the
    # driver's normal ``execute_query`` path. The pool-pollution this
    # function exists to prevent only matters with a real psycopg pool;
    # fakes don't share connections across calls.
    if not hasattr(driver, "conn") or driver.conn is None:
        return await _run_cypher_via_driver(driver, graph_name, cypher_query, columns, columns_clause)
    if not getattr(driver, "is_pool", False):
        # Direct-connection mode (single AsyncConnection). Use it.
        return await _run_cypher_statements(driver.conn, graph_name, cypher_query, columns, columns_clause)
    pool = await driver.conn.pool_connect()
    async with pool.connection() as connection:
        return await _run_cypher_statements(connection, graph_name, cypher_query, columns, columns_clause)


async def _run_cypher_via_driver(
    driver: Any,
    graph_name: str,
    cypher_query: str,
    columns: list[str],
    columns_clause: str,
) -> list[dict[str, Any]]:
    """Fallback path for test fakes — issues the same statements via the driver.

    Doesn't get the pool-isolation guarantee of the production path,
    but the only callers that hit this branch are tests with in-memory
    drivers that don't share connection state anyway.
    """
    await driver.execute_query("LOAD 'age'")
    await driver.execute_query(f"SET search_path = {graph_name}, ag_catalog, public")
    rows = await driver.execute_query(
        f"SELECT * FROM cypher('{graph_name}', %s) AS ({columns_clause})",
        [cypher_query],
    )
    parsed_rows: list[dict[str, Any]] = []
    for row in rows or []:
        parsed_row: dict[str, Any] = {}
        for col in columns:
            raw_val = row.cells.get(col)
            parsed_row[col] = parse_agtype(raw_val)
        parsed_rows.append(parsed_row)
    return parsed_rows


async def _run_cypher_statements(
    connection: Any,
    graph_name: str,
    cypher_query: str,
    columns: list[str],
    columns_clause: str,
) -> list[dict[str, Any]]:
    """Issue the LOAD + SET LOCAL + cypher() trio on ``connection``.

    Wrapped in an explicit ``BEGIN``/``COMMIT`` so ``SET LOCAL`` auto-
    resets even if the cypher() call fails (``ROLLBACK`` resets local
    settings the same way ``COMMIT`` does).
    """
    from psycopg.rows import dict_row

    async with connection.cursor(row_factory=dict_row) as cur:
        await cur.execute("BEGIN")
        try:
            await cur.execute("LOAD 'age'")
            # ``SET LOCAL`` is valid only inside an explicit txn; auto-resets
            # at COMMIT or ROLLBACK regardless of which exit we take.
            await cur.execute(f"SET LOCAL search_path = {graph_name}, ag_catalog, public")
            await cur.execute(
                f"SELECT * FROM cypher('{graph_name}', %s) AS ({columns_clause})",
                [cypher_query],
            )
            fetched = await cur.fetchall()
            await cur.execute("COMMIT")
        except BaseException:
            # BaseException — not Exception — so asyncio.CancelledError
            # also lands here and we always issue ROLLBACK. Otherwise
            # a cancelled request would leave the (possibly shared)
            # connection in an aborted-transaction state and poison
            # every subsequent query on it.
            try:
                await cur.execute("ROLLBACK")
            except asyncio.CancelledError:
                raise
            except Exception:
                # ROLLBACK itself can fail if the connection is already
                # toast — at that point the pool's reset hook will
                # discard it. Don't let the rollback failure mask the
                # original exception.
                pass
            raise

    parsed_rows: list[dict[str, Any]] = []
    for row in fetched or []:
        parsed_row: dict[str, Any] = {}
        for col in columns:
            raw_val = row.get(col)
            parsed_row[col] = parse_agtype(raw_val)
        parsed_rows.append(parsed_row)
    return parsed_rows
