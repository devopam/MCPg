"""Composite tools — agent UX wins built on top of existing primitives.

These tools replace what would otherwise be 4-5 individual tool calls
with a single composite call that returns a structured snapshot. They
own no SQL of their own beyond a thin glue layer; the heavy lifting
lives in :mod:`mcpg.introspection`, :mod:`mcpg.query`,
:mod:`mcpg.health`, and :mod:`mcpg.liveops`.

Tools shipped here:

- :func:`summarize_table` — describe + indexes + constraints + FKs +
  size / row-count / last vacuum-analyze + a small sample, in one
  result. Replaces the typical "tell me about this table" sequence.
- :func:`why_is_this_slow` — EXPLAIN + plan analysis + concurrent
  active queries + blocking locks + cache hit ratio + targeted
  recommendations, in one result. Replaces the typical "this query
  is slow, dig in" loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import (
    ColumnInfo,
    ConstraintInfo,
    ForeignKeyInfo,
    IndexInfo,
    describe_table,
    list_constraints,
    list_foreign_keys,
    list_indexes,
)
from mcpg.liveops import list_active_queries
from mcpg.query import analyze_query_plan, explain_query

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class CompositeError(Exception):
    """Raised when a composite tool's inputs are invalid."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise CompositeError(f"invalid {kind} name: {name!r}")


# --- summarize_table -------------------------------------------------


@dataclass(frozen=True, slots=True)
class TableStats:
    """Storage + maintenance stats for a single table.

    All values are best-effort: a brand-new table that hasn't been
    analyzed yet will report ``None`` for last_analyzed.
    """

    estimated_row_count: int
    total_size_bytes: int
    table_size_bytes: int
    indexes_size_bytes: int
    seq_scans: int
    index_scans: int
    last_vacuum: str | None
    last_autovacuum: str | None
    last_analyze: str | None
    last_autoanalyze: str | None


@dataclass(frozen=True, slots=True)
class TableSummary:
    """The composite shape of :func:`summarize_table`.

    A self-contained snapshot of a table that an agent can render
    without further round-trips.
    """

    schema: str
    table: str
    columns: list[ColumnInfo]
    primary_key: list[str]
    foreign_keys: list[ForeignKeyInfo]
    constraints: list[ConstraintInfo]
    indexes: list[IndexInfo]
    stats: TableStats
    sample_rows: list[dict[str, Any]] = field(default_factory=list)


def _quoted(name: str) -> str:
    return f'"{name}"'


def _pk_columns(constraints: list[ConstraintInfo]) -> list[str]:
    """Pull the column list out of the PRIMARY KEY constraint definition."""
    for con in constraints:
        if con.type == "primary_key":
            match = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", con.definition, re.IGNORECASE)
            if match:
                return [c.strip().strip('"') for c in match.group(1).split(",")]
    return []


async def _fetch_table_stats(driver: SqlDriver, schema: str, table: str) -> TableStats:
    """Read pg_stat_user_tables + pg_class + size functions in one query."""
    rows = await driver.execute_query(
        "SELECT "
        "  COALESCE(c.reltuples, 0)::bigint AS estimated_row_count, "
        "  COALESCE(pg_total_relation_size(c.oid), 0) AS total_size_bytes, "
        "  COALESCE(pg_table_size(c.oid), 0) AS table_size_bytes, "
        "  COALESCE(pg_indexes_size(c.oid), 0) AS indexes_size_bytes, "
        "  COALESCE(s.seq_scan, 0) AS seq_scans, "
        "  COALESCE(s.idx_scan, 0) AS index_scans, "
        "  s.last_vacuum::text AS last_vacuum, "
        "  s.last_autovacuum::text AS last_autovacuum, "
        "  s.last_analyze::text AS last_analyze, "
        "  s.last_autoanalyze::text AS last_autoanalyze "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'r'",
        params=[schema, table],
        force_readonly=True,
    )
    if not rows:
        # The table doesn't exist or we lack permissions; return zeros.
        return TableStats(
            estimated_row_count=0,
            total_size_bytes=0,
            table_size_bytes=0,
            indexes_size_bytes=0,
            seq_scans=0,
            index_scans=0,
            last_vacuum=None,
            last_autovacuum=None,
            last_analyze=None,
            last_autoanalyze=None,
        )
    cells = rows[0].cells
    return TableStats(
        estimated_row_count=int(cells["estimated_row_count"]),
        total_size_bytes=int(cells["total_size_bytes"]),
        table_size_bytes=int(cells["table_size_bytes"]),
        indexes_size_bytes=int(cells["indexes_size_bytes"]),
        seq_scans=int(cells["seq_scans"]),
        index_scans=int(cells["index_scans"]),
        last_vacuum=cells["last_vacuum"],
        last_autovacuum=cells["last_autovacuum"],
        last_analyze=cells["last_analyze"],
        last_autoanalyze=cells["last_autoanalyze"],
    )


