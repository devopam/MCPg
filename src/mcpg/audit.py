"""Audit logging of tool invocations and DBA database performance checks.

Every tool call is recorded to the ``mcpg.audit`` logger with the tool name,
its arguments (with secrets masked), and the outcome.

Also implements deep DBA-level checks across memory, connections, locks, table cleanliness,
query workloads, and server logs, producing a comprehensive diagnostic JSON report.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcpg._vendor.sql import SqlDriver, obfuscate_password

# --- Tool invocation logger (Original Audit Logic) ------------------------

audit_logger = logging.getLogger("mcpg.audit")

_log_format: str = "text"


def configure_log_format(format_name: str) -> None:
    """Configure the active log format for tool invocation logs.

    Idempotent.
    """
    global _log_format
    if format_name in {"text", "json"}:
        _log_format = format_name


# Names whose VALUES are credentials and must be masked before the
# arguments dict is logged or persisted. Each entry is a regex matched
# case-insensitively via ``re.search`` against the argument key — a
# substring match by default, so ``password`` also catches
# ``PGPASSWORD``, ``user_password`` and ``app.password``. Operators
# extend the list via ``MCPG_AUDIT_REDACT_KEYS`` (comma-separated
# regex fragments).
_DEFAULT_SECRET_NAME_PATTERNS: tuple[str, ...] = (
    r"password",
    r"passwd",
    r"secret",
    r"token",
    r"api[_-]?key",
    r"bearer",
    r"authorization",
    r"database_url",
    r"dsn",
    r"conninfo",
)
_MASK = "****"


def _compile_secret_name_pattern(extra: str | None) -> re.Pattern[str]:
    patterns = list(_DEFAULT_SECRET_NAME_PATTERNS)
    if extra:
        for raw in extra.split(","):
            stripped = raw.strip()
            if stripped:
                patterns.append(stripped)
    return re.compile("|".join(patterns), re.IGNORECASE)


# Pattern is recomputable so ``configure_redaction`` can swap in an
# extended list at server start without restarting the process.
_secret_name_re: re.Pattern[str] = _compile_secret_name_pattern(os.environ.get("MCPG_AUDIT_REDACT_KEYS"))


def configure_redaction(env: Mapping[str, str] | None = None) -> None:
    """Reload the secret-name pattern from ``MCPG_AUDIT_REDACT_KEYS``.

    Idempotent. ``load_settings`` calls this once with the validated env
    mapping; tests call it directly to flip the pattern without
    mutating the process environment.
    """
    global _secret_name_re
    source = os.environ if env is None else env
    _secret_name_re = _compile_secret_name_pattern(source.get("MCPG_AUDIT_REDACT_KEYS"))


def is_secret_key(name: str) -> bool:
    """Return True when ``name`` matches the configured secret-name pattern.

    Public so other audit-adjacent modules (notably
    :mod:`mcpg.audit_trail`) can share the same key-match decision
    instead of reaching into a private helper.
    """
    return bool(_secret_name_re.search(name))


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A record of a single tool invocation."""

    tool: str
    arguments: dict[str, Any]
    status: str
    error: str | None = None


def _redact_value(value: Any) -> Any:
    """Recursively mask credentials in dicts, lists, tuples, and strings.

    Mapping keys whose name matches the configured secret-name pattern
    have their value masked wholesale. String leaves pass through
    ``obfuscate_password`` so an embedded DSN credential nested
    arbitrarily deep is still scrubbed. Non-string scalars
    (``int`` / ``bool`` / ``None``) pass through unchanged.
    """
    if isinstance(value, dict):
        return {k: _MASK if isinstance(k, str) and is_secret_key(k) else _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, str):
        return obfuscate_password(value)
    return value


def redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of tool arguments with sensitive values masked.

    Arguments named like a credential (per the configured pattern, see
    :func:`configure_redaction`) are masked entirely; string leaves have
    any embedded connection-string password obfuscated; nested dicts /
    lists / tuples are walked recursively.
    """
    return _redact_value(arguments)  # type: ignore[no-any-return]


def record(event: AuditEvent) -> None:
    """Emit an audit event to the ``mcpg.audit`` logger."""
    safe_arguments = redact_arguments(event.arguments)
    if _log_format == "json":
        payload = {
            "tool": event.tool,
            "status": event.status,
            "arguments": safe_arguments,
        }
        if event.error is not None:
            payload["error"] = event.error
        msg = json.dumps(payload)
        if event.error is None:
            audit_logger.info(msg)
        else:
            audit_logger.warning(msg)
    else:
        if event.error is None:
            audit_logger.info("tool=%s status=%s arguments=%s", event.tool, event.status, safe_arguments)
        else:
            audit_logger.warning(
                "tool=%s status=%s arguments=%s error=%s",
                event.tool,
                event.status,
                safe_arguments,
                event.error,
            )


# --- DBA Database Performance Auditor (New Tier-A Feature) ----------------


@dataclass(frozen=True, slots=True)
class MetricResult:
    """Detailed facts about a single audit check."""

    name: str
    value: float | int | str
    unit: str
    target: str
    status: str  # GOOD, WARNING, CRITICAL
    severity: int  # 0: GOOD, 2: WARNING, 3: CRITICAL
    evidence: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class CategoryResult:
    """Combined status and score for a group of related checks."""

    category: str
    status: str  # GOOD, WARNING, CRITICAL
    score: int
    metrics: list[MetricResult]


@dataclass(frozen=True, slots=True)
class TopIssue:
    """A critical or high-priority issue requiring DBA triage."""

    issue: str
    severity: str  # CRITICAL, HIGH, MEDIUM
    affected_component: str
    root_cause: str
    suggested_action: str


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A prescriptive next-step to resolve issues or optimize performance."""

    priority: str  # CRITICAL, HIGH, MEDIUM
    action: str
    objects: list[str]
    estimated_impact: str


@dataclass(frozen=True, slots=True)
class AuditReport:
    """The master diagnostic payload returned to the DBA."""

    timestamp: str
    database: str
    version: str
    overall_health: str  # GOOD, WARNING, CRITICAL
    health_score: int
    categories: list[CategoryResult]
    top_issues: list[TopIssue]
    recommendations: list[Recommendation]
    raw_stats_snapshot: dict[str, Any] = field(default_factory=dict)


async def _get_version_and_db(driver: SqlDriver) -> tuple[str, str]:
    """Retrieve the PostgreSQL engine version and active database name."""
    try:
        rows = await driver.execute_query(
            "SELECT version(), current_database() AS dbname",
            force_readonly=True,
        )
        if rows:
            ver = str(rows[0].cells["version"])
            short_ver = ver.split(",")[0] if "," in ver else ver
            return short_ver, str(rows[0].cells["dbname"])
    except Exception:
        pass
    return "PostgreSQL Unknown", "unknown"


