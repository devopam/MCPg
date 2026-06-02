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
from datetime import UTC, datetime
from os import environ
from typing import Any

from mcpg._vendor.sql import SqlDriver, obfuscate_password
from mcpg.audit import is_secret_key
from mcpg.config import _parse_bool

_AUDIT_LOCK: asyncio.Lock | None = None


def _get_audit_lock() -> asyncio.Lock:
    global _AUDIT_LOCK
    if _AUDIT_LOCK is None:
        _AUDIT_LOCK = asyncio.Lock()
    return _AUDIT_LOCK


AUDIT_SCHEMA = "mcpg_audit"
AUDIT_TABLE = "events"

_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"

_MASK = "****"


class AuditTrailError(Exception):
    """Raised when an audit-trail maintenance operation is rejected."""


@dataclass(frozen=True, slots=True)
class PruneResult:
    """The outcome of a :func:`prune_audit_events` call.

    ``deleted`` is the number of rows removed. ``cutoff`` is the
    ISO-8601 timestamp before which events were pruned. ``remaining``
    is the row count left in the table afterwards.
    """

    deleted: int
    cutoff: str
    remaining: int


@dataclass(frozen=True, slots=True)
class AuditTrailEntry:
    """A persisted SQL audit row read back from ``mcpg_audit.events``."""

    id: int
    occurred_at: Any  # naive timestamp - the driver returns datetime
    tool: str
    arguments: dict[str, Any]
    status: str
    error: str | None
    result: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
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
                "error": error,
                "result": safe_result,
            }
            payload_bytes = json.dumps(payload_data, sort_keys=True, default=str).encode("utf-8")

            # Compute HMAC signature
            key_bytes = audit_hmac_key.encode("utf-8")
            data_to_sign = prev_hmac.encode("utf-8") + payload_bytes
            event_hmac = hmac.new(key_bytes, data_to_sign, hashlib.sha256).hexdigest()

        await driver.execute_query(
            f"INSERT INTO {_QUALIFIED} (tool, arguments, status, error, result, occurred_at, prev_hmac, event_hmac) "
            "VALUES (%s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s)",
            params=[
                tool,
                json.dumps(safe_args, default=str),
                status,
                error,
                json.dumps(safe_result, default=str) if safe_result is not None else None,
                now_dt,
                prev_hmac,
                event_hmac,
            ],
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
        return PruneResult(deleted=0, cutoff="", remaining=0)

    rows = await driver.execute_query(
        f"WITH cutoff AS (SELECT now() - make_interval(days => %s) AS ts) "
        f"DELETE FROM {_QUALIFIED} WHERE occurred_at < (SELECT ts FROM cutoff) "
        f"RETURNING (SELECT ts FROM cutoff) AS cutoff_ts",
        params=[older_than_days],
        force_readonly=False,
    )
    deleted = len(rows or [])
    cutoff = ""
    if rows:
        cutoff_val = rows[0].cells.get("cutoff_ts")
        if cutoff_val is not None:
            cutoff = cutoff_val.isoformat() if hasattr(cutoff_val, "isoformat") else str(cutoff_val)

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
