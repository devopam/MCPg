"""Throughput / latency benchmark for the MCPg query path.

Runs many concurrent ``run_select`` calls against a real PostgreSQL and
reports throughput and latency percentiles. This is an operator tool, not a
test; run it manually:

    uv run python benchmarks/bench.py --requests 2000 --concurrency 16

The database URL comes from ``--database-url`` or ``MCPG_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time

from mcpg.config import load_settings
from mcpg.database import Database
from mcpg.query import run_select

_QUERY = "SELECT 1 AS one"


async def _worker(database: Database, count: int, latencies: list[float]) -> None:
    for _ in range(count):
        start = time.perf_counter()
        await run_select(database.driver(), _QUERY)
        latencies.append(time.perf_counter() - start)


async def _run(database_url: str, requests: int, concurrency: int) -> None:
    settings = load_settings({"MCPG_DATABASE_URL": database_url, "MCPG_POOL_MAX_SIZE": str(concurrency)})
    database = Database(settings)
    await database.connect()
    latencies: list[float] = []
    per_worker = requests // concurrency
    try:
        wall_start = time.perf_counter()
        async with asyncio.TaskGroup() as group:
            for _ in range(concurrency):
                group.create_task(_worker(database, per_worker, latencies))
        wall = time.perf_counter() - wall_start
    finally:
        await database.close()

    done = len(latencies)
    ordered = sorted(latencies)
    print(f"requests       : {done}")
    print(f"concurrency    : {concurrency}")
    print(f"wall time      : {wall:.3f} s")
    print(f"throughput     : {done / wall:.0f} req/s")
    print(f"latency p50    : {ordered[done // 2] * 1000:.2f} ms")
    print(f"latency p95    : {ordered[int(done * 0.95)] * 1000:.2f} ms")
    print(f"latency mean   : {statistics.mean(latencies) * 1000:.2f} ms")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the MCPg query path.")
    parser.add_argument("--database-url", default=os.environ.get("MCPG_TEST_DATABASE_URL"))
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args()

    if not args.database_url:
        parser.error("set --database-url or MCPG_TEST_DATABASE_URL")
    asyncio.run(_run(args.database_url, args.requests, args.concurrency))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
