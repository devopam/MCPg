"""``AIO`` — PG 19 asynchronous-I/O subsystem coverage.

PG 19 introduces a true async-I/O subsystem. The headline knob is the
``io_method`` GUC, which selects between ``sync`` (the historical
behaviour), ``worker`` (a pool of background workers issues I/O), and
``io_uring`` (kernel-side completion queues on Linux 5.1+). The default
in PG 19 is ``worker`` with adaptive scaling controlled by
``io_min_workers`` / ``io_max_workers``.

Operators don't intuitively know which ``io_method`` is right for their
workload. This module ships two tools:

* ``get_aio_status`` — version probe; never raises. Reports whether the
  subsystem is available and the current setting values.
* ``recommend_io_method`` — the advisor. Inspects ``pg_stat_io``,
  ``pg_stat_database`` (cache pressure), and the configured GUCs, then
  emits a recommendation (``io_uring`` / ``worker`` / ``sync`` /
  ``current_optimal``) with a stable reason code.

The advisor is read-only — it never changes ``io_method`` itself.
Operators apply the recommendation manually via ``ALTER SYSTEM`` or a
postgresql.conf edit; the returned ``ready_to_run_sql`` carries the
canonical ``ALTER SYSTEM SET io_method = '…'`` so the agent can hand
it to the operator verbatim.

Backward compatibility
----------------------
This module is additive. The existing ``read_pg_stat_io`` tool in
``mcpg.io_stats`` keeps its current return shape — any new PG 19
columns it surfaces appear as additional fields with safe defaults
(per the no-deprecation rule documented in
``docs/plans/pg19-readiness.md``).

Security posture
----------------
Read-only; no DDL, no identifier substitution into SQL. PG 19+ gate via
``server_version_num >= 190000``; on older servers the advisor reports
``available=false`` with a guidance string pointing at the existing
maintenance / IO-stat tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg._vendor.sql import SqlDriver

# PG 19 ships the AIO subsystem. The version-num probe is the boundary
# guard — no extension to install.
_MIN_AIO_VERSION = 190000

# Allowed io_method values per the PG 19 docs. Used both for parsing
# pg_settings and for the recommendation enum.
_VALID_IO_METHODS = frozenset({"sync", "worker", "io_uring"})

# Heuristic thresholds. Inline so the advisor and the docstring share
# one source of truth.

# Cache miss ratio = read_bytes / (read_bytes + hit_bytes). Above this
# the workload is "I/O-pressured" — worth recommending an async method.
_HIGH_CACHE_MISS_RATIO = 0.30

# Reads per second below which the workload is too light to bother
# recommending a method change. Avoids "io_uring all the things".
_MIN_READS_PER_SECOND = 50.0

# Reads per second above which io_uring (where supported) really shines
# — kernel-side completion queues amortise the syscall overhead.
_HIGH_READ_RATE_THRESHOLD = 5_000.0

# Minimum recent stats window to be confident in the recommendation
# (in seconds since the most recent ``stats_reset``).
_MIN_STATS_WINDOW_SECONDS = 300.0


class AioError(Exception):
    """Raised when an AIO operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AioStatus:
    """Reports whether the PG 19 AIO subsystem is usable on this server.

    ``available`` is True when ``server_version_num`` >= 190000.
    ``io_method``, ``io_min_workers``, ``io_max_workers`` are the current
    setting values (``None`` when unavailable). ``detail`` is a
    human-readable guidance string suitable for surfacing to an agent.
    """

    available: bool
    server_version_num: int
    server_version: str
    io_method: str | None
    io_min_workers: int | None
    io_max_workers: int | None
    detail: str


