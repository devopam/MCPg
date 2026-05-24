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

AUDIT_SCHEMA = "mcpg_audit"
AUDIT_TABLE = "events"
_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"

# Argument keys whose values should never be persisted unredacted.
_SECRET_KEYS = frozenset({"password", "secret", "token", "database_url", "dsn", "conninfo"})
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


def _redact(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``arguments`` with credentials masked."""
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        if key.lower() in _SECRET_KEYS:
            safe[key] = _MASK
        elif isinstance(value, str):
            safe[key] = obfuscate_password(value)
        else:
            safe[key] = value
    return safe


async def ensure_audit_table(driver: SqlDriver) -> None:
    """Create the ``mcpg_audit.events`` schema/table if it doesn't exist.

    The CREATEs are idempotent (``IF NOT EXISTS``) so this is cheap to
    call before every write.
    """
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

    Credentials in ``arguments`` are masked via :func:`_redact` before
    persisting. ``result`` is the structured result a caller wants
    audited; ``None`` is stored as SQL NULL.
    """
    await ensure_audit_table(driver)
    safe_args = _redact(arguments)
    await driver.execute_query(
        f"INSERT INTO {_QUALIFIED} (tool, arguments, status, error, result) VALUES (%s, %s::jsonb, %s, %s, %s::jsonb)",
        params=[
            tool,
            json.dumps(safe_args, default=str),
            status,
            error,
            json.dumps(result, default=str) if result is not None else None,
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
