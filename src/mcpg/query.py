"""Safe read-only query execution.

``run_select`` runs an arbitrary, agent-supplied SQL string through the
vendored ``SafeSqlDriver``, which parses and validates it against an
allowlist before execution and forces a read-only transaction. Writes, DDL,
and other unsafe statements are rejected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SafeSqlDriver, SqlDriver

# Default per-query execution timeout, in seconds.
DEFAULT_TIMEOUT_SECONDS = 30.0


class QueryError(Exception):
    """Raised when a query is rejected as unsafe or fails to execute."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """The outcome of a read-only query."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


async def run_select(driver: SqlDriver, sql: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> QueryResult:
    """Validate and execute a read-only SQL query.

    Args:
        driver: The SQL driver to run the query through.
        sql: The query text supplied by the caller.
        timeout: Per-query execution timeout, in seconds.

    Raises:
        QueryError: If the query is rejected as unsafe or fails to execute.
    """
    safe_driver = SafeSqlDriver(sql_driver=driver, timeout=timeout)
    try:
        # SafeSqlDriver parses and validates this runtime SQL before running it.
        rows = await safe_driver.execute_query(sql)
    except Exception as exc:
        raise QueryError(str(exc)) from exc

    result_rows = [dict(row.cells) for row in rows or []]
    columns = list(result_rows[0].keys()) if result_rows else []
    return QueryResult(columns=columns, rows=result_rows, row_count=len(result_rows))


@dataclass(frozen=True, slots=True)
class ExplainResult:
    """A query's execution plan, as returned by ``EXPLAIN``."""

    plan: Any


async def explain_query(driver: SqlDriver, sql: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ExplainResult:
    """Return the PostgreSQL execution plan for a query.

    The query is wrapped in ``EXPLAIN (FORMAT JSON)`` and validated by the
    same safety allowlist as :func:`run_select`. The query itself is not run;
    only its plan is produced.

    Raises:
        QueryError: If the query is rejected as unsafe or planning fails.
    """
    safe_driver = SafeSqlDriver(sql_driver=driver, timeout=timeout)
    try:
        rows = await safe_driver.execute_query(f"EXPLAIN (FORMAT JSON) {sql}")
    except Exception as exc:
        raise QueryError(str(exc)) from exc

    if not rows:
        raise QueryError("EXPLAIN produced no plan")
    raw = next(iter(rows[0].cells.values()))
    plan = json.loads(raw) if isinstance(raw, str) else raw
    return ExplainResult(plan=plan)