@dataclass(frozen=True)
class IoMethodRecommendation:
    """One recommendation row from :func:`recommend_io_method`.

    ``recommended_method`` is one of ``io_uring`` / ``worker`` / ``sync``.
    ``reason`` is a stable identifier the agent can react to:

    * ``high_concurrent_read_load`` — high read-rate + high miss-ratio →
      io_uring on supported kernels.
    * ``bursty_io_with_cache_pressure`` — moderate read-rate + high
      miss-ratio → worker (the PG 19 default; bias safely).
    * ``low_io_pressure`` — workload doesn't touch enough I/O to
      justify async; sync is fine.
    * ``current_setting_optimal`` — already on the recommended method;
      no change suggested.
    * ``insufficient_stats`` — pg_stat_io window too short to give a
      confident recommendation.

    ``ready_to_run_sql`` is the canonical ``ALTER SYSTEM SET …`` operators
    paste; only emitted when the recommendation differs from the current
    setting.
    """

    recommended_method: str
    reason: str
    current_method: str | None
    cache_miss_ratio: float
    reads_per_second: float
    stats_window_seconds: float
    ready_to_run_sql: str | None


@dataclass(frozen=True)
class RecommendIoMethodResult:
    """Roll-up of :func:`recommend_io_method` — the recommendation plus
    the AIO status that produced it.

    Always returns exactly one ``recommendation``; the list shape is for
    forward-compat if future PG versions partition the recommendation
    per backend type.
    """

    available: bool
    server_version_num: int
    detail: str
    recommendations: list[IoMethodRecommendation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared probes
# ---------------------------------------------------------------------------


async def _server_version(driver: SqlDriver) -> tuple[int, str]:
    """Return ``(server_version_num, server_version)`` in one round trip."""
    rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::int AS ver_num, current_setting('server_version') AS ver",
        force_readonly=True,
    )
    if not rows:
        return 0, ""
    cells = rows[0].cells
    return int(cells.get("ver_num") or 0), str(cells.get("ver") or "")


async def _aio_settings(driver: SqlDriver) -> tuple[str | None, int | None, int | None]:
    """Probe pg_settings for ``io_method`` / ``io_min_workers`` / ``io_max_workers``.

    Returns ``(None, None, None)`` when the settings aren't recognised
    (older PG, or PG 19 build without AIO enabled). We use a single
    ``current_setting(..., true)`` per knob because pg_settings is too
    broad and the ``missing_ok`` form (``true``) returns ``NULL`` rather
    than ERROR when a knob doesn't exist.
    """
    rows = await driver.execute_query(
        "SELECT current_setting('io_method', true) AS method, "
        "current_setting('io_min_workers', true) AS min_w, "
        "current_setting('io_max_workers', true) AS max_w",
        force_readonly=True,
    )
    if not rows:
        return None, None, None
    cells = rows[0].cells
    method = cells.get("method")
    if method is not None and method not in _VALID_IO_METHODS:
        # Unrecognised value — better to surface as unavailable than to
        # pass through an unknown string.
        method = None
    min_w = cells.get("min_w")
    max_w = cells.get("max_w")
    try:
        min_w_int = int(min_w) if min_w is not None else None
    except (TypeError, ValueError):
        min_w_int = None
    try:
        max_w_int = int(max_w) if max_w is not None else None
    except (TypeError, ValueError):
        max_w_int = None
    return method, min_w_int, max_w_int


# ---------------------------------------------------------------------------
# Status — never raises
# ---------------------------------------------------------------------------


