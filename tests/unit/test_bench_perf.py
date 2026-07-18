"""Unit tests for the benchmark suite's pure core (roadmap 19, Phase 1).

Covers the deterministic, DB-free parts — the statistics, the query taxonomy,
and the result schema serialization. The DB-touching paths (paths.py, runner.py)
are operator tools run against a live PostgreSQL, not unit-tested (matching
benchmarks/bench.py).
"""

from __future__ import annotations

import json

from benchmarks.perf import queries, stats
from benchmarks.perf.schema import Assertion, Decomposition, LatencyBlock, PerfRun, ResultRow

# --- stats ----------------------------------------------------------------


def test_percentile_nearest_rank() -> None:
    data = sorted([10, 20, 30, 40, 50])
    assert stats.percentile(data, 50) == 30
    assert stats.percentile(data, 100) == 50
    assert stats.percentile([], 50) == 0.0


def test_summarize_converts_ns_to_ms_and_reports_percentiles() -> None:
    # 1_000_000 ns == 1 ms.
    samples = [1_000_000, 2_000_000, 3_000_000, 4_000_000, 5_000_000]
    s = stats.summarize(samples)
    assert s.n == 5
    assert s.p50 == 3.0
    assert s.min == 1.0
    assert s.max == 5.0
    lo, hi = s.median_ci95
    assert lo <= s.p50 <= hi


def test_bootstrap_median_ci_is_seeded_reproducible() -> None:
    samples = [5, 7, 7, 8, 9, 10, 12, 15, 20, 100]
    a = stats.bootstrap_median_ci(samples, seed=42)
    b = stats.bootstrap_median_ci(samples, seed=42)
    assert a == b  # same seed → identical CI
    assert a[0] <= a[1]


def test_bootstrap_median_ci_degenerate_sample() -> None:
    assert stats.bootstrap_median_ci([42]) == (42.0, 42.0)
    assert stats.bootstrap_median_ci([]) == (0.0, 0.0)


def test_drop_warmup() -> None:
    assert stats.drop_warmup([1, 2, 3, 4, 5], 2) == [3, 4, 5]
    assert stats.drop_warmup([1, 2], 5) == []


# --- query taxonomy -------------------------------------------------------


def test_query_set_spans_both_axes() -> None:
    qs = queries.all_queries()
    assert len(qs) >= 8
    ids = {q.id for q in qs}
    assert len(ids) == len(qs)  # ids are unique
    classes = {q.compute_class for q in qs}
    assert {"ultralight", "light", "heavy"} <= classes
    sizes = {q.result_size for q in qs}
    assert {"1", "~100", "large"} <= sizes
    # The large-result case raises max_rows so serialization is exercised.
    large = next(q for q in qs if q.result_size == "large")
    assert large.max_rows >= 100_000
    # Every query is a read-only SELECT (no writes sneak into the harness).
    for q in qs:
        assert q.sql.lstrip().upper().startswith("SELECT")


def test_heavy_tier_includes_tpch() -> None:
    heavy = [q for q in queries.all_queries() if q.compute_class == "heavy"]
    assert any(q.id.startswith("tpch_") for q in heavy)


# --- schema serialization -------------------------------------------------


def test_perf_run_serializes_to_json() -> None:
    row = ResultRow(
        path="server_side",
        query_id="tpch_q1",
        compute_class="heavy",
        result_size="~100",
        temperature="warm",
        concurrency=1,
        n=50,
        latency_ms=LatencyBlock(
            p50=1.2, p95=1.9, p99=2.1, mean=1.3, stdev=0.2, min=1.0, max=2.5, median_ci95=(1.1, 1.3)
        ),
        samples_ns=[1_200_000, 1_300_000],
        decomposition_ns=Decomposition(t_parse=1000.0, t_db=800_000.0),
    )
    run = PerfRun(
        metadata={"timestamp": "2026-07-18T00:00:00Z", "scale_factor": 1},
        results=[row],
        assertions=[Assertion(name="server_side_overhead_p50_ms", query_id="tpch_q1", passed=True, detail={})],
    )
    payload = json.loads(json.dumps(run.to_dict()))  # round-trips cleanly
    assert payload["schema_version"] == 1
    assert payload["kind"] == "performance"
    assert payload["results"][0]["path"] == "server_side"
    assert payload["results"][0]["decomposition_ns"]["t_db"] == 800_000.0
    assert payload["assertions"][0]["name"] == "server_side_overhead_p50_ms"