async def audit_memory_io(driver: SqlDriver, health_score: dict[str, int]) -> CategoryResult:
    """Analyze memory and I/O efficiency: cache hit ratio, checkpoints, WAL and temp files."""
    metrics: list[MetricResult] = []
    category_score = 100

    # 1. Buffer Cache Hit Ratio
    try:
        rows = await driver.execute_query(
            "SELECT sum(blks_hit) AS hits, sum(blks_read) AS reads FROM pg_stat_database",
            force_readonly=True,
        )
        hits = int((rows or [])[0].cells["hits"] or 0)
        reads = int((rows or [])[0].cells["reads"] or 0)
        total = hits + reads
        ratio = (hits / total) * 100 if total else 100.0

        if ratio < 99.0:
            status = "WARNING"
            severity = 2
            category_score -= 10
            evidence = f"Hit ratio averaged {ratio:.2f}% since last reset."
            suggestion = "Increase shared_buffers to ensure the working set fits in memory."
        else:
            status = "GOOD"
            severity = 0
            evidence = f"Excellent hit ratio of {ratio:.2f}%."
            suggestion = "Keep monitoring. shared_buffers are appropriately sized."

        metrics.append(
            MetricResult(
                name="Buffer Cache Hit Ratio",
                value=round(ratio, 2),
                unit="%",
                target="> 99",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Buffer Cache Hit Ratio",
                value="N/A",
                unit="",
                target="> 99",
                status="WARNING",
                severity=2,
                evidence=f"Failed to query pg_stat_database: {exc}",
                suggestion="Verify permissions on pg_stat_database.",
            )
        )

    # 2. Checkpoint Completion & Bgwriter Efficiency
    try:
        # Check if pg_stat_checkpointer view exists (introduced in PG 17+)
        cp_exists = await driver.execute_query(
            "SELECT 1 FROM pg_views WHERE schemaname = 'pg_catalog' AND viewname = 'pg_stat_checkpointer'",
            force_readonly=True,
        )
        if cp_exists:
            # Query for PG 17+ combining pg_stat_checkpointer and pg_stat_bgwriter
            rows = await driver.execute_query(
                "SELECT num_timed AS checkpoints_timed, num_requested AS checkpoints_req, "
                "buffers_written AS buffers_checkpoint, buffers_clean, maxwritten_clean, "
                "0 AS buffers_backend, 0 AS buffers_backend_fsync "
                "FROM pg_stat_checkpointer, pg_stat_bgwriter",
                force_readonly=True,
            )
        else:
            # Query for PG 16 and older
            rows = await driver.execute_query(
                "SELECT checkpoints_timed, checkpoints_req, "
                "buffers_checkpoint, buffers_clean, maxwritten_clean, "
                "buffers_backend, buffers_backend_fsync "
                "FROM pg_stat_bgwriter",
                force_readonly=True,
            )
        cells = (rows or [])[0].cells
        cp_timed = int(cells["checkpoints_timed"] or 0)
        cp_req = int(cells["checkpoints_req"] or 0)
        buf_clean = int(cells["buffers_clean"] or 0)
        buf_backend = int(cells["buffers_backend"] or 0)
        buf_fsync = int(cells["buffers_backend_fsync"] or 0)

        cp_total = cp_timed + cp_req
        req_ratio = (cp_req / cp_total) * 100 if cp_total else 0.0
        bg_ratio = (buf_clean / (buf_clean + buf_backend)) * 100 if (buf_clean + buf_backend) else 100.0

        if req_ratio > 10.0 or buf_backend > buf_clean or buf_fsync > 0:
            status = "WARNING"
            severity = 2
            category_score -= 10
            evidence = (
                f"Requested checkpoints: {req_ratio:.1f}%. "
                f"Backend writes: {buf_backend} vs Bgwriter: {buf_clean}. "
                f"Backend syncs: {buf_fsync}."
            )
            suggestion = (
                "Increase max_wal_size / checkpoint_segments to reduce requested checkpoints. "
                "Tune bgwriter_delay and bgwriter_lru_maxpages."
            )
        else:
            status = "GOOD"
            severity = 0
            evidence = "Bgwriter is keeping up. Checkpoint frequency is optimal."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Bgwriter Efficiency",
                value=round(bg_ratio, 2),
                unit="%",
                target="> 70",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Bgwriter Efficiency",
                value="N/A",
                unit="%",
                target="> 70",
                status="WARNING",
                severity=2,
                evidence=f"Failed to query pg_stat_bgwriter: {exc}",
                suggestion="Verify permissions.",
            )
        )

    # 3. Temporary File Spill Usage
    try:
        rows = await driver.execute_query(
            "SELECT sum(temp_files) AS temp_files, sum(temp_bytes) AS temp_bytes FROM pg_stat_database",
            force_readonly=True,
        )
        cells = (rows or [])[0].cells
        t_files = int(cells["temp_files"] or 0)
        t_bytes = int(cells["temp_bytes"] or 0)

        if t_bytes > 500 * 1024 * 1024:  # > 500 MB
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"{t_files} temp files generated, consuming {t_bytes} bytes."
            suggestion = "Increase work_mem to reduce disk spills during sorting and hash aggregations."
        elif t_bytes > 0:
            status = "WARNING"
            severity = 2
            category_score -= 5
            evidence = f"Minor disk spill: {t_files} files, {t_bytes} bytes."
            suggestion = "Consider raising work_mem or indexing sorting columns."
        else:
            status = "GOOD"
            severity = 0
            evidence = "Zero temporary files created. Queries fitting in memory."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Temporary File Usage",
                value=t_bytes,
                unit="bytes",
                target="< 500 MB/hour",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Temporary File Usage",
                value="N/A",
                unit="bytes",
                target="< 500 MB/hour",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Verify permissions on pg_stat_database.",
            )
        )

    cat_score = max(0, category_score)
    status_label = "GOOD" if cat_score >= 90 else ("WARNING" if cat_score >= 70 else "CRITICAL")
    return CategoryResult(
        category="Memory & I/O Efficiency",
        status=status_label,
        score=cat_score,
        metrics=metrics,
    )


