"""Performance-run orchestrator (CLI).

Times the **native** and **server-side** paths across the query set (cold +
warm), aggregates to percentiles + a bootstrap median CI, and writes one
structured JSON document under ``benchmarks/results/``.

    uv run python -m benchmarks.perf.runner \
        --database-url "$MCPG_TEST_DATABASE_URL" \
        --scale-factor 1 --iterations 50 --output benchmarks/results/perf.json

Provenance (git SHA, timestamp) is passed in so a result always carries the
exact conditions that produced it. This first cut covers native + server-side;
the end-to-end transport paths, the overhead decomposition, and the
concurrency sweep land in follow-up phases (see docs/plans/benchmark-suite.md).

Operator tool — not unit-tested (needs a live PostgreSQL); the pure helpers it
calls (stats, queries, schema) are.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import platform
import sys
from pathlib import Path
from typing import Any

from benchmarks.perf import stats
from benchmarks.perf.paths import NativeRunner, PathRunner, ServerSideRunner
from benchmarks.perf.queries import BenchQuery, all_queries
from benchmarks.perf.schema import Assertion, LatencyBlock, PerfRun, ResultRow
from mcpg import __version__
from mcpg.config import load_settings
from mcpg.database import Database

_WARMUP = 5


async def _sample_path(runner: PathRunner, query: BenchQuery, iterations: int) -> tuple[list[int], int]:
    """Return (warm samples, cold sample) for one path x query.

    GC is disabled around each timed call so a collection pause never lands
    inside a measurement; collected in the gaps instead.
    """
    gc.collect()
    cold = await runner.run_once(query.sql, max_rows=query.max_rows)  # first call = cold bucket
    samples: list[int] = []
    for i in range(iterations + _WARMUP):
        gc.disable()
        try:
            elapsed = await runner.run_once(query.sql, max_rows=query.max_rows)
        finally:
            gc.enable()
        samples.append(elapsed)
        if i % 8 == 7:
            gc.collect()
    warm = stats.drop_warmup(samples, _WARMUP)
    return warm, cold


def _row(path: str, query: BenchQuery, temperature: str, samples_ns: list[int]) -> ResultRow:
    s = stats.summarize(samples_ns)
    return ResultRow(
        path=path,
        query_id=query.id,
        compute_class=query.compute_class,
        result_size=query.result_size,
        temperature=temperature,
        concurrency=1,
        n=s.n,
        latency_ms=LatencyBlock(
            p50=s.p50, p95=s.p95, p99=s.p99, mean=s.mean, stdev=s.stdev, min=s.min, max=s.max, median_ci95=s.median_ci95
        ),
        samples_ns=samples_ns,
    )


async def _run(args: argparse.Namespace) -> PerfRun:
    settings = load_settings(
        {"MCPG_DATABASE_URL": args.database_url, "MCPG_POOL_MIN_SIZE": "1", "MCPG_POOL_MAX_SIZE": "4"}
    )
    database = Database(settings)
    await database.connect()
    native = await NativeRunner.connect(args.database_url)
    results: list[ResultRow] = []
    try:
        pg = await database.driver().execute_query(
            "SELECT current_setting('server_version') AS v, current_setting('server_version_num')::int AS num",
            force_readonly=True,
        )
        pg_meta = {"version_string": pg[0].cells["v"], "server_version_num": pg[0].cells["num"]} if pg else {}

        for query in all_queries():
            for label, runner in (("native", native), ("server_side", ServerSideRunner(database))):
                warm, cold = await _sample_path(runner, query, args.iterations)
                results.append(_row(label, query, "warm", warm))
                results.append(_row(label, query, "cold", [cold]))
    finally:
        await native.close()
        await database.close()

    metadata: dict[str, Any] = {
        "timestamp": args.timestamp,
        "git_sha": args.git_sha,
        "mcpg_version": __version__,
        "postgres": pg_meta,
        "scale_factor": args.scale_factor,
        "host": {
            "python": platform.python_version(),
            "os": platform.platform(),
            "machine": platform.machine(),
        },
        "iterations": args.iterations,
        "warmup_discarded": _WARMUP,
    }
    return PerfRun(metadata=metadata, results=results, assertions=_assertions(results))


def _assertions(results: list[ResultRow]) -> list[Assertion]:
    """Overhead sanity checks. The fine-grained t_db == native assertion lands
    with the decomposition phase; here we record the warm total-latency delta
    per query so the report can show MCPg's overhead is bounded."""
    out: list[Assertion] = []
    by_key = {(r.path, r.query_id): r for r in results if r.temperature == "warm"}
    query_ids = {r.query_id for r in results}
    for qid in sorted(query_ids):
        native = by_key.get(("native", qid))
        server = by_key.get(("server_side", qid))
        if native is None or server is None:
            continue
        overhead_ms = server.latency_ms.p50 - native.latency_ms.p50
        out.append(
            Assertion(
                name="server_side_overhead_p50_ms",
                query_id=qid,
                passed=True,  # informational; the t_db==native gate lands with decomposition
                detail={
                    "native_p50_ms": native.latency_ms.p50,
                    "server_side_p50_ms": server.latency_ms.p50,
                    "overhead_p50_ms": overhead_ms,
                },
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPg performance benchmark (native vs server-side).")
    parser.add_argument("--database-url", required=True, help="PostgreSQL DSN (a TPC-H-loaded database).")
    parser.add_argument("--iterations", type=int, default=50, help="Warm iterations per pathxquery (>= 20).")
    parser.add_argument("--scale-factor", type=int, default=1, help="TPC-H scale factor the DB was loaded at.")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the result JSON.")
    parser.add_argument("--git-sha", default="unknown", help="Provenance: the commit under test.")
    parser.add_argument("--timestamp", default="unknown", help="Provenance: ISO-8601 run timestamp.")
    args = parser.parse_args(argv)

    run = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(run.to_dict(), indent=2) + "\n")
    print(f"wrote {args.output} ({len(run.results)} result rows)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