async def get_aio_status(driver: SqlDriver) -> AioStatus:
    """Report whether the PG 19 AIO subsystem is usable.

    Read-only; never raises. On PG < 19 returns ``available=False`` with
    a diagnostic pointing the agent at the existing ``read_pg_stat_io`` /
    ``run_maintenance`` tools. The version probe and settings lookup are
    wrapped in ``try/except`` so a transient driver error here still
    yields a clean ``available=False`` result instead of crashing the
    tool call.
    """
    try:
        ver_num, ver = await _server_version(driver)
    except Exception as exc:
        return AioStatus(
            available=False,
            server_version_num=0,
            server_version="",
            io_method=None,
            io_min_workers=None,
            io_max_workers=None,
            detail=(
                f"AIO status is unavailable (server version probe failed: {exc}). "
                "Fall back to read_pg_stat_io / run_maintenance for I/O observability "
                "and tuning on older servers."
            ),
        )
    if ver_num < _MIN_AIO_VERSION:
        return AioStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            io_method=None,
            io_min_workers=None,
            io_max_workers=None,
            detail=(
                "PG 19's async-I/O subsystem requires PostgreSQL 19 or newer; "
                "this server is older. Use read_pg_stat_io for I/O observability "
                "on PG ≤ 18."
            ),
        )
    try:
        method, min_w, max_w = await _aio_settings(driver)
    except Exception as exc:
        return AioStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            io_method=None,
            io_min_workers=None,
            io_max_workers=None,
            detail=(
                f"AIO subsystem is reachable but settings probe failed: {exc}. Re-run after the server is back online."
            ),
        )
    if method is None:
        # PG ≥ 19 but the `io_method` GUC isn't recognised — server was
        # built without AIO, or returned an unrecognised value. Surface
        # available=False so the agent doesn't try to ALTER SYSTEM SET
        # something that will fail (gemini review on PR #131).
        return AioStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            io_method=None,
            io_min_workers=None,
            io_max_workers=None,
            detail=(
                "PG 19 async-I/O GUC 'io_method' is not available on this server. "
                "The server may have been compiled without AIO support, or it returned "
                "an unrecognised value. Fall back to read_pg_stat_io / run_maintenance."
            ),
        )
    return AioStatus(
        available=True,
        server_version_num=ver_num,
        server_version=ver,
        io_method=method,
        io_min_workers=min_w,
        io_max_workers=max_w,
        detail=(
            f"AIO subsystem is available. Current io_method={method} "
            f"(io_min_workers={min_w}, io_max_workers={max_w}). "
            "Call recommend_io_method to get a workload-aware recommendation."
        ),
    )


# ---------------------------------------------------------------------------
# Advisor — recommend_io_method
# ---------------------------------------------------------------------------


async def _io_pressure(driver: SqlDriver) -> tuple[float, float, float]:
    """Probe pg_stat_io / pg_stat_database for cache pressure and read rate.

    Returns ``(cache_miss_ratio, reads_per_second, stats_window_seconds)``.

    Uses ``pg_stat_database`` aggregates (``blks_read`` / ``blks_hit`` /
    ``stats_reset``) because they're stable across PG 14+; PG 19 augments
    ``pg_stat_io`` but we don't depend on the new columns here so the
    advisor still works while pg_stat_io is being reshaped. Aggregates
    across all non-template databases.
    """
    # Use COALESCE(MIN(stats_reset), pg_postmaster_start_time()) so the
    # window doesn't collapse to 0 on a cluster where stats have never
    # been reset (the default state on a fresh install) — gemini review
    # on PR #131. pg_postmaster_start_time() is the correct upper bound
    # on accumulated stats when stats_reset is NULL.
    rows = await driver.execute_query(
        "SELECT "
        "  COALESCE(SUM(blks_read), 0) AS reads, "
        "  COALESCE(SUM(blks_hit), 0) AS hits, "
        "  EXTRACT(epoch FROM (now() - COALESCE(MIN(stats_reset), pg_postmaster_start_time()))) "
        "    AS window_seconds "
        "FROM pg_stat_database "
        "WHERE datname IS NOT NULL "
        "  AND datname NOT IN ('template0', 'template1')",
        force_readonly=True,
    )
    if not rows:
        return 0.0, 0.0, 0.0
    cells = rows[0].cells
    reads = int(cells.get("reads") or 0)
    hits = int(cells.get("hits") or 0)
    window_seconds = float(cells.get("window_seconds") or 0)
    total = reads + hits
    miss_ratio = (reads / total) if total > 0 else 0.0
    reads_per_second = (reads / window_seconds) if window_seconds > 0 else 0.0
    return miss_ratio, reads_per_second, window_seconds