async def audit_transactions_connections(driver: SqlDriver) -> CategoryResult:
    """Analyze transaction health: rollback rate, wraparound, prepared transactions, and connection saturation."""
    metrics: list[MetricResult] = []
    category_score = 100

    # 1. Transaction Rollback Rate
    try:
        rows = await driver.execute_query(
            "SELECT sum(xact_commit) AS commits, sum(xact_rollback) AS rollbacks FROM pg_stat_database",
            force_readonly=True,
        )
        commits = int((rows or [])[0].cells["commits"] or 0)
        rollbacks = int((rows or [])[0].cells["rollbacks"] or 0)
        total = commits + rollbacks
        ratio = (rollbacks / total) * 100 if total else 0.0

        if ratio > 0.1:
            status = "WARNING"
            severity = 2
            category_score -= 10
            evidence = f"Rollback rate is {ratio:.3f}% ({rollbacks} rollbacks)."
            suggestion = "Investigate application logs for query exceptions, deadlock termination, or rollbacks."
        else:
            status = "GOOD"
            severity = 0
            evidence = f"Pristine rollback rate: {ratio:.3f}%."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Transaction Rollback Rate",
                value=round(ratio, 3),
                unit="%",
                target="< 0.1",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Transaction Rollback Rate",
                value="N/A",
                unit="%",
                target="< 0.1",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Check pg_stat_database connectivity.",
            )
        )

    # 2. Connection Saturation
    try:
        rows = await driver.execute_query(
            "SELECT count(*) AS used, "
            "COALESCE(nullif(current_setting('max_connections')::int, 0), 100) AS maximum "
            "FROM pg_stat_activity",
            force_readonly=True,
        )
        used = int((rows or [])[0].cells["used"] or 0)
        maximum = int((rows or [])[0].cells["maximum"] or 100)
        ratio = (used / maximum) * 100

        if ratio > 80.0:
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"High connection use: {used}/{maximum} ({ratio:.1f}%)."
            suggestion = "Identify connection leaks. Configure connection poolers like PgBouncer."
        elif ratio > 60.0:
            status = "WARNING"
            severity = 2
            category_score -= 5
            evidence = f"Growing connections: {used}/{maximum} ({ratio:.1f}%)."
            suggestion = "Monitor backend processes. Check application pools."
        else:
            status = "GOOD"
            severity = 0
            evidence = f"Safe connection level: {used}/{maximum} ({ratio:.1f}%)."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Connection Saturation",
                value=round(ratio, 1),
                unit="%",
                target="< 80",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Connection Saturation",
                value="N/A",
                unit="%",
                target="< 80",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Check pg_stat_activity permissions.",
            )
        )

    # 3. Transaction ID Wraparound Age
    try:
        rows = await driver.execute_query(
            "SELECT age(datfrozenxid) AS age_xid FROM pg_database WHERE datname = current_database()",
            force_readonly=True,
        )
        age_val = int((rows or [])[0].cells["age_xid"] or 0)

        if age_val > 150000000:
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"Oldest transaction ID is {age_val} transactions old."
            suggestion = "Schedule a system-wide aggressive VACUUM FREEZE to prevent catastrophic wraparound lockup."
        elif age_val > 80000000:
            status = "WARNING"
            severity = 2
            category_score -= 5
            evidence = f"Oldest XID is {age_val} transactions old."
            suggestion = "Configure more aggressive autovacuum freeze settings."
        else:
            status = "GOOD"
            severity = 0
            evidence = f"XID is safe. Age: {age_val}."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Transaction ID Wraparound Age",
                value=age_val,
                unit="transactions",
                target="< 1.5B",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Transaction ID Wraparound Age",
                value="N/A",
                unit="transactions",
                target="< 1.5B",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Check pg_database catalog.",
            )
        )

    # 4. Forgotten Prepared Transactions
    try:
        rows = await driver.execute_query(
            "SELECT count(*) AS prepared_count FROM pg_prepared_xacts",
            force_readonly=True,
        )
        p_count = int((rows or [])[0].cells["prepared_count"] or 0)

        if p_count > 0:
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"{p_count} prepared transactions are dangling in pg_prepared_xacts."
            suggestion = "Dangling 2PC transactions lock tables and cause wraparound; execute ROLLBACK PREPARED."
        else:
            status = "GOOD"
            severity = 0
            evidence = "Zero prepared transactions detected."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Prepared Transactions",
                value=p_count,
                unit="transactions",
                target="0",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Prepared Transactions",
                value="N/A",
                unit="transactions",
                target="0",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Check pg_prepared_xacts catalog.",
            )
        )

    cat_score = max(0, category_score)
    status_label = "GOOD" if cat_score >= 90 else ("WARNING" if cat_score >= 70 else "CRITICAL")
    return CategoryResult(
        category="Transaction & Connection Health",
        status=status_label,
        score=cat_score,
        metrics=metrics,
    )


