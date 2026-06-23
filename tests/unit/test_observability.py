"""Tests for the observability / Prometheus metrics module."""

from __future__ import annotations

from itertools import pairwise

import pytest

from mcpg.observability import (
    DEFAULT_BUCKETS,
    Metrics,
    _escape_label_value,
    _Histogram,
    get_metrics,
    render_prometheus,
    reset_metrics,
)


@pytest.fixture(autouse=True)
def _reset_metrics_between_tests() -> None:
    """Module singleton state could leak across tests — start clean."""
    reset_metrics()


def test_histogram_observe_increments_every_bucket_whose_upper_bound_includes_the_value() -> None:
    hist = _Histogram()
    hist.observe(0.05)
    # 0.05 should land in buckets [0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]
    # — counts for indexes 0..2 (0.005, 0.01, 0.025) stay at 0; 3+ get +1.
    assert hist.counts[0] == 0  # 0.005
    assert hist.counts[1] == 0  # 0.01
    assert hist.counts[2] == 0  # 0.025
    assert hist.counts[3] == 1  # 0.05
    assert hist.counts[-1] == 1  # 60.0
    assert hist.count == 1
    assert hist.sum == pytest.approx(0.05)


def test_metrics_record_call_counts_per_tool_bucket_status_triple() -> None:
    m = Metrics()
    m.record_call("run_select", "ok", 0.01, bucket="query_execution")
    m.record_call("run_select", "ok", 0.02, bucket="query_execution")
    m.record_call("run_select", "error", 0.005, bucket="query_execution")
    m.record_call("export_query", "ok", 0.4, bucket="data_movement")

    calls, durations = m.snapshot()
    assert calls[("run_select", "query_execution", "ok")] == 2
    assert calls[("run_select", "query_execution", "error")] == 1
    assert calls[("export_query", "data_movement", "ok")] == 1
    # Histograms are per-tool, NOT per-(tool, bucket, status).
    assert durations["run_select"].count == 3
    assert durations["export_query"].count == 1


def test_metrics_record_call_bucket_defaults_to_unknown_for_unrouted_tools() -> None:
    """``classify_tool`` returns None for any tool we haven't routed —
    `record_call` must keep the label dimension cardinality-stable by
    defaulting to "unknown" rather than crashing or carrying None
    through to the Prometheus output."""
    m = Metrics()
    m.record_call("orphan_tool", "ok", 0.01)
    calls, _ = m.snapshot()
    assert calls[("orphan_tool", "unknown", "ok")] == 1


def test_metrics_snapshot_is_defensive_copy() -> None:
    m = Metrics()
    m.record_call("run_select", "ok", 0.01, bucket="query_execution")
    calls, durations = m.snapshot()
    # Mutating the snapshot must not affect the source.
    calls.clear()
    durations["run_select"].counts[0] = 999
    calls_again, durations_again = m.snapshot()
    assert calls_again == {("run_select", "query_execution", "ok"): 1}
    assert durations_again["run_select"].counts[0] != 999


def test_escape_label_value_handles_backslash_quote_and_newline() -> None:
    assert _escape_label_value('hello "world"') == 'hello \\"world\\"'
    assert _escape_label_value("a\\b") == "a\\\\b"
    assert _escape_label_value("a\nb") == "a\\nb"


def test_render_prometheus_emits_help_and_type_comments_with_zero_counters() -> None:
    out = render_prometheus()
    assert "# HELP mcpg_tool_calls_total" in out
    assert "# TYPE mcpg_tool_calls_total counter" in out
    assert "# HELP mcpg_tool_duration_seconds" in out
    assert "# TYPE mcpg_tool_duration_seconds histogram" in out


def test_render_prometheus_emits_one_counter_line_per_observed_tool_bucket_status_triple() -> None:
    m = get_metrics()
    m.record_call("run_select", "ok", 0.1, bucket="query_execution")
    m.record_call("run_select", "error", 0.05, bucket="query_execution")
    m.record_call("run_write", "ok", 1.0, bucket="query_execution")

    out = render_prometheus()
    assert 'mcpg_tool_calls_total{tool="run_select",bucket="query_execution",status="ok"} 1' in out
    assert 'mcpg_tool_calls_total{tool="run_select",bucket="query_execution",status="error"} 1' in out
    assert 'mcpg_tool_calls_total{tool="run_write",bucket="query_execution",status="ok"} 1' in out


def test_render_prometheus_distinguishes_bucket_label_for_same_tool() -> None:
    """Belt + braces — if a tool were ever re-routed mid-session
    (it can't, but the test pins the label dimensionality contract),
    the bucket label cleanly partitions the counts."""
    m = get_metrics()
    m.record_call("renamed_tool", "ok", 0.01, bucket="old_bucket")
    m.record_call("renamed_tool", "ok", 0.01, bucket="new_bucket")

    out = render_prometheus()
    assert 'mcpg_tool_calls_total{tool="renamed_tool",bucket="old_bucket",status="ok"} 1' in out
    assert 'mcpg_tool_calls_total{tool="renamed_tool",bucket="new_bucket",status="ok"} 1' in out


def test_render_prometheus_emits_one_histogram_block_per_observed_tool() -> None:
    m = get_metrics()
    m.record_call("run_select", "ok", 0.02, bucket="query_execution")
    m.record_call("run_select", "ok", 0.06, bucket="query_execution")

    out = render_prometheus()
    assert 'mcpg_tool_duration_seconds_bucket{tool="run_select",le="+Inf"} 2' in out
    assert 'mcpg_tool_duration_seconds_count{tool="run_select"} 2' in out
    # Sum is the float total of all observed durations for this tool.
    assert 'mcpg_tool_duration_seconds_sum{tool="run_select"}' in out


def test_render_prometheus_does_not_re_accumulate_already_cumulative_bucket_counts() -> None:
    # Regression for the cumulative-double-count bug — bucket counts
    # stored by observe() are ALREADY cumulative, so each bucket line
    # should report the count for that bucket directly, not the sum
    # of all prior buckets.
    m = get_metrics()
    m.record_call("run_select", "ok", 0.1)  # lands in 0.1 and every larger bucket
    m.record_call("run_select", "ok", 0.5)  # lands in 0.5 and every larger bucket

    out = render_prometheus()
    # le="0.1" sees only the 0.1 observation -> count 1.
    assert 'mcpg_tool_duration_seconds_bucket{tool="run_select",le="0.1"} 1' in out
    # le="0.5" sees both -> count 2.
    assert 'mcpg_tool_duration_seconds_bucket{tool="run_select",le="0.5"} 2' in out
    # le="+Inf" always matches total count.
    assert 'mcpg_tool_duration_seconds_bucket{tool="run_select",le="+Inf"} 2' in out


def test_render_prometheus_output_ends_with_a_trailing_newline() -> None:
    # Prometheus exposition format requires the body to end with \n.
    out = render_prometheus()
    assert out.endswith("\n")


def test_default_buckets_are_monotonically_increasing() -> None:
    for previous, current in pairwise(DEFAULT_BUCKETS):
        assert previous < current
