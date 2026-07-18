"""Throughput-under-concurrency sweep.

Single-client latency hides the costs that only appear under load: the
connection **pool** (bounded checkout) and per-call **serialization** competing
for CPU. This sweep drives a path with 1 / 4 / 16 / 64 concurrent clients and
reports aggregate **throughput (queries/sec)** plus the latency distribution
*under load*.

Fairness: at concurrency ``C`` each path gets ``C`` real connections — the
native baseline opens ``C`` persistent connections, and the server-side pool is
sized to the sweep's ceiling — so we measure genuine throughput + serialization
overhead, not artificial pool starvation. (Starvation under a deliberately small
pool is a separate question; here we isolate the overhead.)

Each worker runs a warm-up round first; then a single timed region brackets all
workers' steady-state work, so wall-clock reflects concurrent throughput rather
than warm-up. Operator tool (live DB) — not unit-tested; the pure aggregation
(:func:`throughput_rps`, :func:`aggregate`) is.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from benchmarks.perf.paths import PathRunner
from benchmarks.perf.queries import BenchQuery

# The concurrency points (locked in docs/plans/benchmark-suite.md).
CONCURRENCY_LEVELS = (1, 4, 16, 64)


@dataclass(frozen=True)
class ConcurrencyResult:
    """Aggregate outcome of one path x query x concurrency level."""

    concurrency: int
    total_queries: int
    wall_seconds: float
    throughput_rps: float
    latencies_ns: list[int]


def throughput_rps(total_queries: int, wall_seconds: float) -> float:
    """Queries per second, guarding a zero/negative wall time (returns 0.0)."""
    if wall_seconds <= 0:
        return 0.0
    return total_queries / wall_seconds


def aggregate(concurrency: int, per_worker_latencies: list[list[int]], wall_ns: int) -> ConcurrencyResult:
    """Fold per-worker latency lists + the shared wall time into a result.

    Pure so the throughput/latency bookkeeping is unit-tested without a DB.
    """
    latencies = [lat for worker in per_worker_latencies for lat in worker]
    wall_seconds = wall_ns / 1e9
    return ConcurrencyResult(
        concurrency=concurrency,
        total_queries=len(latencies),
        wall_seconds=wall_seconds,
        throughput_rps=throughput_rps(len(latencies), wall_seconds),
        latencies_ns=latencies,
    )


async def sweep_level(
    runners: list[PathRunner],
    query: BenchQuery,
    *,
    iterations_per_worker: int,
    warmup_per_worker: int,
) -> ConcurrencyResult:
    """Run ``len(runners)`` workers concurrently for one query.

    Each worker owns one runner (so ``native`` gets a dedicated connection;
    ``server_side`` runners share the pool, which is concurrent-safe). A warm-up
    gather primes caches/pool, then a single timed gather measures steady-state
    concurrent throughput.
    """

    async def _warmup(runner: PathRunner) -> None:
        for _ in range(warmup_per_worker):
            await runner.run_once(query.sql, max_rows=query.max_rows)

    async def _timed(runner: PathRunner) -> list[int]:
        out: list[int] = []
        for _ in range(iterations_per_worker):
            out.append(await runner.run_once(query.sql, max_rows=query.max_rows))
        return out

    await asyncio.gather(*(_warmup(r) for r in runners))
    start = time.perf_counter_ns()
    per_worker = await asyncio.gather(*(_timed(r) for r in runners))
    wall_ns = time.perf_counter_ns() - start
    return aggregate(len(runners), list(per_worker), wall_ns)