async def audit_concurrency_locks(driver: SqlDriver) -> CategoryResult:
    """Analyze lock contention, longest wait times, blocking paths, and deadlocks."""
    metrics: list[MetricResult] = []
    category_score = 100

    # 1. Lock Wait Count
    try:
        rows = await driver.execute_query(
            "SELECT count(*) AS count_waiting FROM pg_locks WHERE NOT granted",
            force_readonly=True,
        )
        waiting = int((rows or [])[0].cells["count_waiting"] or 0)

        if waiting > 5:
            status = "CRITICAL"
            severity = 3
            category_score -= 20
            evidence = f"{waiting} backends are blocked and waiting on database locks."
            suggestion = "Run find_blocking_chains and terminate the root blocker sessions immediately."
        elif waiting > 0:
            status = "WARNING"
            severity = 2
            category_score -= 10
            evidence = f"{waiting} backends are waiting on locks."
            suggestion = "Investigate queries holding exclusive locks."
        else:
            status = "GOOD"
            severity = 0
            evidence = "No lock wait contention detected."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Lock Wait Count",
                value=waiting,
                unit="backends",
                target="< 5",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Lock Wait Count",
                value="N/A",
                unit="backends",
                target="< 5",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Check pg_locks catalog.",
            )
        )

    # 2. Longest Lock Wait Duration
    try:
        rows = await driver.execute_query(
            "SELECT COALESCE(max(extract(epoch FROM (now() - state_change))), 0) AS max_wait "
            "FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock'",
            force_readonly=True,
        )
        max_duration = float((rows or [])[0].cells["max_wait"] or 0.0)

        if max_duration > 60.0:
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"Longest waiting backend has been blocked for {max_duration:.1f} seconds."
            suggestion = "Kill the blocking query or configure standard lock_timeout thresholds."
        elif max_duration > 5.0:
            status = "WARNING"
            severity = 2
            category_score -= 5
            evidence = f"Lock wait delay detected: {max_duration:.1f} seconds."
            suggestion = "Configure lock_timeout to fail fast rather than block connections."
        else:
            status = "GOOD"
            severity = 0
            evidence = "Lock wait duration is well within thresholds."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Longest Lock Wait",
                value=round(max_duration, 1),
                unit="seconds",
                target="< 60",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Longest Lock Wait",
                value="N/A",
                unit="seconds",
                target="< 60",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Verify permissions on pg_stat_activity.",
            )
        )

    # 3. Deadlocks
    try:
        rows = await driver.execute_query(
            "SELECT sum(deadlocks) AS deadlocks FROM pg_stat_database",
            force_readonly=True,
        )
        deadlock_count = int((rows or [])[0].cells["deadlocks"] or 0)

        if deadlock_count > 5:
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"{deadlock_count} deadlocks occurred since stats reset."
            suggestion = "Ensure concurrent application updates process objects in the exact same index order."
        elif deadlock_count > 0:
            status = "WARNING"
            severity = 2
            category_score -= 5
            evidence = f"{deadlock_count} deadlocks occurred."
            suggestion = "Monitor deadlocks and minimize transaction runtimes."
        else:
            status = "GOOD"
            severity = 0
            evidence = "Zero deadlocks occurred."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Deadlock Count",
                value=deadlock_count,
                unit="last hour",
                target="0",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Deadlock Count",
                value="N/A",
                unit="last hour",
                target="0",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Verify permissions.",
            )
        )

    cat_score = max(0, category_score)
    status_label = "GOOD" if cat_score >= 90 else ("WARNING" if cat_score >= 70 else "CRITICAL")
    return CategoryResult(
        category="Concurrency & Lock Contention",
        status=status_label,
        score=cat_score,
        metrics=metrics,
    )


