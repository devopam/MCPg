"""Structured result schema for a performance run.

Frozen dataclasses serialized with ``dataclasses.asdict`` (repo convention).
One JSON file per run under ``benchmarks/results/``. Provenance
(``timestamp`` / ``git_sha`` / host / versions) is **passed in** by the caller
so a published number always carries the exact conditions that produced it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# The measurement paths (see perf/paths.py).
Path = str  # "native" | "server_side" | "e2e_inmemory" | "e2e_stdio" | "e2e_http"


@dataclass(frozen=True)
class Decomposition:
    """The overhead waterfall, in nanoseconds (medians). ``None`` where N/A."""

    t_parse: float | None = None
    t_pool: float | None = None
    t_txn: float | None = None
    t_db: float | None = None
    t_serialize: float | None = None
    t_protocol: float | None = None
    t_cache: float | None = None


@dataclass(frozen=True)
class LatencyBlock:
    """Reported latency in milliseconds (from perf/stats.LatencySummary)."""

    p50: float
    p95: float
    p99: float
    mean: float
    stdev: float
    min: float
    max: float
    median_ci95: tuple[float, float]


@dataclass(frozen=True)
class ResultRow:
    """One (path, query, temperature, concurrency) measurement."""

    path: Path
    query_id: str
    compute_class: str
    result_size: str
    temperature: str  # "cold" | "warm"
    concurrency: int
    n: int
    latency_ms: LatencyBlock
    throughput_rps: float | None = None
    samples_ns: list[int] = field(default_factory=list)
    decomposition_ns: Decomposition | None = None


@dataclass(frozen=True)
class Assertion:
    """A machine-checkable claim (the load-bearing one: t_db == native)."""

    name: str
    query_id: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PerfRun:
    """A full performance run — the top-level JSON document."""

    metadata: dict[str, Any]
    results: list[ResultRow]
    assertions: list[Assertion] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    kind: str = "performance"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
