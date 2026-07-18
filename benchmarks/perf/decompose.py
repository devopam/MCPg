"""Overhead decomposition — the server-side latency waterfall.

Times the MCPg server path **segment by segment** so we can attribute the
added latency and land the load-bearing result: **``t_db`` is identical to the
native path**, with the (fixed-cost) overhead living in parse + serialize.

    t_parse -> t_pool -> t_txn -> t_db -> t_serialize

:class:`DecompositionRunner` faithfully mirrors the real production path with
``time.perf_counter_ns()`` checkpoints between segments. It MUST stay in
lockstep with:

* ``src/mcpg/sql/safety.py`` ``SafeSqlDriver._validate``       -> ``t_parse``
* ``src/mcpg/sql/driver.py`` ``_execute_with_connection``      -> ``t_pool`` /
  ``t_txn`` / ``t_db`` / ``t_serialize``
* ``src/mcpg/query.py`` ``run_select``                         -> ``t_serialize``
  (the ``max_rows`` truncation + dict materialization)

If any of those change, update the checkpoints here or the waterfall silently
lies. The ``t_db`` segment executes the **same** ``/* crystaldba */``-prefixed
statement the server issues, so it is directly comparable to the native path's
``t_db`` (the marker comment is lexed and discarded — nil execution cost).

Operator tool — needs a live PostgreSQL, so (like ``paths.py`` / ``runner.py``)
it is not unit-tested; the pure summariser + tolerance check below are.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from psycopg.rows import dict_row

from benchmarks.perf import stats
from mcpg.database import Database
from mcpg.sql import SafeSqlDriver, SqlDriver

from .schema import Decomposition

# Same marker the server prefixes onto every validated query (safety.py). We
# replicate it verbatim so the timed t_db statement is byte-identical to what
# MCPg actually sends.
_MARKER = "/* crystaldba */ "


@dataclass(frozen=True)
class SegmentSample:
    """One decomposition measurement, per-segment elapsed nanoseconds."""

    t_parse: int
    t_pool: int
    t_txn: int
    t_db: int
    t_serialize: int


class DecompositionRunner:
    """Times the server path segment-by-segment (see module docstring)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def run_once(self, sql: str, *, max_rows: int) -> SegmentSample:
        driver = self._database.driver()
        safe = SafeSqlDriver(sql_driver=driver)
        # Ensure the pool is open before timing — the one-time open is not part
        # of a per-query cost and must not land in t_pool.
        pool = await driver.connect().pool_connect()

        # t_parse — pglast parse + allowlist AST walk (the real validator).
        t0 = time.perf_counter_ns()
        safe._validate(sql)
        t_parse = time.perf_counter_ns() - t0

        # t_pool — check a connection out of the warmed pool.
        t0 = time.perf_counter_ns()
        async with pool.connection() as conn:
            t_pool = time.perf_counter_ns() - t0
            async with conn.cursor(row_factory=dict_row) as cur:
                # t_txn — BEGIN READ ONLY + ROLLBACK (both ends of the envelope).
                t0 = time.perf_counter_ns()
                await cur.execute("BEGIN TRANSACTION READ ONLY")
                t_txn_begin = time.perf_counter_ns() - t0

                try:
                    # t_db — the query + fetch. This is the segment that must
                    # match native; it runs the exact statement the server sends.
                    t0 = time.perf_counter_ns()
                    await cur.execute(f"{_MARKER}{sql}")
                    rows = await cur.fetchall()
                    t_db = time.perf_counter_ns() - t0

                    # t_serialize — RowResult wrap (driver) + dict + max_rows
                    # truncation (run_select), mirrored exactly.
                    t0 = time.perf_counter_ns()
                    row_results = [SqlDriver.RowResult(cells=dict(row)) for row in rows]
                    all_rows = [dict(rr.cells) for rr in row_results]
                    _ = all_rows[:max_rows]
                    t_serialize = time.perf_counter_ns() - t0

                    # Close the read-only transaction (second half of t_txn).
                    t0 = time.perf_counter_ns()
                    await cur.execute("ROLLBACK")
                    t_txn = t_txn_begin + (time.perf_counter_ns() - t0)
                except BaseException:
                    # A failing sample must not return the pooled connection mid
                    # transaction and poison later checkouts. Best-effort close,
                    # then re-raise the original error.
                    try:
                        await cur.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise

        return SegmentSample(
            t_parse=t_parse,
            t_pool=t_pool,
            t_txn=t_txn,
            t_db=t_db,
            t_serialize=t_serialize,
        )


def _median(values: list[int]) -> float:
    return stats.percentile(sorted(values), 50)


def summarize_segments(samples: list[SegmentSample], warmup: int = 0) -> Decomposition:
    """Aggregate per-segment samples to a :class:`Decomposition` of medians (ns).

    The first ``warmup`` samples are dropped (cold effects). Returns an
    all-``None`` :class:`Decomposition` when nothing survives, so a degenerate
    run serializes cleanly rather than raising.
    """
    kept = samples[warmup:] if warmup else samples
    if not kept:
        return Decomposition()
    return Decomposition(
        t_parse=_median([s.t_parse for s in kept]),
        t_pool=_median([s.t_pool for s in kept]),
        t_txn=_median([s.t_txn for s in kept]),
        t_db=_median([s.t_db for s in kept]),
        t_serialize=_median([s.t_serialize for s in kept]),
    )


def t_db_within_native(
    server_t_db_ns: float,
    native_t_db_ns: float,
    *,
    rel_tol: float = 0.30,
    abs_floor_ns: float = 200_000.0,
) -> bool:
    """Whether the server path's ``t_db`` matches native's — the killer claim.

    Same SQL on the same PostgreSQL does the same work, so the server-path DB
    segment should equal the native baseline. "Equal" is checked with a
    relative tolerance plus an absolute floor: sub-millisecond timings are
    jitter-dominated, so a tiny query's ``t_db`` is allowed to wander by the
    floor (default 0.2 ms) even when that exceeds ``rel_tol``; large queries
    are held to the tight relative bound.
    """
    if native_t_db_ns <= 0:
        return server_t_db_ns <= abs_floor_ns
    allowed = max(rel_tol * native_t_db_ns, abs_floor_ns)
    return abs(server_t_db_ns - native_t_db_ns) <= allowed
