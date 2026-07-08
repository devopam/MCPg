"""Safe read-only query execution.

``run_select`` runs an arbitrary, agent-supplied SQL string through the
vendored ``SafeSqlDriver``, which parses and validates it against an
allowlist before execution and forces a read-only transaction. Writes, DDL,
and other unsafe statements are rejected.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from mcpg.sql import SafeSqlDriver, SqlDriver

# Default per-query execution timeout, in seconds.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Default cap on the number of rows returned to the caller.
DEFAULT_MAX_ROWS = 1000

# A PostgreSQL memory-size GUC value: digits + a unit. Anchored so a value
# like ``256MB`` is accepted but ``256MB; DROP TABLE`` is not — the knob is
# interpolated into SQL, so this regex is also the injection guard.
_MEMORY_SIZE_RE = re.compile(r"^\d+(kB|MB|GB)$", re.IGNORECASE)

# Hard ceiling on a tuned memory knob: 2 GiB. work_mem / maintenance_work_mem
# are *per-operation* — a runaway value is a real OOM risk under concurrency,
# so we refuse anything larger rather than trust the caller.
_MEMORY_CAP_KB = 2 * 1024 * 1024  # 2 GiB expressed in kB


class QueryError(Exception):
    """Raised when a query is rejected as unsafe or fails to execute."""


@dataclass(frozen=True)
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


_UNIT_TO_KB = {"KB": 1, "MB": 1024, "GB": 1024 * 1024}


def _validate_memory_knob(value: str, name: str) -> str:
    """Validate a work_mem-style GUC value against the format + hard cap.

    Returns the canonicalised value on success.

    Raises:
        QueryError: When ``value`` doesn't match ``^\\d+(kB|MB|GB)$``, is
            non-positive, or exceeds the 2 GiB cap.
    """
    candidate = value.strip()
    match = _MEMORY_SIZE_RE.match(candidate)
    if not match:
        raise QueryError(f"{name} must look like '256MB' (digits + kB/MB/GB); got {value!r}")
    magnitude = int(candidate[: match.start(1)])
    if magnitude <= 0:
        raise QueryError(f"{name} must be positive; got {value!r}")
    size_kb = magnitude * _UNIT_TO_KB[match.group(1).upper()]
    if size_kb > _MEMORY_CAP_KB:
        raise QueryError(f"{name} exceeds the 2GB cap (got {value!r}); pick a smaller bound")
    return candidate


async def run_select_tuned(
    driver: SqlDriver,
    sql: str,
    *,
    work_mem: str,
    maintenance_work_mem: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> QueryResult:
    """Run a read-only SELECT with an elevated, bounded ``work_mem``.

    Heavy analytical SELECTs (large sorts, hash joins, ``DISTINCT`` /
    ``GROUP BY`` over millions of rows) spill to disk when ``work_mem`` is
    too small. This raises it — and optionally ``maintenance_work_mem`` —
    for **this one statement only** via ``SET LOCAL`` inside the same
    read-only transaction, so the bump never leaks back into the pool.

    Both knobs are validated against ``^\\d+(kB|MB|GB)$`` and a hard 2 GiB
    cap (unbounded values are an OOM risk). The SQL is validated read-only
    by the same parser as :func:`run_select`. The knob + query are issued
    as a single ``execute_query`` call so ``SET LOCAL`` applies to the same
    transaction.

    ``SET LOCAL`` only affects transactional statements — non-transactional
    maintenance (CREATE INDEX CONCURRENTLY, VACUUM) is out of scope here.

    Raises:
        QueryError: When ``max_rows`` is not positive, a memory knob is
            malformed / oversized, or the query is rejected as unsafe or
            fails to execute.
    """
    if max_rows < 1:
        raise QueryError("max_rows must be at least 1")

    work_mem_value = _validate_memory_knob(work_mem, "work_mem")
    maintenance_value = (
        _validate_memory_knob(maintenance_work_mem, "maintenance_work_mem")
        if maintenance_work_mem is not None
        else None
    )

    # Prove the caller SQL is a read-only SELECT (parse-only). We can't run
    # it through SafeSqlDriver because that driver forbids the SET LOCAL
    # prefix — but its validator is purely a pglast parse, independent of
    # the underlying connection.
    safe_driver = SafeSqlDriver(sql_driver=driver, timeout=timeout)
    try:
        safe_driver._validate(sql)
    except Exception as exc:
        raise QueryError(str(exc)) from exc

    prefix = f"SET LOCAL work_mem = '{work_mem_value}'; "
    if maintenance_value is not None:
        prefix += f"SET LOCAL maintenance_work_mem = '{maintenance_value}'; "
    tuned_sql = f"{prefix}{sql}"

    try:
        # One call so SET LOCAL and the SELECT share a transaction;
        # force_readonly wraps it in BEGIN READ ONLY. Reinstate the timeout
        # bound the raw driver doesn't apply itself.
        rows = await asyncio.wait_for(
            driver.execute_query(tuned_sql, force_readonly=True),
            timeout=timeout,
        )
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


@dataclass(frozen=True)
class ExplainResult:
    """A query's execution plan, as returned by ``EXPLAIN``."""

    plan: Any


