"""Observability — Prometheus-format metrics for tool calls.

Lightweight in-process metrics that record per-tool call counts and
latency distribution. No external dependency: the
:func:`render_prometheus` helper emits the standard text exposition
format directly, which any Prometheus scraper consumes.

Three series are exported:

- ``mcpg_tool_calls_total{tool, status}`` — counter, one increment per
  ``call_tool`` invocation. ``status`` is ``ok`` or ``error``.
- ``mcpg_tool_duration_seconds_bucket{tool, le}`` — histogram of
  per-tool wall-clock time (seconds). Default buckets match Prometheus'
  ``DEF_BUCKETS`` plus a 30s and 60s overflow.
- ``mcpg_tool_duration_seconds_sum{tool}`` /
  ``mcpg_tool_duration_seconds_count{tool}`` — totals to match the
  histogram so the standard
  ``rate(..._sum[1m]) / rate(..._count[1m])`` query computes the mean.

A single module-level :class:`Metrics` instance backs the
:class:`mcpg.server.AuditedFastMCP` hook. Tests get a fresh instance
via :func:`reset_metrics` so cross-test pollution can't accumulate.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Final

# Histogram buckets in seconds. Matches Prometheus DEF_BUCKETS plus
# two overflow lanes so a slow pg_dump that takes 30+ seconds still
# lands somewhere meaningful instead of just "+Inf".
DEFAULT_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


@dataclass
class _Histogram:
    """Per-tool histogram state — bucket counts + cumulative sum/count."""

    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    counts: list[int] = field(default_factory=lambda: [0] * len(DEFAULT_BUCKETS))
    sum: float = 0.0
    count: int = 0

    def observe(self, value: float) -> None:
        self.count += 1
        self.sum += value
        for index, upper in enumerate(self.buckets):
            if value <= upper:
                self.counts[index] += 1


class Metrics:
    """Thread-safe per-tool counter + histogram store.

    The MCP server is async-single-threaded in practice but the
    ``threading.Lock`` is cheap insurance against any future
    multi-thread access (e.g. a Prometheus scraper running in a
    background thread).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (tool, status) -> count
        self._calls: dict[tuple[str, str], int] = {}
        # tool -> histogram of durations
        self._durations: dict[str, _Histogram] = {}

    def record_call(self, tool: str, status: str, duration_seconds: float) -> None:
        """Record one ``call_tool`` event.

        Args:
            tool: Tool name (the MCP-side identifier).
            status: ``ok`` on success, ``error`` when the tool raised.
            duration_seconds: Wall-clock time the call took.
        """
        with self._lock:
            self._calls[(tool, status)] = self._calls.get((tool, status), 0) + 1
            hist = self._durations.setdefault(tool, _Histogram())
            hist.observe(duration_seconds)

    def snapshot(self) -> tuple[dict[tuple[str, str], int], dict[str, _Histogram]]:
        """Return a defensive copy of the current counter + histogram state."""
        with self._lock:
            return (
                dict(self._calls),
                {
                    tool: _Histogram(buckets=h.buckets, counts=list(h.counts), sum=h.sum, count=h.count)
                    for tool, h in self._durations.items()
                },
            )


# Module-level singleton — the server's AuditedFastMCP records into it.
_metrics = Metrics()


def get_metrics() -> Metrics:
    """Return the module-level Metrics instance."""
    return _metrics


def reset_metrics() -> None:
    """Replace the singleton with a fresh Metrics — test isolation hook."""
    global _metrics
    _metrics = Metrics()


def _escape_label_value(value: str) -> str:
    """Escape a Prometheus label value per the text exposition format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(metrics: Metrics | None = None) -> str:
    """Render the metrics as Prometheus text exposition (v0.0.4) format.

    Returns a string ready to serve from a ``/metrics`` endpoint. Empty
    state still produces valid output: each series ends in zero counts.
    """
    target = metrics if metrics is not None else _metrics
    calls, durations = target.snapshot()
    lines: list[str] = []

    # mcpg_tool_calls_total
    lines.append("# HELP mcpg_tool_calls_total Total MCP tool invocations partitioned by tool and outcome.")
    lines.append("# TYPE mcpg_tool_calls_total counter")
    for (tool, status), value in sorted(calls.items()):
        tool_label = _escape_label_value(tool)
        status_label = _escape_label_value(status)
        lines.append(f'mcpg_tool_calls_total{{tool="{tool_label}",status="{status_label}"}} {value}')

    # mcpg_tool_duration_seconds (histogram + _sum + _count)
    lines.append("# HELP mcpg_tool_duration_seconds Wall-clock duration of MCP tool invocations, in seconds.")
    lines.append("# TYPE mcpg_tool_duration_seconds histogram")
    for tool in sorted(durations):
        hist = durations[tool]
        tool_label = _escape_label_value(tool)
        # _Histogram.observe stores cumulative counts already (a value
        # increments every bucket whose upper bound it satisfies), so
        # emit them directly — no re-accumulation needed.
        for index, upper in enumerate(hist.buckets):
            lines.append(f'mcpg_tool_duration_seconds_bucket{{tool="{tool_label}",le="{upper}"}} {hist.counts[index]}')
        # +Inf bucket always matches the total count.
        lines.append(f'mcpg_tool_duration_seconds_bucket{{tool="{tool_label}",le="+Inf"}} {hist.count}')
        lines.append(f'mcpg_tool_duration_seconds_sum{{tool="{tool_label}"}} {hist.sum}')
        lines.append(f'mcpg_tool_duration_seconds_count{{tool="{tool_label}"}} {hist.count}')

    return "\n".join(lines) + "\n"