def _classify_io_method(
    *,
    current_method: str | None,
    cache_miss_ratio: float,
    reads_per_second: float,
    stats_window_seconds: float,
) -> tuple[str, str]:
    """Map workload signals to ``(recommended_method, reason)``.

    The decision tree is deliberately conservative — never recommends
    ``io_uring`` for low-traffic workloads (the syscall amortisation
    pays off only at scale), and never recommends ``sync`` for already
    I/O-pressured workloads (it would regress).
    """
    if stats_window_seconds < _MIN_STATS_WINDOW_SECONDS:
        return current_method or "worker", "insufficient_stats"
    if reads_per_second < _MIN_READS_PER_SECOND:
        return "sync", "low_io_pressure"
    if reads_per_second >= _HIGH_READ_RATE_THRESHOLD and cache_miss_ratio >= _HIGH_CACHE_MISS_RATIO:
        return "io_uring", "high_concurrent_read_load"
    if cache_miss_ratio >= _HIGH_CACHE_MISS_RATIO:
        return "worker", "bursty_io_with_cache_pressure"
    # I/O pressure exists but cache is doing its job — current default
    # (worker on PG 19) is fine. Don't churn.
    return current_method or "worker", "current_setting_optimal"


async def recommend_io_method(driver: SqlDriver) -> RecommendIoMethodResult:
    """Recommend a PG 19 ``io_method`` for this workload.

    Inspects ``pg_stat_database`` aggregates (blks_read / blks_hit /
    stats_reset) and the current ``io_method`` setting, then maps the
    workload signals to one of ``io_uring`` / ``worker`` / ``sync``.

    Read-only — never invokes ``ALTER SYSTEM``. The result carries a
    ``ready_to_run_sql`` snippet operators can paste manually when the
    recommendation differs from the current setting.

    Returns an empty ``recommendations`` list with a clear ``detail``
    string on PG < 19.
    """
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_AIO_VERSION:
        return RecommendIoMethodResult(
            available=False,
            server_version_num=ver_num,
            detail=(
                "PG 19's async-I/O subsystem requires PostgreSQL 19 or newer; "
                f"this server reports {ver or 'unknown'} (server_version_num={ver_num})."
            ),
            recommendations=[],
        )
    current_method, _, _ = await _aio_settings(driver)
    if current_method is None:
        # GUC missing / unrecognised — server was built without AIO, or
        # returned a value we don't know about. Bail early rather than
        # recommend a change that will fail (gemini review on PR #131).
        return RecommendIoMethodResult(
            available=False,
            server_version_num=ver_num,
            detail=(
                "PG 19 async-I/O GUC 'io_method' is not available on this server. "
                "The server may have been compiled without AIO support, or it "
                "returned an unrecognised value. Fall back to read_pg_stat_io / "
                "run_maintenance for I/O observability and tuning."
            ),
            recommendations=[],
        )
    cache_miss_ratio, reads_per_second, stats_window_seconds = await _io_pressure(driver)
    recommended_method, reason = _classify_io_method(
        current_method=current_method,
        cache_miss_ratio=cache_miss_ratio,
        reads_per_second=reads_per_second,
        stats_window_seconds=stats_window_seconds,
    )
    ready_sql: str | None = None
    if recommended_method != current_method:
        # ALTER SYSTEM SET is the canonical persistent-setting path.
        # Single-quoted value, no identifier interpolation — safe.
        ready_sql = f"ALTER SYSTEM SET io_method = '{recommended_method}';"
    rec = IoMethodRecommendation(
        recommended_method=recommended_method,
        reason=reason,
        current_method=current_method,
        cache_miss_ratio=round(cache_miss_ratio, 3),
        reads_per_second=round(reads_per_second, 1),
        stats_window_seconds=round(stats_window_seconds, 1),
        ready_to_run_sql=ready_sql,
    )
    return RecommendIoMethodResult(
        available=True,
        server_version_num=ver_num,
        detail=(
            f"Recommended io_method={recommended_method} for current workload "
            f"(reason={reason}, reads/s={reads_per_second:.1f}, "
            f"cache_miss_ratio={cache_miss_ratio:.3f})."
        ),
        recommendations=[rec],
    )


__all__ = [
    "AioError",
    "AioStatus",
    "IoMethodRecommendation",
    "RecommendIoMethodResult",
    "get_aio_status",
    "recommend_io_method",
]
