"""Maintenance operations: gated VACUUM and ANALYZE.

``VACUUM`` cannot run inside a transaction block, so maintenance runs on an
autocommit connection (:meth:`Database.run_unmanaged`) rather than through
the transactional ``SqlDriver``. The target table is named, not parameterised
— PostgreSQL takes an identifier here — so the schema and table are quoted
defensively before they reach SQL.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg.database import Database

# Accepted operation -> the SQL command it maps to.
_OPERATIONS = {
    "vacuum": "VACUUM",
    "analyze": "ANALYZE",
    "vacuum_analyze": "VACUUM (ANALYZE)",
}


class MaintenanceError(Exception):
    """Raised when a maintenance request is rejected or fails."""


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    """The outcome of a maintenance run."""

    operation: str
    target: str


def _quote_identifier(name: str) -> str:
    """Quote a SQL identifier, escaping embedded double-quotes."""
    if not name or "\x00" in name:
        raise MaintenanceError(f"invalid identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


async def run_maintenance(database: Database, operation: str, schema: str, table: str) -> MaintenanceResult:
    """Run ``VACUUM`` or ``ANALYZE`` against one table.

    Args:
        database: The connected database to run maintenance on.
        operation: ``vacuum``, ``analyze``, or ``vacuum_analyze``.
        schema: The target table's schema.
        table: The target table.

    Raises:
        MaintenanceError: If the operation is unknown, an identifier is
            invalid, or the command fails.
    """
    command = _OPERATIONS.get(operation)
    if command is None:
        expected = ", ".join(sorted(_OPERATIONS))
        raise MaintenanceError(f"unknown operation {operation!r}; expected one of {expected}")
    target = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
    label = f"{schema}.{table}"
    try:
        await database.run_unmanaged(f"{command} {target}")
    except Exception as exc:
        raise MaintenanceError(f"{operation} on {label} failed: {exc}") from exc
    return MaintenanceResult(operation=operation, target=label)
