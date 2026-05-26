"""Safe read-only query execution.

``run_select`` runs an arbitrary, agent-supplied SQL string through the
vendored ``SafeSqlDriver``, which parses and validates it against an
allowlist before execution and forces a read-only transaction. Writes, DDL,
and other unsafe statements are rejected.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
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


@dataclass(frozen=True, slots=True)
class QueryPlanAnalysis:
    """A structured summary of a query's execution plan."""

    total_cost: float
    estimated_rows: int
    node_types: list[str]
    sequential_scans: list[str]


def _walk_plan(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield a plan node and all of its descendants."""
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)


async def analyze_query_plan(
    driver: SqlDriver, sql: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> QueryPlanAnalysis:
    """Summarise a query's execution plan into a structured analysis.

    Builds on :func:`explain_query`: walks the plan tree to surface the total
    estimated cost, estimated rows, the node types used, and any tables read
    by a sequential scan.

    Raises:
        QueryError: If the query is rejected, planning fails, or the EXPLAIN
            output has an unexpected shape.
    """
    plan = (await explain_query(driver, sql, timeout=timeout)).plan
    if not isinstance(plan, list) or not plan:
        raise QueryError("unexpected EXPLAIN output")

    root = plan[0]["Plan"]
    nodes = list(_walk_plan(root))
    node_types = sorted({node["Node Type"] for node in nodes})
    sequential_scans = sorted({node["Relation Name"] for node in nodes if node["Node Type"] == "Seq Scan"})
    return QueryPlanAnalysis(
        total_cost=root["Total Cost"],
        estimated_rows=root["Plan Rows"],
        node_types=node_types,
        sequential_scans=sequential_scans,
    )


# --- parallel-select helper (Phase 3.4) ----------------------------------

# Default cap on how many statements one parallel call can dispatch.
# A pool of 5 is the documented default; we leave headroom so other
# in-flight tools don't starve.
DEFAULT_PARALLEL_LIMIT = 8


@dataclass(frozen=True, slots=True)
class ParallelQueryOutcome:
    """One slot in :class:`ParallelQueryResult`.

    Either ``result`` is set and ``error`` is ``None``, or vice versa.
    ``index`` is the position of this query in the input list so the
    caller can correlate without relying on ordering (we preserve
    input order, but agents asking via the MCP layer get JSON so the
    explicit index avoids a class of bugs).
    """

    index: int
    success: bool
    result: QueryResult | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ParallelQueryResult:
    """Aggregate result of :func:`run_select_parallel`."""

    outcomes: list[ParallelQueryOutcome]


async def run_select_parallel(
    driver: SqlDriver,
    statements: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_rows: int = DEFAULT_MAX_ROWS,
    parallel_limit: int = DEFAULT_PARALLEL_LIMIT,
) -> ParallelQueryResult:
    """Run up to ``parallel_limit`` read-only SELECTs concurrently.

    Each statement is validated by the same safety allowlist as
    :func:`run_select`. One bad query does not abort the others — its
    error is captured in its own :class:`ParallelQueryOutcome` and
    the remaining slots are still populated.

    Raises:
        QueryError: When ``statements`` is empty, exceeds
            ``parallel_limit``, or contains a blank entry.
    """
    if not statements:
        raise QueryError("statements must not be empty")
    if len(statements) > parallel_limit:
        raise QueryError(f"too many statements ({len(statements)}); parallel_limit is {parallel_limit}")
    if any(not s.strip() for s in statements):
        raise QueryError("statements must not contain blank entries")

    async def _run_one(index: int, sql: str) -> ParallelQueryOutcome:
        try:
            result = await run_select(driver, sql, timeout=timeout, max_rows=max_rows)
        except QueryError as exc:
            return ParallelQueryOutcome(index=index, success=False, result=None, error=str(exc))
        except Exception as exc:
            return ParallelQueryOutcome(index=index, success=False, result=None, error=str(exc))
        return ParallelQueryOutcome(index=index, success=True, result=result, error=None)

    outcomes = await asyncio.gather(*[_run_one(i, sql) for i, sql in enumerate(statements)])
    return ParallelQueryResult(outcomes=list(outcomes))
