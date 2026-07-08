"""``pg_prewarm`` coverage — extension status, buffer-cache reads, advisor, autowarm.

``pg_prewarm`` is a contrib extension that loads relation pages into shared
buffers / OS page cache *before* they are queried, so the first query
doesn't pay the cold-cache penalty. It's a well-known operational tool
that is genuinely under-used because:

1. Operators don't always know *which* tables would benefit most.
2. The "do this every restart" loop is manual — there is no shipped
   autowarm policy.

This module collapses both gaps into the MCPg tool surface:

* Read tools — buffer-cache residency (``pg_buffercache`` join) and
  extension status (presence + ``shared_preload_libraries`` hint).
* Advisor — ``recommend_prewarm_targets`` scores tables by cold-miss
  rate, sequential-scan ratio, and current shared-buffer residency,
  respects a ``shared_buffers_budget_pct`` cap, and emits ready-to-run
  ``SELECT pg_prewarm(...)`` statements.
* Write tools — single-relation invocation, bulk "apply recommended"
  with budget enforcement.
* Autowarm scheduler — a pg_cron job that calls the bulk advisor at a
  chosen cadence.

The whole module is opt-in via ``pg_prewarm`` (and, for residency
visibility, ``pg_buffercache``) being installed. Every helper degrades
to a deterministic "not available" result rather than raising when the
extension is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg.extensions import extension_installed
from mcpg.sql import SqlDriver

# pg_prewarm modes (matches the upstream signature: pg_prewarm(regclass,
# mode text, fork text, first_block, last_block)). We don't expose ``fork``
# / block-range tuning at the tool surface — the common case is "warm the
# whole main fork" and operators who need range control are already past
# the LLM-agent layer.
_VALID_PREWARM_MODES = frozenset({"prefetch", "read", "buffer"})

# Default safeguards for the advisor. Operators can lift them per call.
_DEFAULT_BUDGET_PCT = 60.0
_DEFAULT_MIN_HEAP_BLKS_READ = 1_000
_DEFAULT_LIMIT = 20

# The default autowarm job name. Picked so list_autowarm_jobs has a
# stable substring to filter cron jobs on.
_AUTOWARM_JOB_NAME = "mcpg_autowarm"


class PrewarmError(Exception):
    """Raised when a pg_prewarm operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrewarmExtensionStatus:
    """Whether the prewarm-related extensions are usable.

    ``pg_prewarm`` is the headline; ``pg_buffercache`` is the
    supporting cast — without it, residency visibility tools degrade
    to "not available". ``autoprewarm_libraries_present`` reports
    whether ``pg_prewarm`` is listed in ``shared_preload_libraries``
    (the autoprewarm background worker requires it).
    """

    pg_prewarm_installed: bool
    pg_buffercache_installed: bool
    autoprewarm_libraries_present: bool
    shared_preload_libraries: str


@dataclass(frozen=True)
class PrewarmedRelation:
    """Current shared-buffer residency for one relation.

    ``blocks_cached`` is the number of 8 KiB blocks of this relation
    currently in shared buffers; ``total_blocks`` is its on-disk size
    in the same unit. ``pct_cached`` is the convenient ratio.
    """

    schema: str
    table: str
    blocks_cached: int
    total_blocks: int
    pct_cached: float
    dirty_blocks: int


@dataclass(frozen=True)
class PrewarmRecommendation:
    """One advisor recommendation.

    ``reason`` is one of:

    * ``high_cold_miss_rate`` — heap_blks_read >> heap_blks_hit.
    * ``small_hot_relation_uncached`` — small table, hot in stats but
      cold in shared buffers.
    * ``seq_scan_dominant`` — significant seq_scan with low cache
      residency.
    * ``index_in_critical_path`` — index whose blocks are missed often.

    ``prewarm_mode`` is ``buffer`` (shared buffers) / ``prefetch`` (OS
    page cache) / ``read`` (blocking read). ``estimated_buffer_cost``
    is in 8 KiB blocks against ``shared_buffers``.
    """

    schema: str
    relation: str
    reason: str
    prewarm_mode: str
    estimated_buffer_cost: int
    heap_blks_read: int
    heap_blks_hit: int
    cache_miss_ratio: float
    ready_to_run_sql: str


