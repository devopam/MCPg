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

import json
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SqlDriver, obfuscate_password
from mcpg.audit import _is_secret_key

AUDIT_SCHEMA = "mcpg_audit"
AUDIT_TABLE = "events"
_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"

_MASK = "****"


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
        return {k: _MASK if isinstance(k, str) and _is_secret_key(k) else _redact(v) for k, v in value.items()}
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
        "  result jsonb"
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
    await ensure_audit_table(driver)
    safe_args = _redact(arguments)
    safe_result = _redact(result) if result is not None else None
    await driver.execute_query(
        f"INSERT INTO {_QUALIFIED} (tool, arguments, status, error, result) VALUES (%s, %s::jsonb, %s, %s, %s::jsonb)",
        params=[
            tool,
            json.dumps(safe_args, default=str),
            status,
            error,
            json.dumps(safe_result, default=str) if safe_result is not None else None,
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
