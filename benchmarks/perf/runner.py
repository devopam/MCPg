"""Performance-run orchestrator (CLI).

Times the **native** and **server-side** paths across the query set (cold +
warm), aggregates to percentiles + a bootstrap median CI, and writes one
structured JSON document under ``benchmarks/results/``.

    uv run python -m benchmarks.perf.runner \
        --database-url "$MCPG_TEST_DATABASE_URL" \
        --scale-factor 1 --iterations 50 --output benchmarks/results/perf.json

It also runs the **overhead decomposition** (perf/decompose.py) on the
server-side path and records the load-bearing ``t_db == native`` assertion.
With ``--e2e`` it additionally measures the **end-to-end paths** through the
real MCP protocol (perf/e2e.py): in-memory + stdio subprocess, plus streamable
HTTP against an operator-started server via ``--e2e-http-url``. Provenance (git
SHA, timestamp) is passed in so a result always carries the exact conditions
that produced it. The concurrency sweep lands in a follow-up phase (see
docs/plans/benchmark-suite.md).

Operator tool — not unit-tested (needs a live PostgreSQL); the pure helpers it
calls (stats, queries, schema, decompose) are.
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
from benchmarks.perf.decompose import DecompositionRunner, SegmentSample, summarize_segments, t_db_within_native
from benchmarks.perf.e2e import E2EHttpRunner, E2EInMemoryRunner, E2ERunner, E2EStdioRunner
from benchmarks.perf.paths import NativeRunner, PathRunner, ServerSideRunner
from benchmarks.perf.queries import BenchQuery, all_queries
from benchmarks.perf.schema import Assertion, Decomposition, LatencyBlock, PerfRun, ResultRow
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


async def _sample_decomposition(runner: DecompositionRunner, query: BenchQuery, iterations: int) -> Decomposition:
    """Sample the server-path waterfall, returning per-segment medians (ns).

    Warm-up is discarded exactly like :func:`_sample_path`; GC is disabled
    around each timed call so no collection pause lands inside a segment.
    """
    gc.collect()
    samples: list[SegmentSample] = []
    for i in range(iterations + _WARMUP):
        gc.disable()
        try:
            sample = await runner.run_once(query.sql, max_rows=query.max_rows)
        finally:
            gc.enable()
        samples.append(sample)
        if i % 8 == 7:
            gc.collect()
    return summarize_segments(samples, warmup=_WARMUP)


async def _sample_native_db(native: NativeRunner, query: BenchQuery, iterations: int) -> float:
    """Median (ns) of native's pure DB segment — the ``t_db == native`` anchor."""
    gc.collect()
    samples: list[int] = []
    for i in range(iterations + _WARMUP):
        gc.disable()
        try:
            samples.append(await native.db_segment_once(query.sql))
        finally:
            gc.enable()
        if i % 8 == 7:
            gc.collect()
    warm = stats.drop_warmup(samples, _WARMUP)
    return stats.percentile(sorted(warm), 50)


def _row(
    path: str,
    query: BenchQuery,
    temperature: str,
    samples_ns: list[int],
    decomposition: Decomposition | None = None,
) -> ResultRow:
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
        decomposition_ns=decomposition,
    )