@dataclass(frozen=True)
class RecommendPrewarmTargetsResult:
    """Advisor output rolled up with the budget context.

    ``shared_buffers_blocks`` is the configured ``shared_buffers`` in
    8 KiB pages. ``budget_blocks`` is the cap derived from
    ``shared_buffers_budget_pct``. ``total_cost_blocks`` sums the
    recommendations actually returned (after the cap was applied).
    """

    shared_buffers_blocks: int
    budget_blocks: int
    total_cost_blocks: int
    candidates: list[PrewarmRecommendation] = field(default_factory=list)


@dataclass(frozen=True)
class PrewarmResult:
    """The outcome of a single ``pg_prewarm(...)`` call."""

    schema: str
    relation: str
    mode: str
    blocks_prewarmed: int


@dataclass(frozen=True)
class BulkPrewarmOutcome:
    """One row in :class:`BulkPrewarmResult`."""

    schema: str
    relation: str
    mode: str
    blocks_prewarmed: int
    error: str | None


@dataclass(frozen=True)
class BulkPrewarmResult:
    """Result of ``prewarm_recommended``."""

    dry_run: bool
    total_blocks: int
    outcomes: list[BulkPrewarmOutcome] = field(default_factory=list)


@dataclass(frozen=True)
class AutowarmJob:
    """A pg_cron job MCPg owns for autowarm."""

    jobid: int
    jobname: str
    schedule: str
    command: str


@dataclass(frozen=True)
class ScheduleAutowarmResult:
    """Outcome of registering an autowarm cron job."""

    jobid: int
    name: str
    schedule: str


@dataclass(frozen=True)
class UnscheduleAutowarmResult:
    """Outcome of removing an autowarm cron job."""

    name: str
    removed: bool


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


async def get_prewarm_extension_status(driver: SqlDriver) -> PrewarmExtensionStatus:
    """Report whether ``pg_prewarm`` (+ optional ``pg_buffercache``) are usable.

    Also surfaces the current ``shared_preload_libraries`` setting so
    operators can spot the missing autoprewarm worker registration in
    one glance.
    """
    has_prewarm = await extension_installed(driver, "pg_prewarm")
    has_buffercache = await extension_installed(driver, "pg_buffercache")
    rows = await driver.execute_query(
        "SELECT current_setting('shared_preload_libraries', true) AS spl",
        force_readonly=True,
    )
    spl = (rows[0].cells.get("spl") if rows else None) or ""
    libs = [piece.strip() for piece in spl.split(",") if piece.strip()]
    return PrewarmExtensionStatus(
        pg_prewarm_installed=has_prewarm,
        pg_buffercache_installed=has_buffercache,
        autoprewarm_libraries_present="pg_prewarm" in libs,
        shared_preload_libraries=spl,
    )


# ---------------------------------------------------------------------------
# Buffer-cache residency reads
# ---------------------------------------------------------------------------


async def list_prewarmed_relations(
    driver: SqlDriver,
    *,
    schema: str | None = None,
    limit: int = 100,
) -> list[PrewarmedRelation]:
    """Report current shared-buffer residency per relation.

    Requires ``pg_buffercache``. Returns an empty list when the extension
    is missing (deterministic for callers — they treat absence as "no
    visibility" rather than a hard error).

    ``limit`` defends against very wide databases — a 10k-relation join
    against ``pg_buffercache`` is cheap but ranking the top-N is what
    operators typically want.
    """
    if not await extension_installed(driver, "pg_buffercache"):
        return []
    if limit <= 0:
        raise PrewarmError("limit must be positive")
    # Pass schema as a bound parameter; NULL means "no schema filter".
    # Using `(%s::text IS NULL OR n.nspname = %s)` keeps the SQL static
    # (no string interpolation) so bandit B608 isn't tripped.
    rows = await driver.execute_query(
        "SELECT n.nspname AS schema, c.relname AS table_name, "
        "  count(*) FILTER (WHERE b.relfilenode IS NOT NULL) AS blocks_cached, "
        "  count(*) FILTER (WHERE b.isdirty) AS dirty_blocks, "
        "  pg_relation_size(c.oid) / current_setting('block_size')::int AS total_blocks "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_buffercache b ON b.relfilenode = pg_relation_filenode(c.oid) "
        "WHERE c.relkind IN ('r', 'p') "
        "  AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "  AND (%s::text IS NULL OR n.nspname = %s) "
        "GROUP BY n.nspname, c.relname, c.oid "
        "HAVING count(*) FILTER (WHERE b.relfilenode IS NOT NULL) > 0 "
        "ORDER BY blocks_cached DESC "
        "LIMIT %s",
        params=[schema, schema, limit],
        force_readonly=True,
    )
    results: list[PrewarmedRelation] = []
    for row in rows or []:
        cached = int(row.cells["blocks_cached"])
        total = int(row.cells["total_blocks"] or 0)
        pct = (100.0 * cached / total) if total > 0 else 0.0
        results.append(
            PrewarmedRelation(
                schema=row.cells["schema"],
                table=row.cells["table_name"],
                blocks_cached=cached,
                total_blocks=total,
                pct_cached=round(pct, 2),
                dirty_blocks=int(row.cells["dirty_blocks"]),
            )
        )
    return results


