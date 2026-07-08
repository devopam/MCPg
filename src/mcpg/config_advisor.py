"""Configuration & sizing advisors — pghero / pgtune coverage.

Realises roadmap **§16**. Three operator-facing tools that fill the
gaps the existing ``audit_database`` doesn't cover:

* ``audit_sequences(warning_pct, critical_pct)`` — **16.1**. Walks
  ``pg_sequences`` for serial / identity / explicit sequences nearing
  their ceiling. Sequence overflow is catastrophic and silent until
  the next ``nextval()`` raises ``nextval: reached maximum value`` —
  the int4 ``serial`` ceiling (2³¹ - 1) is hit far more often than
  operators expect. Pure read.

* ``audit_settings(total_ram_mb)`` — **16.2**. Reads ``pg_settings``
  and applies a pghero-style rule set: deprecated / dangerous values
  (``fsync = off``, ``autovacuum = off``), cross-setting sanity
  (``maintenance_work_mem`` ≥ ``work_mem``; ``shared_buffers`` not
  left at the tiny default), and — when ``total_ram_mb`` is supplied —
  RAM-relative ratios for ``shared_buffers`` / ``effective_cache_size``.
  Pure read.

* ``recommend_postgres_conf(...)`` — **16.3**. A pure pgtune-style
  calculator (no DB connection): given RAM / CPU / workload / storage /
  max-connections it returns recommended values for the headline
  ``postgresql.conf`` knobs.

These are standalone tools rather than ``audit_database`` categories:
folding into that subsystem would mean matching its bespoke
``CategoryResult`` scoring machinery, a bigger and more fragile change.
The fold-in is a clean follow-up; the standalone tools deliver the
value now with their own ``available`` / ``detail`` envelope.

Security posture
================
* ``audit_sequences`` / ``audit_settings`` issue fixed catalogue reads
  with no caller-supplied identifiers; the only arguments are numeric
  thresholds, all range-validated.
* ``recommend_postgres_conf`` touches no database at all — it's a pure
  function of its arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg.sql import SqlDriver

# Status codes shared across the audit tools — same vocabulary as
# mcpg.audit's MetricResult.status so an operator sees consistent
# severity language across the whole diagnostic surface.
STATUS_GOOD = "GOOD"
STATUS_WARNING = "WARNING"
STATUS_CRITICAL = "CRITICAL"

_VALID_WORKLOADS = frozenset({"web", "oltp", "dw", "desktop", "mixed"})
_VALID_STORAGE = frozenset({"ssd", "hdd", "san"})


class ConfigAdvisorError(Exception):
    """Raised when a config-advisor argument fails validation."""


# ---------------------------------------------------------------------------
# 16.1 — audit_sequences
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SequenceUsage:
    """One sequence's consumption against its ceiling.

    ``used_pct`` is ``last_value / max_value * 100``. ``status`` is
    ``GOOD`` / ``WARNING`` / ``CRITICAL`` against the configured
    thresholds. ``remaining`` is the headroom (``max_value -
    last_value``) so an operator sees the absolute runway, not just the
    ratio.
    """

    schema: str
    sequence: str
    last_value: int
    max_value: int
    used_pct: float
    remaining: int
    status: str


@dataclass(frozen=True)
class SequenceAuditResult:
    """Roll-up of :func:`audit_sequences`.

    ``available`` is ``False`` only when the server predates
    ``pg_sequences`` (PG < 10) — every supported PG version has it.
    ``sequences`` lists ONLY the at-risk ones (status != GOOD), sorted
    by ``used_pct`` descending. ``total_examined`` is the full count
    walked so "0 at-risk of 412 examined" is distinguishable from
    "nothing to examine".
    """

    available: bool
    total_examined: int
    warning_pct: float
    critical_pct: float
    sequences: list[SequenceUsage] = field(default_factory=list)
    detail: str = ""


async def _has_pg_sequences(driver: SqlDriver) -> bool:
    rows = await driver.execute_query(
        "SELECT to_regclass('pg_catalog.pg_sequences') IS NOT NULL AS present",
        force_readonly=True,
    )
    return bool(rows and rows[0].cells.get("present"))


async def audit_sequences(
    driver: SqlDriver,
    *,
    warning_pct: float = 80.0,
    critical_pct: float = 95.0,
) -> SequenceAuditResult:
    """Flag sequences nearing their ceiling.

    Args:
        warning_pct: usage ratio (0-100) at/above which a sequence is
            flagged ``WARNING``. Default 80.
        critical_pct: usage ratio at/above which a sequence is flagged
            ``CRITICAL``. Default 95. Must be >= ``warning_pct``.

    Sequences with a NULL ``last_value`` (never advanced) are counted
    in ``total_examined`` but never flagged — a fresh sequence at 0
    isn't at risk.

    Direction-aware: ascending sequences exhaust toward ``max_value``;
    descending sequences (``increment_by < 0``) exhaust toward
    ``min_value``. ``used_pct`` and ``remaining`` are computed against
    the direction of travel, so a near-exhausted descending sequence is
    flagged and a freshly-started one isn't.
    """
    if not (0 < warning_pct <= 100):
        raise ConfigAdvisorError(f"warning_pct must be in (0, 100]; got {warning_pct}")
    if not (0 < critical_pct <= 100):
        raise ConfigAdvisorError(f"critical_pct must be in (0, 100]; got {critical_pct}")
    if critical_pct < warning_pct:
        raise ConfigAdvisorError(f"critical_pct ({critical_pct}) must be >= warning_pct ({warning_pct})")

    if not await _has_pg_sequences(driver):
        return SequenceAuditResult(
            available=False,
            total_examined=0,
            warning_pct=warning_pct,
            critical_pct=critical_pct,
            sequences=[],
            detail=(
                "pg_sequences is not present (PostgreSQL < 10). Sequence overflow auditing requires PG 10 or newer."
            ),
        )

    rows = await driver.execute_query(
        "SELECT schemaname, sequencename, last_value, min_value, max_value, increment_by "
        "FROM pg_sequences ORDER BY schemaname, sequencename",
        force_readonly=True,
    )

    at_risk: list[SequenceUsage] = []
    total = 0
    for row in rows or []:
        total += 1
        last_value = row.cells.get("last_value")
        max_value = row.cells.get("max_value")
        min_value = row.cells.get("min_value")
        increment_by = row.cells.get("increment_by")
        if last_value is None or max_value is None:
            # Never-advanced sequence — not at risk.
            continue
        last_i = int(last_value)
        max_i = int(max_value)
        # Defaults mirror a plain ascending serial when the catalogue
        # row didn't carry these (e.g. a hand-built test fixture).
        min_i = int(min_value) if min_value is not None else 1
        inc_i = int(increment_by) if increment_by is not None else 1
        seq_range = max_i - min_i
        if inc_i == 0 or seq_range <= 0:
            # Degenerate sequence (zero increment / inverted bounds) — skip.
            continue
        # Direction-aware: an ASCENDING sequence exhausts toward
        # max_value; a DESCENDING one (increment_by < 0) toward
        # min_value. Compute consumption + headroom against the
        # direction of travel so descending sequences aren't silently
        # missed (gemini review on #181).
        if inc_i < 0:
            remaining = last_i - min_i
            used_pct = ((max_i - last_i) / seq_range) * 100.0
        else:
            remaining = max_i - last_i
            used_pct = ((last_i - min_i) / seq_range) * 100.0
        if used_pct >= critical_pct:
            status = STATUS_CRITICAL
        elif used_pct >= warning_pct:
            status = STATUS_WARNING
        else:
            continue  # GOOD sequences aren't listed — keeps the payload small.
        at_risk.append(
            SequenceUsage(
                schema=str(row.cells["schemaname"]),
                sequence=str(row.cells["sequencename"]),
                last_value=last_i,
                max_value=max_i,
                used_pct=round(used_pct, 4),
                remaining=remaining,
                status=status,
            )
        )

    at_risk.sort(key=lambda s: s.used_pct, reverse=True)
    crit = sum(1 for s in at_risk if s.status == STATUS_CRITICAL)
    warn = sum(1 for s in at_risk if s.status == STATUS_WARNING)
    if crit:
        detail = (
            f"{crit} sequence(s) at/above {critical_pct}% of their ceiling — "
            "ALTER SEQUENCE to a larger type (e.g. int → bigint) or reset "
            "before nextval() raises."
        )
    elif warn:
        detail = f"{warn} sequence(s) at/above {warning_pct}% of their ceiling — plan a type widening."
    else:
        detail = f"All {total} sequence(s) are within healthy bounds."

    return SequenceAuditResult(
        available=True,
        total_examined=total,
        warning_pct=warning_pct,
        critical_pct=critical_pct,
        sequences=at_risk,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 16.2 — audit_settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingFinding:
    """One postgresql.conf sanity-check finding.

    ``code`` is a stable identifier (e.g. ``fsync_off``); ``setting``
    is the GUC name; ``current`` is its current value (as text);
    ``status`` is ``WARNING`` / ``CRITICAL``; ``suggestion`` is the
    one-line remedy.
    """

    code: str
    setting: str
    current: str
    status: str
    suggestion: str


@dataclass(frozen=True)
class SettingsAuditResult:
    """Roll-up of :func:`audit_settings`.

    ``ram_aware`` is ``True`` when ``total_ram_mb`` was supplied and the
    RAM-relative ratio checks ran. ``findings`` lists only the rules
    that tripped (empty == clean). ``examined_settings`` is the GUC
    name list the sweep read, so the audit is auditable.
    """

    ram_aware: bool
    findings: list[SettingFinding] = field(default_factory=list)
    examined_settings: list[str] = field(default_factory=list)
    detail: str = ""


# The settings the sweep reads. Memory GUCs are pulled in bytes via
# pg_size_bytes(current_setting(...)) so we never have to decode the
# pg_settings unit column by hand.
_SETTINGS_QUERY = (
    "SELECT "
    "  current_setting('fsync') AS fsync, "
    "  current_setting('full_page_writes') AS full_page_writes, "
    "  current_setting('autovacuum') AS autovacuum, "
    "  current_setting('synchronous_commit') AS synchronous_commit, "
    "  pg_size_bytes(current_setting('shared_buffers')) AS shared_buffers, "
    "  pg_size_bytes(current_setting('effective_cache_size')) AS effective_cache_size, "
    "  pg_size_bytes(current_setting('work_mem')) AS work_mem, "
    "  pg_size_bytes(current_setting('maintenance_work_mem')) AS maintenance_work_mem, "
    "  current_setting('max_connections')::int AS max_connections, "
    "  current_setting('checkpoint_completion_target')::float8 AS checkpoint_completion_target"
)

_EXAMINED_SETTINGS = [
    "fsync",
    "full_page_writes",
    "autovacuum",
    "synchronous_commit",
    "shared_buffers",
    "effective_cache_size",
    "work_mem",
    "maintenance_work_mem",
    "max_connections",
    "checkpoint_completion_target",
]

_MB = 1024 * 1024


async def audit_settings(
    driver: SqlDriver,
    *,
    total_ram_mb: int | None = None,
) -> SettingsAuditResult:
    """Sanity-sweep ``postgresql.conf`` via ``pg_settings``.

    Args:
        total_ram_mb: Host RAM in MiB. PostgreSQL can't see the host's
            RAM from inside the server, so the RAM-relative ratio checks
            (``shared_buffers`` ≈ 25 % of RAM, ``effective_cache_size``
            ≈ 50-75 %) only run when the operator supplies this. When
            ``None`` the absolute / cross-setting checks still run.

    Returns a :class:`SettingsAuditResult` whose ``findings`` lists only
    the rules that tripped.
    """
    if total_ram_mb is not None and total_ram_mb <= 0:
        raise ConfigAdvisorError(f"total_ram_mb must be positive when set; got {total_ram_mb}")

    rows = await driver.execute_query(_SETTINGS_QUERY, force_readonly=True)
    if not rows:
        return SettingsAuditResult(
            ram_aware=total_ram_mb is not None,
            findings=[],
            examined_settings=list(_EXAMINED_SETTINGS),
            detail="Could not read pg_settings.",
        )
    c = rows[0].cells

    findings: list[SettingFinding] = []

    # --- deprecated / dangerous toggles ------------------------------------
    if str(c.get("fsync")) == "off":
        findings.append(
            SettingFinding(
                code="fsync_off",
                setting="fsync",
                current="off",
                status=STATUS_CRITICAL,
                suggestion=(
                    "fsync=off risks unrecoverable data corruption on crash. "
                    "Only acceptable for throwaway/bulk-load clusters; turn it "
                    "back on for anything you care about."
                ),
            )
        )
    if str(c.get("full_page_writes")) == "off":
        findings.append(
            SettingFinding(
                code="full_page_writes_off",
                setting="full_page_writes",
                current="off",
                status=STATUS_CRITICAL,
                suggestion=(
                    "full_page_writes=off risks torn-page corruption on crash "
                    "unless the storage guarantees atomic 8kB writes. Turn on "
                    "unless you are certain."
                ),
            )
        )
    if str(c.get("autovacuum")) == "off":
        findings.append(
            SettingFinding(
                code="autovacuum_off",
                setting="autovacuum",
                current="off",
                status=STATUS_CRITICAL,
                suggestion=(
                    "autovacuum=off leads to unbounded bloat and eventual "
                    "transaction-ID wraparound. Re-enable it; tune per-table "
                    "thresholds instead of disabling globally."
                ),
            )
        )
    if str(c.get("synchronous_commit")) == "off":
        findings.append(
            SettingFinding(
                code="synchronous_commit_off",
                setting="synchronous_commit",
                current="off",
                status=STATUS_WARNING,
                suggestion=(
                    "synchronous_commit=off trades durability for throughput — "
                    "committed transactions can be lost on crash. Intentional on "
                    "some replicas; confirm it's deliberate here."
                ),
            )
        )

    # --- cross-setting sanity ----------------------------------------------
    shared_buffers = int(c.get("shared_buffers") or 0)
    work_mem = int(c.get("work_mem") or 0)
    maintenance_work_mem = int(c.get("maintenance_work_mem") or 0)
    checkpoint_completion_target = float(c.get("checkpoint_completion_target") or 0.0)

    if 0 < shared_buffers < 128 * _MB:
        findings.append(
            SettingFinding(
                code="shared_buffers_tiny",
                setting="shared_buffers",
                current=f"{shared_buffers // _MB}MB",
                status=STATUS_WARNING,
                suggestion=(
                    "shared_buffers below 128MB usually means the cluster is "
                    "running near defaults. A common starting point is ~25% of "
                    "host RAM — run recommend_postgres_conf for a sizing."
                ),
            )
        )
    if maintenance_work_mem and work_mem and maintenance_work_mem < work_mem:
        findings.append(
            SettingFinding(
                code="maintenance_work_mem_below_work_mem",
                setting="maintenance_work_mem",
                current=f"{maintenance_work_mem // _MB}MB",
                status=STATUS_WARNING,
                suggestion=(
                    "maintenance_work_mem is smaller than work_mem — VACUUM / "
                    "CREATE INDEX get less memory than a single query sort. "
                    "Raise it (commonly several times work_mem)."
                ),
            )
        )
    if 0 < checkpoint_completion_target < 0.9:
        findings.append(
            SettingFinding(
                code="checkpoint_completion_target_low",
                setting="checkpoint_completion_target",
                current=str(checkpoint_completion_target),
                status=STATUS_WARNING,
                suggestion=(
                    "checkpoint_completion_target below 0.9 concentrates "
                    "checkpoint I/O into a short burst. 0.9 (the PG 14+ default) "
                    "spreads it out and smooths write latency."
                ),
            )
        )

    # --- RAM-relative ratios (only when RAM supplied) ----------------------
    if total_ram_mb is not None:
        ram_bytes = total_ram_mb * _MB
        effective_cache_size = int(c.get("effective_cache_size") or 0)
        sb_pct = (shared_buffers / ram_bytes) * 100.0 if ram_bytes else 0.0
        ecs_pct = (effective_cache_size / ram_bytes) * 100.0 if ram_bytes else 0.0
        if sb_pct < 10.0 or sb_pct > 45.0:
            findings.append(
                SettingFinding(
                    code="shared_buffers_ratio_off",
                    setting="shared_buffers",
                    current=f"{shared_buffers // _MB}MB ({sb_pct:.1f}% of RAM)",
                    status=STATUS_WARNING,
                    suggestion=(
                        "shared_buffers is usually ~25% of host RAM (healthy "
                        "band 10-45%). Run recommend_postgres_conf for a target."
                    ),
                )
            )
        if ecs_pct < 40.0:
            findings.append(
                SettingFinding(
                    code="effective_cache_size_low",
                    setting="effective_cache_size",
                    current=f"{effective_cache_size // _MB}MB ({ecs_pct:.1f}% of RAM)",
                    status=STATUS_WARNING,
                    suggestion=(
                        "effective_cache_size is a planner hint for total cache "
                        "(shared_buffers + OS page cache); ~50-75% of RAM is "
                        "typical. A low value makes the planner under-favour "
                        "index scans."
                    ),
                )
            )

    crit = sum(1 for f in findings if f.status == STATUS_CRITICAL)
    if crit:
        detail = f"{crit} critical configuration issue(s) found; review immediately."
    elif findings:
        detail = f"{len(findings)} configuration warning(s) found."
    else:
        detail = "No configuration issues detected by the current rule set."

    return SettingsAuditResult(
        ram_aware=total_ram_mb is not None,
        findings=findings,
        examined_settings=list(_EXAMINED_SETTINGS),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 16.3 — recommend_postgres_conf (pure pgtune-style calculator)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfRecommendation:
    """Recommended ``postgresql.conf`` values from :func:`recommend_postgres_conf`.

    Every memory field is a postgres-ready string (e.g. ``"2GB"``,
    ``"64MB"``). ``settings`` is the same data as a flat
    ``{guc: value}`` dict so a caller can render a conf snippet
    directly. The structured fields exist so an agent can reason about
    individual knobs without string-parsing.
    """

    workload: str
    storage: str
    total_ram_mb: int
    cpu_count: int
    max_connections: int
    shared_buffers: str
    effective_cache_size: str
    maintenance_work_mem: str
    work_mem: str
    wal_buffers: str
    min_wal_size: str
    max_wal_size: str
    checkpoint_completion_target: float
    default_statistics_target: int
    random_page_cost: float
    effective_io_concurrency: int
    max_worker_processes: int
    max_parallel_workers_per_gather: int
    max_parallel_workers: int
    max_parallel_maintenance_workers: int
    settings: dict[str, str] = field(default_factory=dict)


# Per-workload default connection counts (pgtune convention).
_DEFAULT_MAX_CONNECTIONS = {"web": 200, "oltp": 300, "dw": 40, "desktop": 10, "mixed": 100}
# Per-workload WAL sizing (MiB).
_WAL_SIZE_MB = {
    "web": (1024, 4096),
    "oltp": (2048, 8192),
    "dw": (4096, 16384),
    "desktop": (100, 2048),
    "mixed": (1024, 4096),
}
# Per-workload default_statistics_target.
_STATS_TARGET = {"web": 100, "oltp": 100, "dw": 500, "desktop": 100, "mixed": 100}
# Per-storage random_page_cost + effective_io_concurrency.
_RANDOM_PAGE_COST = {"ssd": 1.1, "san": 1.1, "hdd": 4.0}
_EFFECTIVE_IO_CONCURRENCY = {"ssd": 200, "san": 300, "hdd": 2}


def _format_kb(value_kb: int) -> str:
    """Format a KiB count as a postgres memory literal.

    Aligns down to a clean MB (or GB when divisible) so the output is a
    tidy ``"2GB"`` / ``"512MB"`` rather than ``"2097152kB"``. Values
    under 1 MB stay in kB.
    """
    if value_kb >= 1024:
        mb = value_kb // 1024  # floor to whole MB
        if mb % 1024 == 0:
            return f"{mb // 1024}GB"
        return f"{mb}MB"
    return f"{max(value_kb, 1)}kB"


def recommend_postgres_conf(
    *,
    total_ram_mb: int,
    cpu_count: int = 4,
    workload: str = "mixed",
    storage: str = "ssd",
    max_connections: int | None = None,
) -> ConfRecommendation:
    """Compute pgtune-style ``postgresql.conf`` recommendations.

    Pure function — no DB connection. The constants follow the
    well-known pgtune (le0pard) heuristics; treat the output as a
    starting point to review, not gospel.

    Args:
        total_ram_mb: Host RAM in MiB. Required.
        cpu_count: Logical CPUs. Parallel-worker knobs only populate
            above the PG defaults when ``cpu_count >= 4``.
        workload: One of ``web`` / ``oltp`` / ``dw`` / ``desktop`` /
            ``mixed``.
        storage: One of ``ssd`` / ``hdd`` / ``san``.
        max_connections: Override the per-workload default.
    """
    if total_ram_mb < 256:
        raise ConfigAdvisorError(f"total_ram_mb must be >= 256; got {total_ram_mb}")
    if cpu_count < 1:
        raise ConfigAdvisorError(f"cpu_count must be >= 1; got {cpu_count}")
    if workload not in _VALID_WORKLOADS:
        raise ConfigAdvisorError(f"workload must be one of {sorted(_VALID_WORKLOADS)}; got {workload!r}")
    if storage not in _VALID_STORAGE:
        raise ConfigAdvisorError(f"storage must be one of {sorted(_VALID_STORAGE)}; got {storage!r}")

    conns = max_connections if max_connections is not None else _DEFAULT_MAX_CONNECTIONS[workload]
    if conns < 1:
        raise ConfigAdvisorError(f"max_connections must be >= 1; got {conns}")

    ram_kb = total_ram_mb * 1024

    # shared_buffers: RAM/4 (desktop RAM/16).
    shared_buffers_kb = ram_kb // 16 if workload == "desktop" else ram_kb // 4
    # effective_cache_size: RAM*3/4 (desktop RAM/4).
    effective_cache_kb = ram_kb // 4 if workload == "desktop" else (ram_kb * 3) // 4
    # maintenance_work_mem: RAM/16, capped at 2GB.
    maintenance_kb = min(ram_kb // 16, 2 * 1024 * 1024)

    # Parallelism: only lift above defaults when CPUs warrant it.
    if cpu_count >= 4:
        max_worker_processes = cpu_count
        max_parallel_workers = cpu_count
        max_parallel_per_gather = max(1, cpu_count // 2)
        max_parallel_maintenance = max(1, min(cpu_count // 2, 4))
    else:
        max_worker_processes = 8  # PG default
        max_parallel_workers = 8
        max_parallel_per_gather = 2
        max_parallel_maintenance = 2

    # work_mem: (RAM - shared_buffers) / (conns * 3) / parallel-divisor,
    # then a per-workload divisor. Floor at 64kB.
    parallel_divisor = max(1, max_parallel_per_gather)
    work_mem_base = (ram_kb - shared_buffers_kb) // (conns * 3) // parallel_divisor
    work_mem_workload_divisor = {"web": 1, "oltp": 1, "dw": 2, "desktop": 6, "mixed": 2}[workload]
    work_mem_kb = max(64, work_mem_base // work_mem_workload_divisor)

    # wal_buffers: 3% of shared_buffers, capped at 16MB; floor 32kB.
    wal_buffers_kb = min(shared_buffers_kb * 3 // 100, 16 * 1024)
    wal_buffers_kb = max(wal_buffers_kb, 32)

    min_wal_mb, max_wal_mb = _WAL_SIZE_MB[workload]

    settings = {
        "shared_buffers": _format_kb(shared_buffers_kb),
        "effective_cache_size": _format_kb(effective_cache_kb),
        "maintenance_work_mem": _format_kb(maintenance_kb),
        "work_mem": _format_kb(work_mem_kb),
        "wal_buffers": _format_kb(wal_buffers_kb),
        "min_wal_size": f"{min_wal_mb}MB",
        "max_wal_size": f"{max_wal_mb}MB",
        "checkpoint_completion_target": "0.9",
        "default_statistics_target": str(_STATS_TARGET[workload]),
        "random_page_cost": str(_RANDOM_PAGE_COST[storage]),
        "effective_io_concurrency": str(_EFFECTIVE_IO_CONCURRENCY[storage]),
        "max_connections": str(conns),
        "max_worker_processes": str(max_worker_processes),
        "max_parallel_workers_per_gather": str(max_parallel_per_gather),
        "max_parallel_workers": str(max_parallel_workers),
        "max_parallel_maintenance_workers": str(max_parallel_maintenance),
    }

    return ConfRecommendation(
        workload=workload,
        storage=storage,
        total_ram_mb=total_ram_mb,
        cpu_count=cpu_count,
        max_connections=conns,
        shared_buffers=settings["shared_buffers"],
        effective_cache_size=settings["effective_cache_size"],
        maintenance_work_mem=settings["maintenance_work_mem"],
        work_mem=settings["work_mem"],
        wal_buffers=settings["wal_buffers"],
        min_wal_size=settings["min_wal_size"],
        max_wal_size=settings["max_wal_size"],
        checkpoint_completion_target=0.9,
        default_statistics_target=_STATS_TARGET[workload],
        random_page_cost=_RANDOM_PAGE_COST[storage],
        effective_io_concurrency=_EFFECTIVE_IO_CONCURRENCY[storage],
        max_worker_processes=max_worker_processes,
        max_parallel_workers_per_gather=max_parallel_per_gather,
        max_parallel_workers=max_parallel_workers,
        max_parallel_maintenance_workers=max_parallel_maintenance,
        settings=settings,
    )


__all__ = [
    "STATUS_CRITICAL",
    "STATUS_GOOD",
    "STATUS_WARNING",
    "ConfRecommendation",
    "ConfigAdvisorError",
    "SequenceAuditResult",
    "SequenceUsage",
    "SettingFinding",
    "SettingsAuditResult",
    "audit_sequences",
    "audit_settings",
    "recommend_postgres_conf",
]