async def audit_cleanliness_bloat(driver: SqlDriver, schema: str) -> CategoryResult:
    """Analyze table cleanliness: dead tuple ratios and invalid indexes."""
    metrics: list[MetricResult] = []
    category_score = 100

    # 1. Dead Tuple Ratio
    try:
        rows = await driver.execute_query(
            "SELECT count(*) FILTER ("
            "  WHERE n_dead_tup > 100 AND (n_dead_tup::numeric / GREATEST(n_live_tup + n_dead_tup, 1)) * 100 > 10.0"
            ") AS bloated_tables, "
            "COALESCE("
            "  max((n_dead_tup::numeric / GREATEST(n_live_tup + n_dead_tup, 1)) * 100), "
            "  0.0"
            ") AS max_bloat_pct "
            "FROM pg_stat_user_tables "
            "WHERE schemaname = %s",
            params=[schema],
            force_readonly=True,
        )
        cells = (rows or [])[0].cells
        bloated_tables = int(cells["bloated_tables"] or 0)
        max_bloat = float(cells["max_bloat_pct"] or 0.0)

        if bloated_tables > 0:
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"{bloated_tables} tables have dead tuple ratios > 10% (Peak: {max_bloat:.1f}%)."
            suggestion = "Run manual VACUUM ANALYZE on bloated tables. Tune autovacuum scale factors."
        else:
            status = "GOOD"
            severity = 0
            evidence = f"Clean schema. Peak dead tuple ratio: {max_bloat:.1f}%."
            suggestion = "Autovacuum is successfully keeping up."

        metrics.append(
            MetricResult(
                name="Dead Tuple Ratio",
                value=round(max_bloat, 1),
                unit="%",
                target="< 10",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Dead Tuple Ratio",
                value="N/A",
                unit="%",
                target="< 10",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Verify permissions on pg_stat_user_tables.",
            )
        )

    # 2. Invalid Indexes
    try:
        rows = await driver.execute_query(
            "SELECT count(*) AS invalid FROM pg_index idx "
            "JOIN pg_class c ON c.oid = idx.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND NOT idx.indisvalid",
            params=[schema],
            force_readonly=True,
        )
        invalid = int((rows or [])[0].cells["invalid"] or 0)

        if invalid > 0:
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"{invalid} invalid or corrupted indexes exist in schema {schema!r}."
            suggestion = "Rebuild invalid indexes immediately using REINDEX INDEX CONCURRENTLY."
        else:
            status = "GOOD"
            severity = 0
            evidence = "Zero invalid indexes detected."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Invalid Index Count",
                value=invalid,
                unit="indexes",
                target="0",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Invalid Index Count",
                value="N/A",
                unit="indexes",
                target="0",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Verify catalog access.",
            )
        )

    cat_score = max(0, category_score)
    status_label = "GOOD" if cat_score >= 90 else ("WARNING" if cat_score >= 70 else "CRITICAL")
    return CategoryResult(
        category="Table Cleanliness & Bloat",
        status=status_label,
        score=cat_score,
        metrics=metrics,
    )