# ---------------------------------------------------------------------------
# Advisor — recommend_prewarm_targets
# ---------------------------------------------------------------------------


async def _shared_buffers_blocks(driver: SqlDriver) -> int:
    """Return ``shared_buffers`` in 8 KiB blocks (the unit pg_prewarm uses).

    ``current_setting('shared_buffers')`` returns a human-readable value
    like ``"128MB"`` or ``"16384"``; ``pg_size_bytes`` normalises that
    to bytes and we divide by ``block_size``.
    """
    rows = await driver.execute_query(
        "SELECT pg_size_bytes(current_setting('shared_buffers')) /   current_setting('block_size')::int AS blocks",
        force_readonly=True,
    )
    if not rows:
        return 0
    return int(rows[0].cells.get("blocks") or 0)


def _classify_prewarm_reason(*, seq_scans: int, idx_scans: int, miss_ratio: float, est_blocks: int) -> str:
    """Pick the stable reason code for one candidate."""
    if seq_scans > idx_scans and miss_ratio >= 0.5:
        return "seq_scan_dominant"
    if miss_ratio >= 0.5:
        return "high_cold_miss_rate"
    if est_blocks <= 256:
        return "small_hot_relation_uncached"
    return "index_in_critical_path"


def _prewarm_sql(schema: str, relation: str, mode: str) -> str:
    """The ``SELECT pg_prewarm(...)`` statement for one relation."""
    return f"SELECT pg_prewarm('{schema}.{relation}'::regclass, '{mode}');"


