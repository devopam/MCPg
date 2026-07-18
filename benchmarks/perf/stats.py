"""Statistical aggregation for the performance harness.

Pure functions over sample arrays (nanosecond integers from
``time.perf_counter_ns()``). No I/O, no database — unit-tested. The harness
records raw samples in the result JSON; these turn them into the reported
percentiles + a bootstrap confidence interval on the median (robust, no
normality assumption).
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class LatencySummary:
    """Summary of one sample array, in **milliseconds** (converted from ns)."""

    n: int
    p50: float
    p95: float
    p99: float
    mean: float
    stdev: float
    min: float
    max: float
    median_ci95: tuple[float, float]


def percentile(sorted_ns: list[int], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted ns array (0 < pct <= 100)."""
    if not sorted_ns:
        return 0.0
    import math

    rank = max(0, min(len(sorted_ns) - 1, math.ceil(pct / 100.0 * len(sorted_ns)) - 1))
    return float(sorted_ns[rank])


def _median(sorted_ns: list[int]) -> float:
    n = len(sorted_ns)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(sorted_ns[mid])
    return (sorted_ns[mid - 1] + sorted_ns[mid]) / 2.0


def bootstrap_median_ci(samples_ns: list[int], *, resamples: int = 10_000, seed: int = 0) -> tuple[float, float]:
    """95% CI on the median via seeded bootstrap resampling (returns ns).

    Seeded so a published number is reproducible. Returns ``(median, median)``
    for a degenerate (<2) sample.
    """
    if len(samples_ns) < 2:
        m = _median(sorted(samples_ns))
        return (m, m)
    rng = random.Random(seed)
    n = len(samples_ns)
    medians: list[float] = []
    for _ in range(resamples):
        resample = sorted(samples_ns[rng.randrange(n)] for _ in range(n))
        medians.append(_median(resample))
    medians.sort()
    lo = percentile([int(m) for m in medians], 2.5)
    hi = percentile([int(m) for m in medians], 97.5)
    return (lo, hi)


def summarize(samples_ns: list[int], *, bootstrap_seed: int = 0) -> LatencySummary:
    """Summarize a sample array into reported milliseconds."""
    import statistics as _stats

    n = len(samples_ns)
    ordered = sorted(samples_ns)
    ns_to_ms = 1e-6
    ci_lo_ns, ci_hi_ns = bootstrap_median_ci(samples_ns, seed=bootstrap_seed)
    return LatencySummary(
        n=n,
        p50=percentile(ordered, 50) * ns_to_ms,
        p95=percentile(ordered, 95) * ns_to_ms,
        p99=percentile(ordered, 99) * ns_to_ms,
        mean=(_stats.fmean(samples_ns) * ns_to_ms) if n else 0.0,
        stdev=(_stats.stdev(samples_ns) * ns_to_ms) if n > 1 else 0.0,
        min=(float(ordered[0]) * ns_to_ms) if n else 0.0,
        max=(float(ordered[-1]) * ns_to_ms) if n else 0.0,
        median_ci95=(ci_lo_ns * ns_to_ms, ci_hi_ns * ns_to_ms),
    )


def drop_warmup(samples_ns: list[int], warmup: int) -> list[int]:
    """Discard the first ``warmup`` samples (pool priming, plan cache, first-call)."""
    return samples_ns[warmup:] if warmup < len(samples_ns) else []
