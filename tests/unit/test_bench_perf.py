"""Unit tests for the benchmark suite's pure core (roadmap 19, Phase 1).

Covers the deterministic, DB-free parts — the statistics, the query taxonomy,
and the result schema serialization. The DB-touching paths (paths.py, runner.py)
are operator tools run against a live PostgreSQL, not unit-tested (matching
benchmarks/bench.py).
"""

from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult, TextContent

from benchmarks.perf import queries, runner, stats
from benchmarks.perf.decompose import SegmentSample, summarize_segments, t_db_within_native
from benchmarks.perf.e2e import row_count_of
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


# --- runner CLI validation (DB-free; fails before any connection) ---------


def test_runner_rejects_too_few_iterations() -> None:
    # Below _WARMUP + 1 the warm bucket is empty after dropping warmup, so the
    # guard must fail fast (SystemExit from argparse) before touching the DB.
    with pytest.raises(SystemExit) as exc:
        runner.main(["--database-url", "postgresql://unused", "--output", "/tmp/unused.json", "--iterations", "0"])
    assert exc.value.code == 2


# --- overhead decomposition (pure helpers) --------------------------------


def _seg(t_db: int) -> SegmentSample:
    # Fixed non-db segments; vary only t_db for the median assertions.
    return SegmentSample(t_parse=100, t_pool=200, t_txn=300, t_db=t_db, t_serialize=400)


def test_summarize_segments_medians_per_field() -> None:
    samples = [_seg(1000), _seg(2000), _seg(3000)]
    d = summarize_segments(samples)
    assert d.t_parse == 100
    assert d.t_pool == 200
    assert d.t_txn == 300
    assert d.t_db == 2000  # median of 1000/2000/3000
    assert d.t_serialize == 400


def test_summarize_segments_drops_warmup() -> None:
    # First two are warmup outliers; only the last three count.
    samples = [_seg(9_000_000), _seg(9_000_000), _seg(1000), _seg(2000), _seg(3000)]
    d = summarize_segments(samples, warmup=2)
    assert d.t_db == 2000


def test_summarize_segments_empty_is_all_none() -> None:
    d = summarize_segments([], warmup=0)
    assert d.t_db is None and d.t_parse is None


def test_t_db_within_native_matches_when_close() -> None:
    # 1 ms native, server 1.1 ms -> within 30% relative tolerance.
    assert t_db_within_native(1_100_000, 1_000_000) is True


def test_t_db_within_native_absolute_floor_covers_jitter() -> None:
    # Sub-ms native: a large *relative* wobble under the 0.2 ms floor passes.
    assert t_db_within_native(150_000, 50_000) is True  # delta 0.1 ms < floor
    # ...but a delta beyond the floor on a tiny value fails.
    assert t_db_within_native(500_000, 50_000) is False


def test_t_db_within_native_fails_when_far() -> None:
    # 10 ms native, server 15 ms -> 50% over, well beyond tolerance.
    assert t_db_within_native(15_000_000, 10_000_000) is False


def test_assertions_include_t_db_gate() -> None:
    # A server-side warm row carrying a decomposition + a native anchor should
    # yield the machine-checkable t_db_matches_native assertion.
    def _row(path: str, t_db: float | None) -> ResultRow:
        return ResultRow(
            path=path,
            query_id="tpch_q1",
            compute_class="heavy",
            result_size="~100",
            temperature="warm",
            concurrency=1,
            n=20,
            latency_ms=LatencyBlock(
                p50=5.0, p95=6.0, p99=6.5, mean=5.1, stdev=0.3, min=4.5, max=7.0, median_ci95=(4.9, 5.2)
            ),
            samples_ns=[5_000_000],
            decomposition_ns=Decomposition(t_db=t_db) if t_db is not None else None,
        )

    results = [_row("native", None), _row("server_side", 4_000_000.0)]
    out = runner._assertions(results, {"tpch_q1": 4_100_000.0})
    gate = [a for a in out if a.name == "t_db_matches_native"]
    assert len(gate) == 1
    assert gate[0].passed is True
    assert gate[0].detail["native_t_db_ms"] == 4.1


# --- e2e helper (pure) ----------------------------------------------------


def test_row_count_of_reads_structured_content() -> None:
    result = CallToolResult(content=[], structuredContent={"row_count": 42})
    assert row_count_of(result) == 42


def test_row_count_of_none_when_absent() -> None:
    # A text-only result (no structuredContent) yields None rather than raising.
    result = CallToolResult(content=[TextContent(type="text", text="[]")])
    assert row_count_of(result) is None
