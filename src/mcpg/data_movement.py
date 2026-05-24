"""Data-movement tools — in-process export to CSV and JSON.

This module covers the read-only half of Batch D (Phase 24): two tools
that serialise SQL query results or full tables to a string an agent
can inspect or write to a file.

The subprocess-driven half (``dump_database``, ``restore_database``,
``copy_table_between_databases``) follows ADR-0004 and lives in a
separate PR. Imports (``import_csv`` / ``import_json``) need
``COPY ... FROM STDIN`` plumbing that the vendored driver doesn't
expose yet; they're tracked as Phase 24c.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.query import QueryError, run_select

# Same identifier allowlist as mcpg.textsearch / mcpg.prisma / mcpg.vector_tuning —
# refuse names that need delimited-identifier quoting, accept plain ones.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

EXPORT_FORMATS = frozenset({"csv", "json"})

# Default ceiling for an export call. Agents wanting more can paginate
# their own LIMIT/OFFSET via ``export_query``.
DEFAULT_EXPORT_LIMIT = 10_000


class ExportError(Exception):
    """Raised when an export call is rejected or fails."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    """The outcome of an export call.

    ``content`` holds the serialised rows. ``truncated`` is ``True`` when
    the underlying query produced more rows than the requested ``limit``;
    the caller should re-export with a higher limit or paginate.
    """

    format: str
    content: str
    row_count: int
    truncated: bool


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise ExportError(f"invalid {kind} name: {name!r}")


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """Serialise dict rows to CSV with a header row taken from the first row."""
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        # Coerce any non-trivial values (datetimes, UUIDs, JSON dicts)
        # to strings so the CSV is round-trippable. Plain scalars pass
        # through unchanged so they survive in a SQL-typed reader.
        writer.writerow(
            {
                key: value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
                for key, value in row.items()
            }
        )
    return buffer.getvalue()


def _rows_to_json(rows: list[dict[str, Any]]) -> str:
    """Serialise dict rows to a JSON array, with non-JSON values stringified."""
    # ``default=str`` covers datetime, UUID, Decimal, and any custom type
    # the catalog hands us back — anything that isn't JSON-native becomes
    # its ``str()`` form. Round-tripping is the consumer's responsibility.
    return json.dumps(rows, default=str)


async def export_query(
    driver: SqlDriver,
    sql: str,
    *,
    format: str = "csv",
    limit: int = DEFAULT_EXPORT_LIMIT,
) -> ExportResult:
    """Run a read-only SQL query and serialise its rows.

    Reuses :func:`mcpg.query.run_select`, so the same SQL safety checks
    apply: the statement must parse as read-only via the vendored
    ``SafeSqlDriver`` allowlist. ``limit`` caps the row count; a query
    producing more rows yields ``truncated=True``.
    """
    if format not in EXPORT_FORMATS:
        raise ExportError(f"unsupported export format {format!r}; expected one of {sorted(EXPORT_FORMATS)}")
    if limit < 1:
        raise ExportError("limit must be at least 1")
    try:
        result = await run_select(driver, sql, max_rows=limit)
    except QueryError as exc:
        raise ExportError(str(exc)) from exc

    content = _rows_to_csv(result.rows) if format == "csv" else _rows_to_json(result.rows)
    return ExportResult(format=format, content=content, row_count=result.row_count, truncated=result.truncated)


async def export_table(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    format: str = "csv",
    limit: int = DEFAULT_EXPORT_LIMIT,
) -> ExportResult:
    """Serialise every row in ``schema.table`` (up to ``limit``).

    Schema and table names must match the plain identifier pattern —
    anything that requires delimited-identifier quoting is rejected.
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    sql = f'SELECT * FROM "{schema}"."{table}"'
    return await export_query(driver, sql, format=format, limit=limit)