async def explain_query(
    driver: SqlDriver,
    sql: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    io: bool = False,
) -> ExplainResult:
    """Return the PostgreSQL execution plan for a query.

    The query is wrapped in ``EXPLAIN (FORMAT JSON)`` and validated by the
    same safety allowlist as :func:`run_select`. The query itself is not run;
    only its plan is produced — unless ``io=True``.

    ``io=True`` switches to ``EXPLAIN (ANALYZE, BUFFERS, TIMING, FORMAT JSON)``.
    PG 19 extended the BUFFERS section of ``EXPLAIN ANALYZE`` with
    asynchronous-I/O statistics (per-node read counts split between
    synchronous and AIO paths). The output is still parseable on PG 14-18
    — the AIO fields simply don't appear there. Pre-PG 19 ``BUFFERS`` was
    already exposed; ``io=True`` works everywhere, just with richer
    breakdowns on PG 19.

    .. warning::

       ``io=True`` runs ``ANALYZE``, which executes the query. The safety
       allowlist still rejects writes / DDL so only SELECTs pass — but
       a long-running SELECT will actually run, not just plan.

    Raises:
        QueryError: If the query is rejected as unsafe or planning fails.
    """
    # The vendored SafeSqlDriver deliberately rejects EXPLAIN ANALYZE
    # because the option *executes* the query — for arbitrary input that
    # would defeat the read-only contract. When io=True we want the
    # execution (we need actual buffer + IO numbers), but only for SQL
    # that the safety allowlist would otherwise accept as a plain SELECT.
    # Strategy: validate the plain `EXPLAIN (FORMAT JSON) <sql>` form
    # via the safe driver — proves the inner SQL is a safe SELECT — then
    # run the ANALYZE variant through the raw driver.
    safe_driver = SafeSqlDriver(sql_driver=driver, timeout=timeout)
    try:
        if io:
            safe_driver._validate(f"EXPLAIN (FORMAT JSON) {sql}")
            # SafeSqlDriver wraps its own execute_query in `asyncio.timeout`;
            # the raw driver doesn't, so reinstate the bound here. Without
            # this an EXPLAIN ANALYZE on a runaway query (long sort, missing
            # index, deadlock) would block the server worker indefinitely.
            rows = await asyncio.wait_for(
                driver.execute_query(f"EXPLAIN (ANALYZE, BUFFERS, TIMING, FORMAT JSON) {sql}"),
                timeout=timeout,
            )
        else:
            rows = await safe_driver.execute_query(f"EXPLAIN (FORMAT JSON) {sql}")
    except Exception as exc:
        raise QueryError(str(exc)) from exc

    if not rows:
        raise QueryError("EXPLAIN produced no plan")
    raw = next(iter(rows[0].cells.values()))
    plan = json.loads(raw) if isinstance(raw, str) else raw
    return ExplainResult(plan=plan)


@dataclass(frozen=True)
class QueryPlanAnalysis:
    """A structured summary of a query's execution plan.

    Fields starting with ``actual_`` and ``buffers_`` / ``io_`` are
    populated only when the underlying ``EXPLAIN`` ran with ``ANALYZE
    + BUFFERS`` (i.e. ``analyze_query_plan(..., io=True)``); otherwise
    they're ``None``. ``aio_read_blocks`` and ``aio_write_blocks``
    surface the PG 19 asynchronous-I/O split — they're ``None`` on
    PG ≤ 18 even with ``io=True`` because the EXPLAIN output doesn't
    carry the keys.
    """

    total_cost: float
    estimated_rows: int
    node_types: list[str]
    sequential_scans: list[str]
    actual_total_time_ms: float | None = None
    actual_rows: int | None = None
    shared_blocks_read: int | None = None
    shared_blocks_hit: int | None = None
    io_read_time_ms: float | None = None
    io_write_time_ms: float | None = None
    # PG 19 BUFFERS extension — asynchronous-I/O counts.
    aio_read_blocks: int | None = None
    aio_write_blocks: int | None = None


