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

# Default cap on the number of rows returned to the caller.
DEFAULT_MAX_ROWS = 1000


class QueryError(Exception):
    """Raised when a query is rejected as unsafe or fails to execute."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """The outcome of a read-only query.

    ``row_count`` is the number of rows actually returned; when ``truncated``
    is true the query produced more rows than the requested cap.
    """

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


async def run_select(
    driver: SqlDriver,
    sql: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> QueryResult:
    """Validate and execute a read-only SQL query.

    Args:
        driver: The SQL driver to run the query through.
        sql: The query text supplied by the caller.
        timeout: Per-query execution timeout, in seconds.
        max_rows: Maximum number of rows to return; extra rows are dropped and
            ``QueryResult.truncated`` is set. Callers wanting more control can
            paginate with their own SQL ``LIMIT``/``OFFSET``.

    Raises:
        QueryError: If ``max_rows`` is not positive, or the query is rejected
            as unsafe or fails to execute.
    """
    if max_rows < 1:
        raise QueryError("max_rows must be at least 1")

    safe_driver = SafeSqlDriver(sql_driver=driver, timeout=timeout)
    try:
        # SafeSqlDriver parses and validates this runtime SQL before running it.
        rows = await safe_driver.execute_query(sql)
    except Exception as exc:
        raise QueryError(str(exc)) from exc

    all_rows = [dict(row.cells) for row in rows or []]
    truncated = len(all_rows) > max_rows
    result_rows = all_rows[:max_rows]
    columns = list(result_rows[0].keys()) if result_rows else []
    return QueryResult(
        columns=columns,
        rows=result_rows,
        row_count=len(result_rows),
        truncated=truncated,
    )


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