async def audit_slow_queries(driver: SqlDriver) -> CategoryResult:
    """Analyze query execution latencies and check active long-running backends."""
    metrics: list[MetricResult] = []
    category_score = 100

    # 1. Queries running > 60s
    try:
        rows = await driver.execute_query(
            "SELECT count(*) AS long_queries "
            "FROM pg_stat_activity "
            "WHERE state = 'active' AND (now() - query_start) > interval '60 seconds'",
            force_readonly=True,
        )
        long_q = int((rows or [])[0].cells["long_queries"] or 0)

        if long_q > 3:
            status = "CRITICAL"
            severity = 3
            category_score -= 15
            evidence = f"{long_q} active queries have been running for more than 60 seconds."
            suggestion = "Analyze active slow sessions. Terminate problematic backends using cancel_query."
        elif long_q > 0:
            status = "WARNING"
            severity = 2
            category_score -= 5
            evidence = f"{long_q} query is running longer than 60 seconds."
            suggestion = "Inspect locks or plan costs for slow-running transactions."
        else:
            status = "GOOD"
            severity = 0
            evidence = "No queries are running longer than 60 seconds."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Queries Running > 60s",
                value=long_q,
                unit="queries",
                target="< 3",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception as exc:
        metrics.append(
            MetricResult(
                name="Queries Running > 60s",
                value="N/A",
                unit="queries",
                target="< 3",
                status="WARNING",
                severity=2,
                evidence=f"Failed: {exc}",
                suggestion="Verify permissions.",
            )
        )

    # 2. pg_stat_statements: Top Time Consumer
    try:
        rows = await driver.execute_query(
            "SELECT query, calls, total_exec_time, mean_exec_time "
            "FROM pg_stat_statements "
            "ORDER BY total_exec_time DESC "
            "LIMIT 1",
            force_readonly=True,
        )
        if rows:
            cells = rows[0].cells
            calls = int(cells["calls"] or 0)
            total_time = float(cells["total_exec_time"] or 0.0)
            mean_time = float(cells["mean_exec_time"] or 0.0)
            query_str = str(cells["query"])[:100] + "..."

            status = "WARNING"
            severity = 2
            evidence = (
                f"Top query template executed {calls} times, "
                f"taking {total_time / 1000:.1f}s total (Mean: {mean_time:.1f}ms): {query_str}"
            )
            suggestion = "Rewrite/optimize with optimize_query, or verify that indexes cover join/filter columns."
        else:
            status = "GOOD"
            severity = 0
            evidence = "pg_stat_statements is active but empty."
            suggestion = "No action needed."

        metrics.append(
            MetricResult(
                name="Top Time Consumer",
                value=calls if "calls" in locals() else 0,
                unit="calls",
                target="Optimize",
                status=status,
                severity=severity,
                evidence=evidence,
                suggestion=suggestion,
            )
        )
    except Exception:
        # Gracefully degrade if pg_stat_statements is not installed/enabled
        category_score -= 10
        metrics.append(
            MetricResult(
                name="Top Time Consumer",
                value="Not Installed",
                unit="",
                target="N/A",
                status="WARNING",
                severity=2,
                evidence="pg_stat_statements extension is not installed or enabled in the database.",
                suggestion=(
                    "Add pg_stat_statements to shared_preload_libraries, "
                    "restart, and run 'CREATE EXTENSION pg_stat_statements;'"
                ),
            )
        )

    cat_score = max(0, category_score)
    status_label = "GOOD" if cat_score >= 90 else ("WARNING" if cat_score >= 70 else "CRITICAL")
    return CategoryResult(
        category="Slow Query Profiling",
        status=status_label,
        score=cat_score,
        metrics=metrics,
    )


async def _check_custom_logs(driver: SqlDriver, log_table: str | None) -> list[TopIssue]:
    """Scan custom server log tables for errors if configured and present in system catalogs."""
    issues: list[TopIssue] = []
    if not log_table:
        return issues

    # Verify that the table exists first to prevent execution runtime errors
    try:
        parts = log_table.split(".")
        schema_filter = parts[0] if len(parts) > 1 else "public"
        table_filter = parts[1] if len(parts) > 1 else parts[0]

        exists_rows = await driver.execute_query(
            "SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s",
            params=[schema_filter, table_filter],
            force_readonly=True,
        )
        if not exists_rows:
            return issues  # table doesn't exist, gracefully ignore

        # Safely quote identifiers to prevent SQL injection
        safe_schema = schema_filter.replace('"', '""')
        safe_table = table_filter.replace('"', '""')
        safe_log_table = f'"{safe_schema}"."{safe_table}"'

        # Run log query safely (supports standard CSV-destination format schemas)
        rows = await driver.execute_query(
            f"SELECT error_severity, count(*) AS count "
            f"FROM {safe_log_table} "
            f"WHERE log_time > now() - interval '1 hour' "
            f"  AND error_severity IN ('ERROR', 'FATAL', 'PANIC') "
            f"GROUP BY error_severity",
            force_readonly=True,
        )

        for row in rows or []:
            severity = str(row.cells["error_severity"])
            count = int(row.cells["count"])
            issues.append(
                TopIssue(
                    issue=f"Recent Server Log Errors ({severity})",
                    severity="CRITICAL" if severity in ("FATAL", "PANIC") else "HIGH",
                    affected_component=f"Logs: {log_table}",
                    root_cause=f"{count} database level {severity} logs captured in the last hour.",
                    suggested_action=f"Inspect severity error entries directly in `{log_table}` table.",
                )
            )
    except Exception:
        pass  # Gracefully degrade and ignore if columns differ or query fails

    return issues