def _walk_plan(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield a plan node and all of its descendants."""
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)


def _sum_optional(values: Iterator[Any]) -> int | None:
    """Sum a sequence of optional ints, returning ``None`` if every value
    is ``None`` (i.e. the source EXPLAIN didn't carry the field) and the
    integer sum otherwise. Distinguishes "no observations" from "zero
    observations" — important for AIO fields, which are zero on idle
    workloads but absent entirely on PG ≤ 18."""
    total = 0
    saw_any = False
    for value in values:
        if value is None:
            continue
        saw_any = True
        total += int(value)
    return total if saw_any else None


def _sum_optional_float(values: Iterator[Any]) -> float | None:
    """Float twin of :func:`_sum_optional` — for ms-typed I/O timing fields."""
    total = 0.0
    saw_any = False
    for value in values:
        if value is None:
            continue
        saw_any = True
        total += float(value)
    return total if saw_any else None


async def analyze_query_plan(
    driver: SqlDriver,
    sql: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    io: bool = False,
) -> QueryPlanAnalysis:
    """Summarise a query's execution plan into a structured analysis.

    Builds on :func:`explain_query`: walks the plan tree to surface the total
    estimated cost, estimated rows, the node types used, and any tables read
    by a sequential scan.

    ``io=True`` requests ``EXPLAIN (ANALYZE, BUFFERS, TIMING)``, which
    actually runs the query and lets us roll up runtime + buffer stats
    across the plan tree. On PG 19 the BUFFERS block additionally
    reports asynchronous-I/O block counts; we surface those as
    :attr:`QueryPlanAnalysis.aio_read_blocks` /
    :attr:`~QueryPlanAnalysis.aio_write_blocks`, which stay ``None``
    on PG ≤ 18.

    Raises:
        QueryError: If the query is rejected, planning fails, or the EXPLAIN
            output has an unexpected shape.
    """
    plan = (await explain_query(driver, sql, timeout=timeout, io=io)).plan
    if not isinstance(plan, list) or not plan:
        raise QueryError("unexpected EXPLAIN output")

    root = plan[0]["Plan"]
    nodes = list(_walk_plan(root))
    node_types = sorted({node["Node Type"] for node in nodes})
    sequential_scans = sorted({node["Relation Name"] for node in nodes if node["Node Type"] == "Seq Scan"})

    if not io:
        return QueryPlanAnalysis(
            total_cost=root["Total Cost"],
            estimated_rows=root["Plan Rows"],
            node_types=node_types,
            sequential_scans=sequential_scans,
        )

    # When io=True we requested ANALYZE; the plan now carries actual
    # timing + per-node buffer counts. Roll them up.
    return QueryPlanAnalysis(
        total_cost=root["Total Cost"],
        estimated_rows=root["Plan Rows"],
        node_types=node_types,
        sequential_scans=sequential_scans,
        actual_total_time_ms=root.get("Actual Total Time"),
        actual_rows=root.get("Actual Rows"),
        shared_blocks_read=_sum_optional(node.get("Shared Read Blocks") for node in nodes),
        shared_blocks_hit=_sum_optional(node.get("Shared Hit Blocks") for node in nodes),
        io_read_time_ms=_sum_optional_float(node.get("I/O Read Time") for node in nodes),
        io_write_time_ms=_sum_optional_float(node.get("I/O Write Time") for node in nodes),
        aio_read_blocks=_sum_optional(node.get("Async I/O Read Blocks") for node in nodes),
        aio_write_blocks=_sum_optional(node.get("Async I/O Write Blocks") for node in nodes),
    )


# --- parallel-select helper (Phase 3.4) ----------------------------------

# Default cap on how many statements one parallel call can dispatch.
# A pool of 5 is the documented default; we leave headroom so other
# in-flight tools don't starve.
DEFAULT_PARALLEL_LIMIT = 8


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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