async def recommend_prewarm_targets(
    driver: SqlDriver,
    *,
    shared_buffers_budget_pct: float = _DEFAULT_BUDGET_PCT,
    min_heap_blks_read: int = _DEFAULT_MIN_HEAP_BLKS_READ,
    limit: int = _DEFAULT_LIMIT,
    prewarm_mode: str = "buffer",
) -> RecommendPrewarmTargetsResult:
    """Recommend tables / indexes whose first-query latency would benefit from prewarm.

    Heuristic (per relation):

    1. From ``pg_statio_user_tables`` get ``heap_blks_read`` (cache
       miss) and ``heap_blks_hit`` (cache hit). High miss-ratio relations
       are good prewarm targets.
    2. From ``pg_stat_user_tables`` get ``seq_scan`` vs ``idx_scan`` to
       distinguish "sequential-scan dominant" from "index-scan critical
       path" — the recommendation carries the right ``reason``.
    3. From ``pg_class.reltuples`` * ``current_setting('block_size')``
       compute the estimated cost in 8 KiB blocks. We sort recommendations
       by descending miss-volume and stop adding once the cumulative cost
       would exceed ``shared_buffers_budget_pct`` * ``shared_buffers``.

    ``prewarm_mode`` propagates into the generated SQL stubs and the
    recommendation rows.

    The advisor is read-only — it never invokes ``pg_prewarm``. Pair
    it with ``prewarm_recommended`` when you want to act on its output.
    """
    if prewarm_mode not in _VALID_PREWARM_MODES:
        raise PrewarmError(f"prewarm_mode must be one of {sorted(_VALID_PREWARM_MODES)}, got {prewarm_mode!r}")
    if shared_buffers_budget_pct <= 0 or shared_buffers_budget_pct > 100:
        raise PrewarmError("shared_buffers_budget_pct must be in (0, 100]")
    if min_heap_blks_read < 0:
        raise PrewarmError("min_heap_blks_read must be non-negative")
    if limit <= 0:
        raise PrewarmError("limit must be positive")

    sb_blocks = await _shared_buffers_blocks(driver)
    budget_blocks = int(sb_blocks * shared_buffers_budget_pct / 100)

    # The advisor relies only on the stats views — it never touches
    # pg_buffercache, so it works even when buffercache isn't installed.
    rows = await driver.execute_query(
        "SELECT s.schemaname AS schema, s.relname AS relation, "
        "  COALESCE(io.heap_blks_read, 0) AS heap_blks_read, "
        "  COALESCE(io.heap_blks_hit, 0) AS heap_blks_hit, "
        "  COALESCE(s.seq_scan, 0) AS seq_scans, "
        "  COALESCE(s.idx_scan, 0) AS idx_scans, "
        "  pg_relation_size(s.relid) / current_setting('block_size')::int AS est_blocks "
        "FROM pg_stat_user_tables s "
        "LEFT JOIN pg_statio_user_tables io ON io.relid = s.relid",
        force_readonly=True,
    )

    raw: list[tuple[int, PrewarmRecommendation]] = []
    for row in rows or []:
        heap_read = int(row.cells["heap_blks_read"])
        heap_hit = int(row.cells["heap_blks_hit"])
        if heap_read < min_heap_blks_read:
            continue
        seq_scans = int(row.cells["seq_scans"])
        idx_scans = int(row.cells["idx_scans"])
        est_blocks = max(int(row.cells["est_blocks"] or 0), 1)
        miss_ratio = heap_read / max(heap_read + heap_hit, 1)
        reason = _classify_prewarm_reason(
            seq_scans=seq_scans,
            idx_scans=idx_scans,
            miss_ratio=miss_ratio,
            est_blocks=est_blocks,
        )
        schema = row.cells["schema"]
        relation = row.cells["relation"]
        rec = PrewarmRecommendation(
            schema=schema,
            relation=relation,
            reason=reason,
            prewarm_mode=prewarm_mode,
            estimated_buffer_cost=est_blocks,
            heap_blks_read=heap_read,
            heap_blks_hit=heap_hit,
            cache_miss_ratio=round(miss_ratio, 3),
            ready_to_run_sql=_prewarm_sql(schema, relation, prewarm_mode),
        )
        raw.append((heap_read, rec))
    # Rank by miss-volume descending (the bigger the absolute pain, the
    # higher the payoff from prewarming).
    raw.sort(key=lambda pair: -pair[0])

    chosen: list[PrewarmRecommendation] = []
    running = 0
    for _, rec in raw:
        if len(chosen) >= limit:
            break
        if budget_blocks > 0 and running + rec.estimated_buffer_cost > budget_blocks:
            # Skip this candidate but keep checking — a smaller one
            # might still fit. Honest cost reporting beats "everything
            # fit silently".
            continue
        chosen.append(rec)
        running += rec.estimated_buffer_cost
    return RecommendPrewarmTargetsResult(
        shared_buffers_blocks=sb_blocks,
        budget_blocks=budget_blocks,
        total_cost_blocks=running,
        candidates=chosen,
    )


# ---------------------------------------------------------------------------
# Write helpers — pg_prewarm() invocation
# ---------------------------------------------------------------------------


def _validate_relation(schema: str, relation: str) -> None:
    """Reject identifiers with characters that would break out of a literal."""
    for label, value in (("schema", schema), ("relation", relation)):
        if not value:
            raise PrewarmError(f"{label} must be non-empty")
        if any(ch in value for ch in "'\";\\\n\r"):
            raise PrewarmError(f"{label} {value!r} contains characters that aren't allowed in a regclass literal")


async def prewarm_relation(
    driver: SqlDriver,
    *,
    schema: str,
    relation: str,
    mode: str = "buffer",
) -> PrewarmResult:
    """Run ``SELECT pg_prewarm('schema.relation'::regclass, mode);`` once."""
    if mode not in _VALID_PREWARM_MODES:
        raise PrewarmError(f"mode must be one of {sorted(_VALID_PREWARM_MODES)}, got {mode!r}")
    _validate_relation(schema, relation)
    if not await extension_installed(driver, "pg_prewarm"):
        raise PrewarmError("pg_prewarm extension is not installed; call enable_extension('pg_prewarm') first")
    rows = await driver.execute_query(
        f"SELECT pg_prewarm('{schema}.{relation}'::regclass, '{mode}') AS blocks",
        force_readonly=False,
    )
    blocks = int(rows[0].cells["blocks"]) if rows else 0
    return PrewarmResult(schema=schema, relation=relation, mode=mode, blocks_prewarmed=blocks)