async def audit_database(driver: SqlDriver, schema: str, log_table: str | None = None) -> AuditReport:
    """Execute comprehensive performance checks, compile scores, recommendations, and issues."""
    from mcpg.pg_search import audit_pg_search_indexes
    from mcpg.rag_efficiency import audit_rag_pipeline, audit_vector_indexes
    from mcpg.turboquant import audit_turboquant_indexes

    ver, dbname = await _get_version_and_db(driver)

    # 1. Execute categories
    cat_mem = await audit_memory_io(driver, {})
    cat_tx = await audit_transactions_connections(driver)
    cat_lock = await audit_concurrency_locks(driver)
    cat_bloat = await audit_cleanliness_bloat(driver, schema)
    cat_slow = await audit_slow_queries(driver)

    # Optional categories — only included when the relevant extension
    # is installed, so a stock cluster's scorecard isn't padded with
    # empty sections and the overall score isn't diluted.
    cat_turboquant = await audit_turboquant_indexes(driver)
    cat_pg_search = await audit_pg_search_indexes(driver)
    cat_vector = await audit_vector_indexes(driver)
    cat_rag_pipeline = await audit_rag_pipeline(driver)

    categories = [cat_mem, cat_tx, cat_lock, cat_bloat, cat_slow]
    if cat_turboquant is not None:
        categories.append(cat_turboquant)
    if cat_pg_search is not None:
        categories.append(cat_pg_search)
    if cat_vector is not None:
        categories.append(cat_vector)
    if cat_rag_pipeline is not None:
        categories.append(cat_rag_pipeline)

    # 2. Dynamic scoring
    overall_score = round(sum(cat.score for cat in categories) / len(categories))
    overall_health = "GOOD" if overall_score >= 90 else ("WARNING" if overall_score >= 70 else "CRITICAL")

    # 3. Compile top issues
    top_issues: list[TopIssue] = []
    recommendations: list[Recommendation] = []

    # Process metrics to find warnings and critical issues
    for cat in categories:
        for metric in cat.metrics:
            if metric.status in ("WARNING", "CRITICAL"):
                sev_label = "CRITICAL" if metric.status == "CRITICAL" else "HIGH"
                top_issues.append(
                    TopIssue(
                        issue=metric.name,
                        severity=sev_label,
                        affected_component=f"Catalog: {metric.name}",
                        root_cause=metric.evidence,
                        suggested_action=metric.suggestion,
                    )
                )

                # Add matching recommendation
                rec_priority = "CRITICAL" if metric.status == "CRITICAL" else "MEDIUM"
                recommendations.append(
                    Recommendation(
                        priority=rec_priority,
                        action=metric.suggestion,
                        objects=[metric.name],
                        estimated_impact=f"Improve category score for {cat.category}",
                    )
                )

    # Incorporate server log scanning
    log_issues = await _check_custom_logs(driver, log_table)
    top_issues.extend(log_issues)
    for issue in log_issues:
        recommendations.append(
            Recommendation(
                priority=issue.severity,
                action=issue.suggested_action,
                objects=[issue.affected_component],
                estimated_impact="Address host-level logging alerts",
            )
        )

    # Keep a raw snapshot summary (useful for detailed inspections)
    snapshot = {
        "scorecard": {cat.category: cat.score for cat in categories},
        "audited_schema": schema,
    }

    return AuditReport(
        timestamp=datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        database=dbname,
        version=ver,
        overall_health=overall_health,
        health_score=overall_score,
        categories=categories,
        top_issues=top_issues,
        recommendations=recommendations,
        raw_stats_snapshot=snapshot,
    )
