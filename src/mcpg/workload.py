"""Workload analysis via the ``pg_stat_statements`` extension.

``analyze_workload`` surfaces the slowest queries by mean execution time. The
extension is optional; when it is not installed the report degrades
gracefully with ``available=False`` rather than failing.

``detect_n_plus_one`` walks the same source for the classic N+1 pattern:
a normalised query template executed hundreds-to-thousands of times,
each call returning at most a row or two. The detector is heuristic —
agents should treat findings as candidates for investigation, not
verdicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

# Default number of slow queries to return.
DEFAULT_LIMIT = 10


@dataclass(frozen=True, slots=True)
class SlowQuery:
    """One entry from ``pg_stat_statements``."""

    query: str
    calls: int
    mean_exec_ms: float
    total_exec_ms: float
    rows: int


@dataclass(frozen=True, slots=True)
class WorkloadReport:
    """The slowest queries, or a note that the extension is unavailable."""

    available: bool
    slow_queries: list[SlowQuery]


async def analyze_workload(driver: SqlDriver, *, limit: int = DEFAULT_LIMIT) -> WorkloadReport:
    """Return the slowest queries by mean execution time.

    Requires the ``pg_stat_statements`` extension. When it is not installed
    the report is returned with ``available=False`` and no queries.

    Args:
        driver: The SQL driver to query through.
        limit: Maximum number of slow queries to return.
    """
    if not await extension_installed(driver, "pg_stat_statements"):
        return WorkloadReport(available=False, slow_queries=[])

    rows = await driver.execute_query(
        "SELECT query, calls, mean_exec_time, total_exec_time, rows "
        "FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT %s",
        params=[limit],
        force_readonly=True,
    )
    slow_queries = [
        SlowQuery(
            query=row.cells["query"],
            calls=row.cells["calls"],
            mean_exec_ms=row.cells["mean_exec_time"],
            total_exec_ms=row.cells["total_exec_time"],
            rows=row.cells["rows"],
        )
        for row in rows or []
    ]
    return WorkloadReport(available=True, slow_queries=slow_queries)


# --- N+1 detector (Phase 8.4) --------------------------------------------

# Heuristic defaults. Tuned for "obvious" N+1: hundreds of calls returning
# one row each. An ORM doing a typical lazy-load loop hits these thresholds
# trivially; honest single-row primary-key reads from a hot cache do not.
DEFAULT_MIN_CALLS = 100
DEFAULT_MAX_ROWS_PER_CALL = 2.0
DEFAULT_MIN_TOTAL_MS = 50.0
DEFAULT_NPLUSONE_LIMIT = 25

# Pull the first FROM-target relation out of the normalised query
# template so the agent gets a hint of which table the loop is hitting.
# Conservative: matches `FROM schema.table` / `FROM "table"` / `FROM table`
# right after the FROM keyword, no joins parsed.
_FROM_PATTERN = re.compile(
    r"\bFROM\s+(?:\"([^\"]+)\"\.)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NPlusOneCandidate:
    """One suspicious query template flagged by the N+1 heuristic.

    Fields mirror ``pg_stat_statements`` plus computed ``rows_per_call``
    and a free-text ``reason`` summarising why the row tripped the
    heuristic. ``table_hint`` is a best-effort extraction of the first
    relation in the FROM clause and is ``None`` when the regex
    can't locate one.
    """

    query: str
    calls: int
    rows: int
    rows_per_call: float
    mean_exec_ms: float
    total_exec_ms: float
    table_hint: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class NPlusOneReport:
    """Aggregate result of :func:`detect_n_plus_one`.

    ``available`` is ``False`` when ``pg_stat_statements`` is not
    installed on the target database. ``thresholds`` echoes the
    parameters used so an agent can interpret the findings without
    re-passing the request.
    """

    available: bool
    thresholds: dict[str, float | int]
    candidates: list[NPlusOneCandidate]


def _extract_table_hint(query: str) -> str | None:
    match = _FROM_PATTERN.search(query)
    if not match:
        return None
    schema, table = match.group(1), match.group(2)
    return f"{schema}.{table}" if schema else table


def _build_reason(calls: int, rows_per_call: float, mean_ms: float) -> str:
    fragments = [
        f"{calls:,} calls",
        f"≈{rows_per_call:.1f} row(s)/call",
        f"mean {mean_ms:.2f} ms",
    ]
    return "; ".join(fragments) + " — looks like a per-row lookup loop"


async def detect_n_plus_one(
    driver: SqlDriver,
    *,
    min_calls: int = DEFAULT_MIN_CALLS,
    max_rows_per_call: float = DEFAULT_MAX_ROWS_PER_CALL,
    min_total_ms: float = DEFAULT_MIN_TOTAL_MS,
    limit: int = DEFAULT_NPLUSONE_LIMIT,
) -> NPlusOneReport:
    """Surface ``pg_stat_statements`` rows that look like an N+1 loop.

    A query template trips the heuristic when it has been called at
    least ``min_calls`` times, each call returns no more than
    ``max_rows_per_call`` rows on average, and the cumulative
    execution time exceeds ``min_total_ms``. The cumulative-time
    filter excludes a query that's only been called once or twice with
    a single-row return — that's not a loop, just a normal lookup.

    Results are sorted by ``total_exec_time`` descending — the worst
    offender (most wall-clock burned in the loop) comes first.
    """
    thresholds: dict[str, float | int] = {
        "min_calls": min_calls,
        "max_rows_per_call": max_rows_per_call,
        "min_total_ms": min_total_ms,
        "limit": limit,
    }
    if not await extension_installed(driver, "pg_stat_statements"):
        return NPlusOneReport(available=False, thresholds=thresholds, candidates=[])

    rows = await driver.execute_query(
        "SELECT query, calls, rows, mean_exec_time, total_exec_time "
        "FROM pg_stat_statements "
        "WHERE calls >= %s "
        "AND total_exec_time >= %s "
        # rows / calls <= max_rows_per_call, expressed without division
        # so a zero `calls` row (shouldn't happen but defensive) doesn't
        # trip a DIV/0.
        "AND rows <= calls * %s "
        "ORDER BY total_exec_time DESC "
        "LIMIT %s",
        params=[min_calls, min_total_ms, max_rows_per_call, limit],
        force_readonly=True,
    )
    candidates: list[NPlusOneCandidate] = []
    for row in rows or []:
        calls = int(row.cells["calls"])
        row_count = int(row.cells["rows"])
        mean_ms = float(row.cells["mean_exec_time"])
        total_ms = float(row.cells["total_exec_time"])
        rpc = row_count / calls if calls > 0 else 0.0
        query = str(row.cells["query"])
        candidates.append(
            NPlusOneCandidate(
                query=query,
                calls=calls,
                rows=row_count,
                rows_per_call=rpc,
                mean_exec_ms=mean_ms,
                total_exec_ms=total_ms,
                table_hint=_extract_table_hint(query),
                reason=_build_reason(calls, rpc, mean_ms),
            )
        )
    return NPlusOneReport(available=True, thresholds=thresholds, candidates=candidates)