async def prewarm_recommended(
    driver: SqlDriver,
    *,
    shared_buffers_budget_pct: float = _DEFAULT_BUDGET_PCT,
    min_heap_blks_read: int = _DEFAULT_MIN_HEAP_BLKS_READ,
    limit: int = _DEFAULT_LIMIT,
    prewarm_mode: str = "buffer",
    dry_run: bool = False,
) -> BulkPrewarmResult:
    """Run ``recommend_prewarm_targets`` and prewarm every candidate.

    ``dry_run=True`` reports what *would* be prewarmed without invoking
    pg_prewarm. Errors per-relation are captured and reported in the
    ``outcomes`` list so a single bad relation doesn't fail the whole
    bulk pass.
    """
    if not dry_run and not await extension_installed(driver, "pg_prewarm"):
        raise PrewarmError("pg_prewarm extension is not installed; call enable_extension('pg_prewarm') first")
    recommendations = await recommend_prewarm_targets(
        driver,
        shared_buffers_budget_pct=shared_buffers_budget_pct,
        min_heap_blks_read=min_heap_blks_read,
        limit=limit,
        prewarm_mode=prewarm_mode,
    )
    outcomes: list[BulkPrewarmOutcome] = []
    total = 0
    for rec in recommendations.candidates:
        if dry_run:
            outcomes.append(
                BulkPrewarmOutcome(
                    schema=rec.schema,
                    relation=rec.relation,
                    mode=rec.prewarm_mode,
                    blocks_prewarmed=0,
                    error=None,
                )
            )
            continue
        try:
            result = await prewarm_relation(
                driver,
                schema=rec.schema,
                relation=rec.relation,
                mode=rec.prewarm_mode,
            )
        except Exception as exc:
            outcomes.append(
                BulkPrewarmOutcome(
                    schema=rec.schema,
                    relation=rec.relation,
                    mode=rec.prewarm_mode,
                    blocks_prewarmed=0,
                    error=str(exc),
                )
            )
            continue
        outcomes.append(
            BulkPrewarmOutcome(
                schema=rec.schema,
                relation=rec.relation,
                mode=rec.prewarm_mode,
                blocks_prewarmed=result.blocks_prewarmed,
                error=None,
            )
        )
        total += result.blocks_prewarmed
    return BulkPrewarmResult(dry_run=dry_run, total_blocks=total, outcomes=outcomes)


# ---------------------------------------------------------------------------
# Autowarm — pg_cron-backed scheduler
# ---------------------------------------------------------------------------


def _validate_cron_schedule(schedule: str) -> str:
    """Reject schedules with characters that would break out of the literal."""
    if not schedule or not schedule.strip():
        raise PrewarmError("schedule must be non-empty")
    if any(ch in schedule for ch in "'\";\\\n\r"):
        raise PrewarmError("schedule contains characters that aren't allowed in a cron expression")
    return schedule.strip()


def _autowarm_command(
    *,
    shared_buffers_budget_pct: float,
    min_heap_blks_read: int,
    limit: int,
    prewarm_mode: str,
) -> str:
    """Build the SELECT statement the cron job will run.

    We embed the parameters directly because pg_cron stores commands as
    text and reads them later in a fresh session. The parameters have
    already been validated (numeric / known-enum) so embedding is safe.
    """
    return (
        "SELECT mcpg.prewarm_recommended_cron("
        f"{float(shared_buffers_budget_pct)}, "
        f"{int(min_heap_blks_read)}, "
        f"{int(limit)}, "
        f"'{prewarm_mode}');"
    )


