"""SQL audit trail with optional persistence to ``mcpg_audit.events``.

Distinct from :mod:`mcpg.audit`, which logs every MCP tool invocation
to the ``mcpg.audit`` *Python logger*. This module:

* Defines a typed :class:`AuditTrailEntry` and a structured
  :class:`SchemaDiffSnapshot` for DDL semantic diffs.
* Idempotently creates an ``mcpg_audit.events`` table (when
  ``MCPG_AUDIT_PERSIST`` is enabled) and writes one row per
  ``run_write`` / ``run_ddl`` invocation.
* Exposes :func:`list_audit_events` to read the trail back.

Persistence is opt-in. With the default ``audit_persist=False``,
``run_write`` and ``run_ddl`` retain their original behaviour and no
``mcpg_audit`` schema is ever touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from os import environ
from typing import Any

from mcpg._vendor.sql import SqlDriver, obfuscate_password
from mcpg.audit import is_secret_key
from mcpg.audit_nl2sql import Backend
from mcpg.audit_nl2sql import NL2SQLAuditError as _SharedAuditError
from mcpg.audit_nl2sql import _check_identifier as _shared_check_identifier
from mcpg.audit_nl2sql import _check_interval as _shared_check_interval
from mcpg.audit_nl2sql import detect_backend as _detect_backend
from mcpg.config import _parse_bool
from mcpg.extensions import extension_installed

_AUDIT_LOCK: asyncio.Lock | None = None


def _get_audit_lock() -> asyncio.Lock:
    global _AUDIT_LOCK
    if _AUDIT_LOCK is None:
        _AUDIT_LOCK = asyncio.Lock()
    return _AUDIT_LOCK


AUDIT_SCHEMA = "mcpg_audit"
AUDIT_TABLE = "events"
CHAIN_TIP_TABLE = "chain_tip"

_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"
_QUALIFIED_CHAIN_TIP = f"{AUDIT_SCHEMA}.{CHAIN_TIP_TABLE}"

_MASK = "****"


class AuditTrailError(Exception):
    """Raised when an audit-trail maintenance operation is rejected."""


def _audit_check_identifier(name: str, *, kind: str) -> None:
    """Identifier check that raises ``AuditTrailError`` instead of the
    shared ``NL2SQLAuditError`` so callers of this module see a
    consistent exception type."""
    try:
        _shared_check_identifier(name, kind=kind)
    except _SharedAuditError as exc:
        raise AuditTrailError(str(exc)) from exc


def _audit_check_interval(value: str, *, kind: str) -> None:
    """Interval check translated to ``AuditTrailError`` — same
    reasoning as :func:`_audit_check_identifier`."""
    try:
        _shared_check_interval(value, kind=kind)
    except _SharedAuditError as exc:
        raise AuditTrailError(str(exc)) from exc


@dataclass(frozen=True)
class PruneResult:
    """The outcome of a :func:`prune_audit_events` call.

    ``deleted`` is the number of rows removed. ``cutoff`` is the
    ISO-8601 timestamp before which events were pruned, or ``None`` when
    no prune ran (the audit table doesn't exist yet). ``remaining`` is
    the row count left in the table afterwards.
    """

    deleted: int
    cutoff: str | None
    remaining: int


@dataclass(frozen=True)
class AuditTrailEntry:
    """A persisted SQL audit row read back from ``mcpg_audit.events``."""

    id: int
    occurred_at: Any  # naive timestamp - the driver returns datetime
    tool: str
    arguments: dict[str, Any]
    status: str
    error: str | None
    result: dict[str, Any] | None


@dataclass(frozen=True)
class SchemaDiffSnapshot:
    """Structured before/after column lists for a single DDL-affected table."""

    schema: str
    table: str
    columns_before: list[dict[str, Any]]
    columns_after: list[dict[str, Any]]


def _redact(value: Any) -> Any:
    """Recursively mask credentials in dicts, lists, and strings.

    Mapping keys named like a credential have their value masked
    wholesale. String leaves are passed through ``obfuscate_password``
    so an embedded connection-string credential — even nested deep in a
    ``RETURNING`` payload — never reaches the audit table in plain text.
    Non-string scalars (ints, bools, None) pass through unchanged.
    """
    if isinstance(value, dict):
        return {k: _MASK if isinstance(k, str) and is_secret_key(k) else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, str):
        return obfuscate_password(value)
    return value


# Drivers that have already had ensure_audit_table run successfully.
# Keyed by id(driver) so the cache doesn't pin drivers in memory or
# survive test teardown (each test builds a fresh driver instance,
# which gets its own id).
_ENSURED_DRIVER_IDS: set[int] = set()


def _reset_audit_init_cache() -> None:
    """Forget which drivers have had the audit table ensured.

    Test-only escape hatch; production code should never call this.
    """
    _ENSURED_DRIVER_IDS.clear()


async def ensure_audit_table(driver: SqlDriver) -> None:
    """Create the ``mcpg_audit.events`` schema/table if it doesn't exist.

    The CREATEs are idempotent (``IF NOT EXISTS``) but each round-trip
    still costs catalog locks and a network hop. We cache per-driver so
    every subsequent call on the same driver instance is a no-op —
    after the first write, audit recording costs one INSERT per call.
    """
    if id(driver) in _ENSURED_DRIVER_IDS:
        return
    await driver.execute_query(
        f"CREATE SCHEMA IF NOT EXISTS {AUDIT_SCHEMA}",
        force_readonly=False,
    )
    await driver.execute_query(
        f"CREATE TABLE IF NOT EXISTS {_QUALIFIED} ("
        "  id bigserial PRIMARY KEY, "
        "  occurred_at timestamptz NOT NULL DEFAULT now(), "
        "  tool text NOT NULL, "
        "  arguments jsonb NOT NULL, "
        "  status text NOT NULL, "
        "  error text, "
        "  result jsonb, "
        "  prev_hmac text, "
        "  event_hmac text"
        ")",
        force_readonly=False,
    )
    # Idempotently add the integrity columns if the table already exists without them
    await driver.execute_query(
        f"ALTER TABLE {_QUALIFIED} ADD COLUMN IF NOT EXISTS prev_hmac text",
        force_readonly=False,
    )
    await driver.execute_query(
        f"ALTER TABLE {_QUALIFIED} ADD COLUMN IF NOT EXISTS event_hmac text",
        force_readonly=False,
    )
    # The chain_tip table anchors verify_audit_chain against the
    # tail-truncation attack flagged in deep-review P1 #6. Single row
    # by construction: id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1) so
    # the ON CONFLICT (id) upsert in record_audit always lands on the
    # same row. last_event_id / last_event_hmac are NULL-allowed so
    # the row can be CREATEd up-front; the first integrity-enabled
    # write populates them.
    await driver.execute_query(
        f"CREATE TABLE IF NOT EXISTS {_QUALIFIED_CHAIN_TIP} ("
        "  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1), "
        "  last_event_id bigint, "
        "  last_event_hmac text, "
        "  updated_at timestamptz NOT NULL DEFAULT now()"
        ")",
        force_readonly=False,
    )
    _ENSURED_DRIVER_IDS.add(id(driver))


async def record_audit(
    driver: SqlDriver,
    *,
    tool: str,
    arguments: dict[str, Any],
    status: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Append one audit row to ``mcpg_audit.events``.

    Credentials in both ``arguments`` and ``result`` are masked
    recursively via :func:`_redact` before persisting — a run_write
    with a ``RETURNING password`` clause must never leak plaintext into
    the audit table. ``None`` ``result`` is stored as SQL NULL.
    """
    async with _get_audit_lock():
        await ensure_audit_table(driver)
        safe_args = _redact(arguments)
        safe_result = _redact(result) if result is not None else None
        # The error field is filled with ``str(exc)`` from the calling
        # write/DDL path (see write.py:_persist_audit), and psycopg /
        # libpq routinely embed DSN fragments or table-literal values
        # in their error messages — passing it raw to SQL would persist
        # plaintext secrets into mcpg_audit.events.error. Pipe it
        # through the same obfuscate_password sweep used by the
        # arguments/result redaction so the column matches the
        # documented "credentials never persisted unmasked" contract.
        safe_error = obfuscate_password(error) if error is not None else None

        # Check if integrity signature chain is enabled
        settings = getattr(driver, "settings", None)
        if settings is not None:
            audit_integrity = settings.audit_integrity
            audit_hmac_key = settings.audit_hmac_key
        else:
            audit_integrity = False
            if (raw := environ.get("MCPG_AUDIT_INTEGRITY")) is not None:
                try:
                    audit_integrity = _parse_bool("MCPG_AUDIT_INTEGRITY", raw)
                except Exception:
                    pass

            key_str = environ.get("MCPG_AUDIT_HMAC_KEY", "").strip()
            audit_hmac_key = key_str if key_str else None

        prev_hmac: str | None = None
        event_hmac: str | None = None
        now_dt = datetime.now(UTC)

        if audit_integrity and audit_hmac_key is not None:
            # Fetch the event_hmac of the immediately preceding row
            prev_rows = await driver.execute_query(
                f"SELECT event_hmac FROM {_QUALIFIED} ORDER BY id DESC LIMIT 1",
                force_readonly=True,
            )
            prev_hmac = ""
            if prev_rows:
                prev_hmac = prev_rows[0].cells.get("event_hmac") or ""

            # Formulate deterministic payload byte string of the current event
            occurred_at_str = now_dt.isoformat()
            payload_data = {
                "occurred_at": occurred_at_str,
                "tool": tool,
                "arguments": safe_args,
                "status": status,
                # Must match what we persist in the INSERT below; using
                # the raw ``error`` here would have a verifier signing
                # plaintext while the table holds the redacted form and
                # break verify_audit_chain.
                "error": safe_error,
                "result": safe_result,
            }
            payload_bytes = json.dumps(payload_data, sort_keys=True, default=str).encode("utf-8")

            # Compute HMAC signature
            key_bytes = audit_hmac_key.encode("utf-8")
            data_to_sign = prev_hmac.encode("utf-8") + payload_bytes
            event_hmac = hmac.new(key_bytes, data_to_sign, hashlib.sha256).hexdigest()

        # When the HMAC chain is enabled, the event INSERT and the
        # chain_tip UPSERT must land atomically — otherwise an
        # attacker who can race between the two could DELETE the just-
        # inserted row and leave chain_tip pointing one step back.
        # PostgreSQL gives us that atomicity for free via a writable-
        # CTE single statement: events INSERT runs first, the
        # chain_tip UPSERT consumes its RETURNING row in the same
        # transaction, and the whole thing is one ON COMMIT step.
        # Non-integrity writes use the plain INSERT — no chain_tip
        # work, same cost as before.
        event_columns = "tool, arguments, status, error, result, occurred_at, prev_hmac, event_hmac"
        event_placeholders = "%s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s"
        event_params: list[Any] = [
            tool,
            json.dumps(safe_args, default=str),
            status,
            safe_error,
            json.dumps(safe_result, default=str) if safe_result is not None else None,
            now_dt,
            prev_hmac,
            event_hmac,
        ]
        if audit_integrity and audit_hmac_key is not None:
            await driver.execute_query(
                f"WITH new_event AS ("
                f"  INSERT INTO {_QUALIFIED} ({event_columns}) "
                f"  VALUES ({event_placeholders}) "
                f"  RETURNING id, event_hmac"
                f") "
                f"INSERT INTO {_QUALIFIED_CHAIN_TIP} (id, last_event_id, last_event_hmac) "
                f"SELECT 1, id, event_hmac FROM new_event "
                f"ON CONFLICT (id) DO UPDATE "
                f"SET last_event_id = EXCLUDED.last_event_id, "
                f"    last_event_hmac = EXCLUDED.last_event_hmac, "
                f"    updated_at = now()",
                params=event_params,
                force_readonly=False,
            )
        else:
            await driver.execute_query(
                f"INSERT INTO {_QUALIFIED} ({event_columns}) VALUES ({event_placeholders})",
                params=event_params,
                force_readonly=False,
            )


async def list_audit_events(driver: SqlDriver, *, limit: int = 100, tool: str | None = None) -> list[AuditTrailEntry]:
    """Read recent rows from ``mcpg_audit.events``, newest first.

    Returns an empty list when the audit table hasn't been created yet —
    the caller is expected to interpret that as "no audited events" rather
    than an error.
    """
    if not await _audit_table_exists(driver):
        return []
    where = ""
    params: list[Any] = []
    if tool is not None:
        where = "WHERE tool = %s "
        params.append(tool)
    params.append(limit)
    rows = await driver.execute_query(
        f"SELECT id, occurred_at, tool, arguments, status, error, result "
        f"FROM {_QUALIFIED} {where}ORDER BY id DESC LIMIT %s",
        params=params,
        force_readonly=True,
    )
    return [
        AuditTrailEntry(
            id=row.cells["id"],
            occurred_at=row.cells["occurred_at"],
            tool=row.cells["tool"],
            arguments=row.cells["arguments"],
            status=row.cells["status"],
            error=row.cells["error"],
            result=row.cells["result"],
        )
        for row in rows or []
    ]


async def prune_audit_events(
    driver: SqlDriver,
    *,
    older_than_days: int,
    integrity_enabled: bool = False,
) -> PruneResult:
    """Delete persisted audit events older than ``older_than_days``.

    A cron-friendly retention helper for the unbounded
    ``mcpg_audit.events`` table. The cutoff is ``now() - interval`` in
    the database's clock.

    Refuses to run when ``integrity_enabled`` is true: the HMAC chain
    anchors its first row on an empty ``prev_hmac``, so deleting the
    oldest rows would leave a row whose ``prev_hmac`` references a
    now-gone event — :func:`mcpg.audit_integrity.verify_audit_chain`
    would then (correctly) report tampering. Operators who need both
    retention and integrity should export-then-truncate out of band;
    re-anchoring the chain in place is deferred.

    Returns a :class:`PruneResult`; pruning a not-yet-created table is a
    no-op (``deleted=0``).
    """
    if older_than_days < 1:
        raise AuditTrailError("older_than_days must be at least 1")
    if integrity_enabled:
        raise AuditTrailError(
            "refusing to prune mcpg_audit.events while MCPG_AUDIT_INTEGRITY is enabled: "
            "deleting the oldest rows would break the HMAC signature chain and make "
            "verify_audit_chain report tampering. Disable integrity to prune, or export "
            "and truncate out of band."
        )
    if not await _audit_table_exists(driver):
        return PruneResult(deleted=0, cutoff=None, remaining=0)

    # Aggregate the delete count inside the CTE rather than RETURNING one
    # row per deleted event — pruning millions of rows must not
    # materialise millions of result rows in memory. The query always
    # returns exactly one row (count over the empty set is 0), so the
    # cutoff timestamp comes back even when nothing matched.
    rows = await driver.execute_query(
        f"WITH cutoff AS (SELECT now() - make_interval(days => %s) AS ts), "
        f"deleted AS (DELETE FROM {_QUALIFIED} WHERE occurred_at < (SELECT ts FROM cutoff) RETURNING 1) "
        f"SELECT count(*) AS deleted_count, (SELECT ts FROM cutoff) AS cutoff_ts FROM deleted",
        params=[older_than_days],
        force_readonly=False,
    )
    deleted = 0
    cutoff: str | None = None
    if rows:
        deleted = int(rows[0].cells.get("deleted_count") or 0)
        cutoff_val = rows[0].cells.get("cutoff_ts")
        if cutoff_val is not None:
            cutoff = cutoff_val.isoformat() if hasattr(cutoff_val, "isoformat") else str(cutoff_val)

    # Remaining count is a SEPARATE statement on purpose: a count folded
    # into the CTE above would read the table at the statement snapshot
    # (before the DELETE is visible) and report the pre-prune total.
    remaining_rows = await driver.execute_query(
        f"SELECT count(*) AS n FROM {_QUALIFIED}",
        force_readonly=True,
    )
    remaining = int(remaining_rows[0].cells.get("n") or 0) if remaining_rows else 0

    return PruneResult(deleted=deleted, cutoff=cutoff, remaining=remaining)


async def _audit_table_exists(driver: SqlDriver) -> bool:
    rows = await driver.execute_query(
        "SELECT 1 AS present FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[AUDIT_SCHEMA, AUDIT_TABLE],
        force_readonly=True,
    )
    return bool(rows)


async def capture_columns(driver: SqlDriver, schema: str, table: str) -> list[dict[str, Any]]:
    """Take a structural snapshot of ``schema.table``'s columns for diffing.

    Returns the column rows verbatim from the catalog (name, type,
    nullable, default) so a caller can store before-and-after lists in
    an :class:`SchemaDiffSnapshot`. Returns ``[]`` when the table does
    not exist (e.g. DROP TABLE — the post-snapshot will be empty).
    """
    rows = await driver.execute_query(
        "SELECT a.attname AS name, "
        "  format_type(a.atttypid, a.atttypmod) AS data_type, "
        "  NOT a.attnotnull AS nullable, "
        "  pg_get_expr(d.adbin, d.adrelid) AS default_value "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE n.nspname = %s AND c.relname = %s "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        params=[schema, table],
        force_readonly=True,
    )
    return [
        {
            "name": row.cells["name"],
            "data_type": row.cells["data_type"],
            "nullable": row.cells["nullable"],
            "default": row.cells["default_value"],
        }
        for row in rows or []
    ]


# --- mcpg_audit.events partitioning retrofit (PR-4) -----------------------
#
# Distinct from the bigserial-only ``ensure_audit_table`` above:
# :func:`migrate_audit_events_to_partitioned` converts an existing
# unpartitioned ``mcpg_audit.events`` into a partitioned/hypertable
# shape — TimescaleDB hypertable if installed, native PG declarative
# partitioning otherwise. Migration preserves the HMAC chain
# (event_hmac / prev_hmac columns + the chain_tip pointer) so
# ``verify_audit_chain`` continues to work post-migration.
#
# Backend ladder mirrors :mod:`mcpg.audit_nl2sql`:
# timescaledb > pg_partman > native. The TimescaleDB path uses
# ``create_hypertable(migrate_data => TRUE)`` which converts the
# existing table in-place. The native + pg_partman paths use a
# rename-create-insert-drop dance inside a single transaction.
#
# Retention is OFF by default for this table — the HMAC chain
# anchors on the oldest event, so dropping it breaks
# ``verify_audit_chain`` (see :func:`prune_audit_events`'s integrity
# guard). Operators who explicitly want chunked retention can set
# ``MCPG_AUDIT_EVENTS_RETENTION_DAYS`` AND disable integrity.


_EVENTS_NATIVE_WINDOW_DAYS = 7


@dataclass(frozen=True)
class EventsAuditMigrationResult:
    """Outcome of :func:`migrate_audit_events_to_partitioned`."""

    migrated: bool
    backend: Backend
    rows_copied: int
    compression_enabled: bool
    retention_days: int | None
    rls_enabled: bool
    reader_role: str | None
    setup_sql: tuple[str, ...]


def _resolve_events_settings(
    env: Any | None,
) -> tuple[str | None, int | None, str, str, bool, str | None]:
    """Read MCPG_AUDIT_EVENTS_* knobs out of env.

    Returns ``(backend, retention_days, chunk_interval, compress_after,
    rls, reader_role)``. ``retention_days`` is ``None`` by default —
    the HMAC chain on the events table makes wholesale dropping of the
    oldest chunks dangerous, so retention only fires when the operator
    explicitly opts in.
    """
    source = env if env is not None else environ
    backend_raw = (source.get("MCPG_AUDIT_EVENTS_BACKEND") or "").strip() or None
    retention_raw = (source.get("MCPG_AUDIT_EVENTS_RETENTION_DAYS") or "").strip()
    retention_days = int(retention_raw) if retention_raw else None
    if retention_days is not None and retention_days < 1:
        raise AuditTrailError(f"MCPG_AUDIT_EVENTS_RETENTION_DAYS must be a positive integer; got {retention_raw!r}")
    chunk_interval = (source.get("MCPG_AUDIT_EVENTS_CHUNK_INTERVAL") or "").strip() or "1 day"
    _audit_check_interval(chunk_interval, kind="MCPG_AUDIT_EVENTS_CHUNK_INTERVAL")
    compress_after = (source.get("MCPG_AUDIT_EVENTS_COMPRESS_AFTER") or "").strip() or "7 days"
    _audit_check_interval(compress_after, kind="MCPG_AUDIT_EVENTS_COMPRESS_AFTER")
    rls_raw = (source.get("MCPG_AUDIT_EVENTS_RLS") or "").strip().lower()
    rls = rls_raw in ("", "true", "1", "yes", "on")
    reader_role = (source.get("MCPG_AUDIT_EVENTS_READER_ROLE") or "").strip() or None
    if reader_role is not None:
        _audit_check_identifier(reader_role, kind="audit-events reader role")
    return backend_raw, retention_days, chunk_interval, compress_after, rls, reader_role


async def _events_table_is_partitioned(driver: SqlDriver) -> bool:
    """Return True when mcpg_audit.events is already a partitioned table
    (native PG) or a TimescaleDB hypertable."""
    rows = await driver.execute_query(
        "SELECT 1 FROM pg_partitioned_table pt "
        "JOIN pg_class c ON c.oid = pt.partrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[AUDIT_SCHEMA, AUDIT_TABLE],
        force_readonly=True,
    )
    if rows:
        return True
    # TimescaleDB: probe the hypertable catalog if the extension is present.
    if await extension_installed(driver, "timescaledb"):
        ht_rows = await driver.execute_query(
            "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_schema = %s AND hypertable_name = %s",
            params=[AUDIT_SCHEMA, AUDIT_TABLE],
            force_readonly=True,
        )
        if ht_rows:
            return True
    return False


async def _events_data_range(driver: SqlDriver) -> tuple[datetime | None, datetime | None, int]:
    """Return ``(min_occurred_at, max_occurred_at, row_count)`` for events.

    Used to pre-create historical partitions on the native + pg_partman
    paths. Empty table returns ``(None, None, 0)`` so the caller can
    skip the partition-pre-create step.
    """
    rows = await driver.execute_query(
        f"SELECT min(occurred_at) AS lo, max(occurred_at) AS hi, count(*) AS n FROM {_QUALIFIED}",
        force_readonly=True,
    )
    if not rows:
        return (None, None, 0)
    cells = rows[0].cells
    return (cells.get("lo"), cells.get("hi"), int(cells.get("n") or 0))


def _events_native_partition_sql(month_start: datetime) -> str:
    """``CREATE TABLE IF NOT EXISTS`` for one monthly historical partition.

    Monthly granularity for the backfill window keeps partition counts
    sensible even for multi-year deployments (12 partitions/year vs
    365 for daily).
    """
    # Normalise to the first of the month.
    start = month_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Next month's first day. Avoid relativedelta to keep no deps.
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    suffix = start.strftime("%Y%m")
    return (
        f"CREATE TABLE IF NOT EXISTS {AUDIT_SCHEMA}.{AUDIT_TABLE}_p{suffix} "
        f"PARTITION OF {_QUALIFIED} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def _events_native_daily_partition_sql(day: datetime) -> str:
    """Daily child partition for the trailing window — keeps recent
    writes on focused partitions for fast retention drops later."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (
        f"CREATE TABLE IF NOT EXISTS {AUDIT_SCHEMA}.{AUDIT_TABLE}_p{start.strftime('%Y%m%d')} "
        f"PARTITION OF {_QUALIFIED} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


async def _events_apply_rls(
    driver: SqlDriver,
    *,
    enabled: bool,
    reader_role: str | None,
    statements: list[str],
) -> None:
    """Apply ALTER … ENABLE / FORCE ROW LEVEL SECURITY and an optional
    reader-role SELECT policy. Idempotent on re-runs via the DO-block
    EXCEPTION trick (CREATE POLICY has no IF NOT EXISTS)."""
    if not enabled:
        return
    sql_rls = f"ALTER TABLE {_QUALIFIED} ENABLE ROW LEVEL SECURITY"
    await driver.execute_query(sql_rls, force_readonly=False)
    statements.append(sql_rls)
    sql_force = f"ALTER TABLE {_QUALIFIED} FORCE ROW LEVEL SECURITY"
    await driver.execute_query(sql_force, force_readonly=False)
    statements.append(sql_force)
    if reader_role is None:
        return
    policy_name = f"{AUDIT_TABLE}_reader_select"
    sql_policy = (
        "DO $$ BEGIN "
        f"  CREATE POLICY {policy_name} ON {_QUALIFIED} "
        f"    FOR SELECT TO {reader_role} USING (true); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    await driver.execute_query(sql_policy, force_readonly=False)
    statements.append(sql_policy)
    sql_grant_schema = f"GRANT USAGE ON SCHEMA {AUDIT_SCHEMA} TO {reader_role}"
    await driver.execute_query(sql_grant_schema, force_readonly=False)
    statements.append(sql_grant_schema)
    sql_grant_tab = f"GRANT SELECT ON {_QUALIFIED} TO {reader_role}"
    await driver.execute_query(sql_grant_tab, force_readonly=False)
    statements.append(sql_grant_tab)


async def _events_migrate_native(
    driver: SqlDriver,
    *,
    rls: bool,
    reader_role: str | None,
) -> tuple[int, bool, list[str]]:
    """Rename → create partitioned → copy data → drop legacy dance.

    Runs under an ACCESS EXCLUSIVE lock so concurrent record_audit
    calls block instead of racing. Returns
    ``(rows_copied, compression_enabled, statements)``.

    .. important::
       The DDL that needs to share a transaction (LOCK through DROP
       legacy) is concatenated into a **single multi-statement
       execute_query** call. The driver issues ``COMMIT`` at the end
       of every ``execute_query`` (``sql_driver.py:249/260``), so
       splitting the migration into per-statement calls would commit
       — and thus release the ACCESS EXCLUSIVE lock — after the
       LOCK TABLE statement, letting concurrent writers race the
       rename dance. PG's simple-query protocol holds an implicit
       transaction across all semicolon-separated statements in a
       single execute call, so the lock holds through the final DROP
       (symmetric fix to the one applied to
       :func:`mcpg.rag_telemetry._rag_migrate_native` after the
       gemini critical review on PR #110).
    """
    # Read probes run separately — they're pre-lock and don't need
    # transactional coupling with the migration batch.
    lo, hi, row_count = await _events_data_range(driver)
    version_rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::integer AS ver",
        force_readonly=True,
    )
    pg_version = int(version_rows[0].cells["ver"]) if version_rows else 0

    seq_qualified = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}_id_seq"
    legacy_table = f"{AUDIT_TABLE}_migration_legacy"
    legacy_qualified = f"{AUDIT_SCHEMA}.{legacy_table}"

    statements: list[str] = [
        # 1. Lock — held for the whole batch.
        f"LOCK TABLE {_QUALIFIED} IN ACCESS EXCLUSIVE MODE",
        # 2. Detach the bigserial sequence so the new partitioned
        # table can adopt it.
        f"ALTER SEQUENCE {seq_qualified} OWNED BY NONE",
        # 3. Rename old → events_migration_legacy.
        f"ALTER TABLE {_QUALIFIED} RENAME TO {legacy_table}",
        # 4. Create new partitioned events with the same column
        # shape + (id, occurred_at) composite PK.
        (
            f"CREATE TABLE {_QUALIFIED} ("
            f"  id bigint NOT NULL DEFAULT nextval('{seq_qualified}'), "
            "  occurred_at timestamptz NOT NULL DEFAULT now(), "
            "  tool text NOT NULL, "
            "  arguments jsonb NOT NULL, "
            "  status text NOT NULL, "
            "  error text, "
            "  result jsonb, "
            "  prev_hmac text, "
            "  event_hmac text, "
            "  PRIMARY KEY (id, occurred_at)"
            ") PARTITION BY RANGE (occurred_at)"
        ),
        # 5. Re-tie sequence ownership.
        f"ALTER SEQUENCE {seq_qualified} OWNED BY {_QUALIFIED}.id",
    ]

    # 6. Pre-create partitions covering the data range (monthly) +
    # the trailing/forward week (daily). Empty table → only the
    # trailing window.
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if lo is not None and hi is not None:
        cursor = lo.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cursor <= hi:
            statements.append(_events_native_partition_sql(cursor))
            cursor = (
                cursor.replace(year=cursor.year + 1, month=1)
                if cursor.month == 12
                else cursor.replace(month=cursor.month + 1)
            )
    for offset in range(-_EVENTS_NATIVE_WINDOW_DAYS, _EVENTS_NATIVE_WINDOW_DAYS + 1):
        day = today + timedelta(days=offset)
        if lo is not None and hi is not None:
            month_start = day.replace(day=1)
            if month_start <= hi.replace(day=1):
                # Monthly partition for this month already covers it.
                continue
        statements.append(_events_native_daily_partition_sql(day))

    # 7. Copy rows. ORDER BY id keeps the HMAC chain order intact.
    statements.append(
        f"INSERT INTO {_QUALIFIED} "
        "  (id, occurred_at, tool, arguments, status, error, result, prev_hmac, event_hmac) "
        f"SELECT id, occurred_at, tool, arguments, status, error, result, prev_hmac, event_hmac "
        f"FROM {legacy_qualified} ORDER BY id"
    )

    # 8. LZ4 compression on the wide text columns (PG 14+).
    compression_enabled = False
    if pg_version >= 140000:
        for col in ("arguments", "result", "error"):
            statements.append(f"ALTER TABLE {_QUALIFIED} ALTER COLUMN {col} SET COMPRESSION lz4")
            compression_enabled = True

    # 9. Drop the legacy table — its rows now live in events.
    statements.append(f"DROP TABLE {legacy_qualified}")

    # Send the migration as one batch. PG's simple-query protocol
    # treats the whole semicolon-separated string as one implicit
    # transaction, so the LOCK acquired in statement 1 is held
    # through the DROP at the end. Without this batching the
    # driver's per-execute_query COMMIT would release the lock
    # after the LOCK TABLE call itself.
    batch_sql = ";\n".join(statements)
    await driver.execute_query(batch_sql, force_readonly=False)

    return row_count, compression_enabled, statements


async def _events_migrate_timescaledb(
    driver: SqlDriver,
    *,
    chunk_interval: str,
    compress_after: str,
    retention_days: int | None,
    rls: bool,
    reader_role: str | None,
) -> tuple[int, bool, list[str]]:
    """In-place conversion via ``create_hypertable(migrate_data => TRUE)``.

    No rename dance — TimescaleDB rewrites the table internally,
    chunking by occurred_at. Compression / retention policies are
    added with ``if_not_exists => TRUE`` so re-runs are no-ops.
    """
    statements: list[str] = []
    _, _, row_count = await _events_data_range(driver)

    # TimescaleDB requires the partition column to be part of the
    # PK. Existing events PK is (id) alone — extend it. The DROP +
    # ADD must be atomic; between them the table has no PK and a
    # concurrent record_audit could insert a duplicate. Bundle into
    # one execute_query so the simple-query protocol holds an
    # implicit transaction across both (same fix as the native
    # path; symmetric to PR #110 review).
    pk_rebuild = (
        f"ALTER TABLE {_QUALIFIED} DROP CONSTRAINT IF EXISTS events_pkey;\n"
        f"ALTER TABLE {_QUALIFIED} ADD PRIMARY KEY (id, occurred_at)"
    )
    await driver.execute_query(pk_rebuild, force_readonly=False)
    statements.append(pk_rebuild)

    sql_ht = (
        f"SELECT create_hypertable('{_QUALIFIED}', 'occurred_at', "
        f"chunk_time_interval => INTERVAL '{chunk_interval}', "
        "migrate_data => TRUE, "
        "if_not_exists => TRUE)"
    )
    await driver.execute_query(sql_ht, force_readonly=False)
    statements.append(sql_ht)

    sql_compress = f"ALTER TABLE {_QUALIFIED} SET (timescaledb.compress = TRUE)"
    await driver.execute_query(sql_compress, force_readonly=False)
    statements.append(sql_compress)
    sql_compress_pol = (
        f"SELECT add_compression_policy('{_QUALIFIED}', INTERVAL '{compress_after}', if_not_exists => TRUE)"
    )
    compression_enabled = False
    try:
        await driver.execute_query(sql_compress_pol, force_readonly=False)
        statements.append(sql_compress_pol)
        compression_enabled = True
    except Exception:
        pass

    if retention_days is not None:
        # HMAC chain anchors on the oldest event. Operator opt-in is
        # required (retention_days unset by default); when they DO opt
        # in, surface the gotcha in audit logs but apply the policy.
        sql_retention = (
            f"SELECT add_retention_policy('{_QUALIFIED}', INTERVAL '{retention_days} days', if_not_exists => TRUE)"
        )
        try:
            await driver.execute_query(sql_retention, force_readonly=False)
            statements.append(sql_retention)
        except Exception:
            pass

    return row_count, compression_enabled, statements


async def migrate_audit_events_to_partitioned(
    driver: SqlDriver,
    *,
    env: Any | None = None,
) -> EventsAuditMigrationResult:
    """Retrofit ``mcpg_audit.events`` onto the partitioning stack.

    Idempotent — re-running on an already-partitioned table is a
    near-zero-cost no-op (one catalog probe, then immediate return).
    The migration preserves all rows + HMAC chain columns +
    chain_tip pointer so :func:`mcpg.audit_integrity.verify_audit_chain`
    continues to work.

    Backend ladder (auto-detected, overrideable via
    ``MCPG_AUDIT_EVENTS_BACKEND``):

    * **timescaledb** — `create_hypertable(migrate_data => TRUE)`
      converts in-place. Compression policy via
      ``add_compression_policy``. Retention requires the operator
      to set ``MCPG_AUDIT_EVENTS_RETENTION_DAYS`` (the HMAC chain
      makes wholesale chunk-drops dangerous; opt-in only).
    * **pg_partman** / **native** — rename + create partitioned +
      copy + drop legacy, all under one ACCESS EXCLUSIVE lock.
      Monthly historical partitions + daily trailing/forward
      window. LZ4 column compression on `arguments` / `result` /
      `error` (PG 14+).

    Raises:
        AuditTrailError: When the events table doesn't exist yet
            (caller should run :func:`ensure_audit_table` first) or
            an invalid backend is forced.
    """
    if not await _audit_table_exists(driver):
        raise AuditTrailError(
            "mcpg_audit.events does not exist; call ensure_audit_table first or enable "
            "MCPG_AUDIT_PERSIST so the table is created on first write."
        )

    forced, retention_days, chunk_interval, compress_after, rls, reader_role = _resolve_events_settings(env)

    if await _events_table_is_partitioned(driver):
        # Already partitioned — but RLS + reader-role might be new
        # operator config, so still apply those (they're idempotent).
        statements: list[str] = []
        backend = await _detect_backend(driver, forced=forced)
        await _events_apply_rls(
            driver,
            enabled=rls,
            reader_role=reader_role,
            statements=statements,
        )
        return EventsAuditMigrationResult(
            migrated=False,
            backend=backend,
            rows_copied=0,
            compression_enabled=False,
            retention_days=retention_days,
            rls_enabled=rls,
            reader_role=reader_role,
            setup_sql=tuple(statements),
        )

    backend = await _detect_backend(driver, forced=forced)

    if backend == "timescaledb":
        rows_copied, compression_enabled, statements = await _events_migrate_timescaledb(
            driver,
            chunk_interval=chunk_interval,
            compress_after=compress_after,
            retention_days=retention_days,
            rls=rls,
            reader_role=reader_role,
        )
    else:
        # Native + pg_partman share the rename-create-insert dance.
        rows_copied, compression_enabled, statements = await _events_migrate_native(
            driver,
            rls=rls,
            reader_role=reader_role,
        )
        if backend == "pg_partman":
            sql_partman = (
                f"SELECT partman.create_parent("
                f"  p_parent_table := '{_QUALIFIED}', "
                "  p_control := 'occurred_at', "
                "  p_type := 'range', "
                f"  p_interval := '{chunk_interval}'"
                ")"
            )
            try:
                await driver.execute_query(sql_partman, force_readonly=False)
                statements.append(sql_partman)
            except Exception:
                # create_parent rejects re-registration; treat as
                # idempotent success.
                pass

    await _events_apply_rls(
        driver,
        enabled=rls,
        reader_role=reader_role,
        statements=statements,
    )

    return EventsAuditMigrationResult(
        migrated=True,
        backend=backend,
        rows_copied=rows_copied,
        compression_enabled=compression_enabled,
        retention_days=retention_days,
        rls_enabled=rls,
        reader_role=reader_role,
        setup_sql=tuple(statements),
    )