async def summarize_table(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    sample_rows: int = 5,
) -> TableSummary:
    """Return a one-stop snapshot of ``schema.table``.

    Composes the introspection primitives the agent would otherwise
    have to call individually (``describe_table`` + ``list_indexes`` +
    ``list_constraints`` + ``list_foreign_keys``) plus storage /
    maintenance stats from ``pg_class`` and ``pg_stat_user_tables``
    and a short ``SELECT *`` sample.

    Args:
        sample_rows: How many random-ordered sample rows to include.
            Set to 0 to skip the sample entirely (useful on very wide
            or jsonb-heavy rows).

    Raises:
        CompositeError: When the schema / table name is not a plain
            identifier.
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    if sample_rows < 0:
        raise CompositeError("sample_rows must be non-negative")

    columns = await describe_table(driver, schema, table)
    constraints = await list_constraints(driver, schema, table)
    indexes = await list_indexes(driver, schema, table)
    # FKs come from the schema-level list filtered to this table.
    fks_all = await list_foreign_keys(driver, schema)
    foreign_keys = [fk for fk in fks_all if fk.from_table == table]
    pk = _pk_columns(constraints)
    stats = await _fetch_table_stats(driver, schema, table)

    sample: list[dict[str, Any]] = []
    if sample_rows > 0 and columns:
        # TABLESAMPLE BERNOULLI is fast but doesn't give a tight count;
        # for the small "show me a few rows" use case a plain LIMIT
        # over an unordered scan is good enough.
        sample_query_rows = await driver.execute_query(
            f"SELECT * FROM {_quoted(schema)}.{_quoted(table)} LIMIT %s",
            params=[sample_rows],
            force_readonly=True,
        )
        sample = [dict(row.cells) for row in sample_query_rows or []]

    return TableSummary(
        schema=schema,
        table=table,
        columns=columns,
        primary_key=pk,
        foreign_keys=foreign_keys,
        constraints=constraints,
        indexes=indexes,
        stats=stats,
        sample_rows=sample,
    )


# --- why_is_this_slow -------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlowQuerySuggestion:
    """One actionable recommendation from :func:`why_is_this_slow`.

    ``category`` is one of ``"plan"``, ``"contention"``,
    ``"cache"``, ``"maintenance"`` to help the agent triage. ``hint``
    is the human-readable advice the agent should surface.
    """

    category: str
    hint: str


@dataclass(frozen=True, slots=True)
class SlowQueryDiagnosis:
    """The composite shape of :func:`why_is_this_slow`.

    Combines the plan summary, snapshot of concurrent activity, lock
    state, and cache hit ratio into one structured payload so the
    agent doesn't need to make four separate calls.
    """

    sql: str
    plan_summary: dict[str, Any]
    explain_plan: dict[str, Any]
    active_queries: list[dict[str, Any]]
    blocking_locks: list[dict[str, Any]]
    cache_hit_ratio: float | None
    suggestions: list[SlowQuerySuggestion]


async def _cache_hit_ratio(driver: SqlDriver) -> float | None:
    """Return the cluster-wide buffer cache hit ratio, or None if no I/O yet."""
    rows = await driver.execute_query(
        "SELECT sum(blks_hit) AS hits, sum(blks_read) AS reads FROM pg_stat_database",
        force_readonly=True,
    )
    if not rows:
        return None
    cells = rows[0].cells
    hits, reads = cells["hits"] or 0, cells["reads"] or 0
    total = hits + reads
    if total == 0:
        return None
    return float(hits) / float(total)


async def _fetch_blocking_locks(driver: SqlDriver) -> list[dict[str, Any]]:
    """Snapshot of currently blocking lock pairs.

    Returns one row per (blocked_pid, blocking_pid) pair with the
    queries involved. Empty list when nothing is blocked. This is
    expensive on busy systems with thousands of locks; we cap the
    rows to keep the result digestible.
    """
    rows = await driver.execute_query(
        "SELECT "
        "  blocked.pid AS blocked_pid, "
        "  blocked.query AS blocked_query, "
        "  blocking.pid AS blocking_pid, "
        "  blocking.query AS blocking_query, "
        "  blocked.wait_event_type AS wait_event_type, "
        "  blocked.wait_event AS wait_event "
        "FROM pg_stat_activity blocked "
        "JOIN pg_stat_activity blocking "
        "  ON blocking.pid = ANY(pg_blocking_pids(blocked.pid)) "
        "WHERE pg_blocking_pids(blocked.pid)::text <> '{}' "
        "LIMIT 25",
        force_readonly=True,
    )
    return [dict(row.cells) for row in rows or []]


def _build_suggestions(
    plan_summary: dict[str, Any],
    active_queries: list[dict[str, Any]],
    blocking_locks: list[dict[str, Any]],
    cache_hit_ratio: float | None,
) -> list[SlowQuerySuggestion]:
    """Translate the gathered signals into concrete advice."""
    suggestions: list[SlowQuerySuggestion] = []
    # Plan-shape signals — sequential scan on a non-trivial table is the
    # single most common "this is slow" cause.
    seq_scans = int(plan_summary.get("sequential_scan_count", 0) or 0)
    total_cost = float(plan_summary.get("total_cost", 0.0) or 0.0)
    if seq_scans > 0 and total_cost > 1_000:
        suggestions.append(
            SlowQuerySuggestion(
                category="plan",
                hint=(
                    f"plan contains {seq_scans} sequential scan(s) with "
                    f"total cost {total_cost:.0f}; consider adding an "
                    "index on the filter or join columns"
                ),
            )
        )
    if total_cost > 100_000:
        suggestions.append(
            SlowQuerySuggestion(
                category="plan",
                hint=(
                    f"planner expects total cost {total_cost:.0f}, which is high; "
                    "run EXPLAIN (ANALYZE, BUFFERS) to see actual vs estimated rows"
                ),
            )
        )
    # Contention.
    if blocking_locks:
        suggestions.append(
            SlowQuerySuggestion(
                category="contention",
                hint=(
                    f"{len(blocking_locks)} blocking lock pair(s) detected; "
                    "another transaction is holding a lock this query needs"
                ),
            )
        )
    if len(active_queries) >= 25:
        suggestions.append(
            SlowQuerySuggestion(
                category="contention",
                hint=(f"{len(active_queries)} concurrent active queries; the system may be CPU- or I/O-bound"),
            )
        )
    # Cache.
    if cache_hit_ratio is not None and cache_hit_ratio < 0.95:
        suggestions.append(
            SlowQuerySuggestion(
                category="cache",
                hint=(
                    f"buffer cache hit ratio is {cache_hit_ratio:.1%}; "
                    "below 95% suggests shared_buffers is too small for "
                    "the working set"
                ),
            )
        )
    if not suggestions:
        suggestions.append(
            SlowQuerySuggestion(
                category="plan",
                hint=(
                    "no obvious slowness signal in the plan, concurrent activity, "
                    "or cache; run EXPLAIN (ANALYZE, BUFFERS) for actual timings"
                ),
            )
        )
    return suggestions


async def why_is_this_slow(driver: SqlDriver, sql: str) -> SlowQueryDiagnosis:
    """Diagnose why a SQL query might be slow, in one call.

    Composes:

    - ``EXPLAIN (FORMAT JSON)`` of the query via :func:`mcpg.query.explain_query`
      (read-only — does not execute the query).
    - ``analyze_query_plan`` to walk the plan tree.
    - ``list_active_queries`` to see what else is competing.
    - Blocking-lock pairs from ``pg_stat_activity`` +
      ``pg_blocking_pids``.
    - Cache hit ratio from :func:`mcpg.health.check_cache_hit_ratio`.

    Returns a structured diagnosis with targeted suggestions per
    category (plan / contention / cache / maintenance). The query is
    NOT executed — only EXPLAIN-ed — so this is safe to run on a
    statement the agent doesn't want to materialise yet.

    Raises:
        CompositeError: When ``sql`` is empty.
    """
    if not sql or not sql.strip():
        raise CompositeError("sql must not be empty")

    plan = await explain_query(driver, sql)
    plan_summary_obj = await analyze_query_plan(driver, sql)
    # analyze_query_plan returns a typed dataclass; convert to a dict for the payload.
    plan_summary = {
        "total_cost": plan_summary_obj.total_cost,
        "estimated_rows": plan_summary_obj.estimated_rows,
        "sequential_scan_count": len(plan_summary_obj.sequential_scans),
        "sequential_scans": list(plan_summary_obj.sequential_scans),
        "node_types": list(plan_summary_obj.node_types),
    }
    active = await list_active_queries(driver)
    active_dicts = [
        {
            "pid": q.pid,
            "username": q.username,
            "state": q.state,
            "query": q.query,
            "wait_event": q.wait_event,
            "duration_seconds": q.duration_seconds,
            "blocked_by": list(q.blocked_by),
        }
        for q in active
    ]
    locks = await _fetch_blocking_locks(driver)
    cache_ratio = await _cache_hit_ratio(driver)

    suggestions = _build_suggestions(plan_summary, active_dicts, locks, cache_ratio)
    return SlowQueryDiagnosis(
        sql=sql,
        plan_summary=plan_summary,
        explain_plan=plan.plan,
        active_queries=active_dicts,
        blocking_locks=locks,
        cache_hit_ratio=cache_ratio,
        suggestions=suggestions,
    )