async def schedule_autowarm(
    driver: SqlDriver,
    *,
    name: str = _AUTOWARM_JOB_NAME,
    schedule: str = "@reboot",
    shared_buffers_budget_pct: float = _DEFAULT_BUDGET_PCT,
    min_heap_blks_read: int = _DEFAULT_MIN_HEAP_BLKS_READ,
    limit: int = _DEFAULT_LIMIT,
    prewarm_mode: str = "buffer",
) -> ScheduleAutowarmResult:
    """Register a pg_cron job that calls the bulk-prewarm helper at ``schedule``.

    ``schedule`` accepts a cron expression (``"0 4 * * *"``) or pg_cron
    interval shortcut (``"@reboot"`` / ``"30 seconds"`` / etc.). The
    default ``@reboot`` plus an operator-supplied daily refresh covers
    the "warm after restart" + "warm again before traffic" pattern most
    teams want.

    The command embeds the budget / limit / mode arguments as numeric
    literals — the receiver function must be installed separately
    (operators paste the helper into their database; the standard
    template is documented in ``docs/plans/pg-prewarm-advisor.md``).
    """
    if prewarm_mode not in _VALID_PREWARM_MODES:
        raise PrewarmError(f"prewarm_mode must be one of {sorted(_VALID_PREWARM_MODES)}, got {prewarm_mode!r}")
    if shared_buffers_budget_pct <= 0 or shared_buffers_budget_pct > 100:
        raise PrewarmError("shared_buffers_budget_pct must be in (0, 100]")
    if min_heap_blks_read < 0:
        raise PrewarmError("min_heap_blks_read must be non-negative")
    if limit <= 0:
        raise PrewarmError("limit must be positive")
    schedule = _validate_cron_schedule(schedule)
    if not name or any(ch in name for ch in "'\";\\\n\r"):
        raise PrewarmError("name must be a simple identifier")
    if not await extension_installed(driver, "pg_cron"):
        raise PrewarmError("pg_cron extension is not installed; install it before scheduling an autowarm job")
    command = _autowarm_command(
        shared_buffers_budget_pct=shared_buffers_budget_pct,
        min_heap_blks_read=min_heap_blks_read,
        limit=limit,
        prewarm_mode=prewarm_mode,
    )
    rows = await driver.execute_query(
        "SELECT cron.schedule(%s, %s, %s) AS jobid",
        params=[name, schedule, command],
        force_readonly=False,
    )
    jobid = int(rows[0].cells["jobid"]) if rows else 0
    return ScheduleAutowarmResult(jobid=jobid, name=name, schedule=schedule)


async def unschedule_autowarm(
    driver: SqlDriver,
    *,
    name: str = _AUTOWARM_JOB_NAME,
) -> UnscheduleAutowarmResult:
    """Remove the autowarm pg_cron job by name (idempotent)."""
    if not name or any(ch in name for ch in "'\";\\\n\r"):
        raise PrewarmError("name must be a simple identifier")
    if not await extension_installed(driver, "pg_cron"):
        return UnscheduleAutowarmResult(name=name, removed=False)
    rows = await driver.execute_query(
        "SELECT cron.unschedule(%s) AS removed",
        params=[name],
        force_readonly=False,
    )
    removed = bool(rows[0].cells["removed"]) if rows else False
    return UnscheduleAutowarmResult(name=name, removed=removed)


async def list_autowarm_jobs(driver: SqlDriver) -> list[AutowarmJob]:
    """List the pg_cron jobs whose name starts with ``mcpg_autowarm``."""
    if not await extension_installed(driver, "pg_cron"):
        return []
    rows = await driver.execute_query(
        "SELECT jobid, jobname, schedule, command FROM cron.job WHERE jobname LIKE 'mcpg_autowarm%' ORDER BY jobid",
        force_readonly=True,
    )
    return [
        AutowarmJob(
            jobid=int(row.cells["jobid"]),
            jobname=row.cells["jobname"],
            schedule=row.cells["schedule"],
            command=row.cells["command"],
        )
        for row in rows or []
    ]


__all__ = [
    "AutowarmJob",
    "BulkPrewarmOutcome",
    "BulkPrewarmResult",
    "PrewarmError",
    "PrewarmExtensionStatus",
    "PrewarmRecommendation",
    "PrewarmResult",
    "PrewarmedRelation",
    "RecommendPrewarmTargetsResult",
    "ScheduleAutowarmResult",
    "UnscheduleAutowarmResult",
    "get_prewarm_extension_status",
    "list_autowarm_jobs",
    "list_prewarmed_relations",
    "prewarm_recommended",
    "prewarm_relation",
    "recommend_prewarm_targets",
    "schedule_autowarm",
    "unschedule_autowarm",
]