async def _run(args: argparse.Namespace) -> PerfRun:
    settings = load_settings(
        {"MCPG_DATABASE_URL": args.database_url, "MCPG_POOL_MIN_SIZE": "1", "MCPG_POOL_MAX_SIZE": "4"}
    )
    database = Database(settings)
    await database.connect()
    native = await NativeRunner.connect(args.database_url)
    results: list[ResultRow] = []
    # Pre-initialised so a failure in the metadata query below can't mask the
    # original exception with an UnboundLocalError when `metadata` is built.
    pg_meta: dict[str, Any] = {}
    e2e_runners: list[E2ERunner] = []
    try:
        pg = await database.driver().execute_query(
            "SELECT current_setting('server_version') AS v, current_setting('server_version_num')::int AS num",
            force_readonly=True,
        )
        if pg:
            pg_meta = {"version_string": pg[0].cells["v"], "server_version_num": pg[0].cells["num"]}

        # Opt-in end-to-end paths (through the real MCP protocol). Started once
        # and reused across every query; torn down in the finally.
        e2e_paths: list[tuple[str, PathRunner]] = []
        if args.e2e:
            inmem = E2EInMemoryRunner(settings)
            await inmem.start()
            e2e_runners.append(inmem)
            e2e_paths.append(("e2e_inmemory", inmem))
            stdio = E2EStdioRunner(args.database_url)
            await stdio.start()
            e2e_runners.append(stdio)
            e2e_paths.append(("e2e_stdio", stdio))
        if args.e2e_http_url:
            http = E2EHttpRunner(args.e2e_http_url)
            await http.start()
            e2e_runners.append(http)
            e2e_paths.append(("e2e_http", http))

        paths: list[tuple[str, PathRunner]] = [
            ("native", native),
            ("server_side", ServerSideRunner(database)),
            *e2e_paths,
        ]
        decomposer = DecompositionRunner(database)
        native_db_ns_by_query: dict[str, float] = {}
        for query in all_queries():
            for label, runner in paths:
                warm, cold = await _sample_path(runner, query, args.iterations)
                # Attach the overhead waterfall to the server-side warm row —
                # the one the report reads t_db from for the native comparison.
                decomposition = (
                    await _sample_decomposition(decomposer, query, args.iterations) if label == "server_side" else None
                )
                results.append(_row(label, query, "warm", warm, decomposition))
                results.append(_row(label, query, "cold", [cold]))
            # The native DB segment (execute + fetch only) anchors t_db == native.
            native_db_ns_by_query[query.id] = await _sample_native_db(native, query, args.iterations)
    finally:
        for e2e in e2e_runners:
            await e2e.close()
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
    return PerfRun(
        metadata=metadata,
        results=results,
        assertions=_assertions(results, native_db_ns_by_query),
    )


def _assertions(results: list[ResultRow], native_db_ns_by_query: dict[str, float]) -> list[Assertion]:
    """Overhead + the load-bearing ``t_db == native`` gate.

    Two assertions per query: an informational warm total-latency delta
    (``server_side_overhead_p50_ms``), and the machine-checkable claim that the
    server path's DB segment matches the native baseline
    (``t_db_matches_native``) — the result the whole performance objective turns
    on.
    """
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
                passed=True,  # informational
                detail={
                    "native_p50_ms": native.latency_ms.p50,
                    "server_side_p50_ms": server.latency_ms.p50,
                    "overhead_p50_ms": overhead_ms,
                },
            )
        )
        server_t_db = server.decomposition_ns.t_db if server.decomposition_ns else None
        native_t_db = native_db_ns_by_query.get(qid)
        if server_t_db is not None and native_t_db is not None:
            out.append(
                Assertion(
                    name="t_db_matches_native",
                    query_id=qid,
                    passed=t_db_within_native(server_t_db, native_t_db),
                    detail={
                        "native_t_db_ms": native_t_db / 1e6,
                        "server_t_db_ms": server_t_db / 1e6,
                        "delta_ms": (server_t_db - native_t_db) / 1e6,
                    },
                )
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPg performance benchmark (native vs server-side).")
    parser.add_argument("--database-url", required=True, help="PostgreSQL DSN (a TPC-H-loaded database).")
    parser.add_argument(
        "--iterations", type=int, default=50, help=f"Warm iterations per pathxquery (>= {_WARMUP + 1})."
    )
    parser.add_argument("--scale-factor", type=int, default=1, help="TPC-H scale factor the DB was loaded at.")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the result JSON.")
    parser.add_argument(
        "--e2e",
        action="store_true",
        help="Also measure the end-to-end paths through the MCP protocol (in-memory + stdio subprocess).",
    )
    parser.add_argument(
        "--e2e-http-url",
        default=None,
        help="Add the streamable-HTTP e2e path against an operator-started mcpg server at this URL (e.g. "
        "http://127.0.0.1:8000/mcp). Implies the HTTP transport is already running.",
    )
    parser.add_argument("--git-sha", default="unknown", help="Provenance: the commit under test.")
    parser.add_argument("--timestamp", default="unknown", help="Provenance: ISO-8601 run timestamp.")
    args = parser.parse_args(argv)
    if args.iterations < _WARMUP + 1:
        # Below this the warm bucket is empty after dropping warmup, and
        # summarize() would report misleading all-zero stats that look valid.
        parser.error(f"--iterations must be >= {_WARMUP + 1} (warm measurements would be empty)")

    run = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(run.to_dict(), indent=2) + "\n")
    print(f"wrote {args.output} ({len(run.results)} result rows)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
