"""Write execution: gated DML (and, in ``write_ddl``, DDL).

Unlike :mod:`mcpg.query`, these statements modify the database, so they run
with a read-write transaction. The vendored read-only allowlist cannot be
used here; instead each statement is parsed with ``pglast`` and required to
be exactly one statement of an expected kind. This blocks statement stacking
(the vendored driver would otherwise happily run ``INSERT ...; DROP ...``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pglast

from mcpg._vendor.sql import SqlDriver

# pglast statement node names accepted by run_write.
_DML_STATEMENTS = frozenset({"InsertStmt", "UpdateStmt", "DeleteStmt"})

# pglast statement node names accepted by run_ddl. Common structural DDL;
# extend as needed (exotic DDL is intentionally not yet supported).
_DDL_STATEMENTS = frozenset(
    {
        "CreateStmt",  # CREATE TABLE
        "CreateTableAsStmt",  # CREATE TABLE AS
        "CreateSchemaStmt",  # CREATE SCHEMA
        "AlterTableStmt",  # ALTER TABLE
        "DropStmt",  # DROP TABLE / INDEX / VIEW / SCHEMA / ...
        "IndexStmt",  # CREATE INDEX
        "RenameStmt",  # ALTER ... RENAME
        "TruncateStmt",  # TRUNCATE
        "ViewStmt",  # CREATE VIEW
        "CreateSeqStmt",  # CREATE SEQUENCE
        "AlterSeqStmt",  # ALTER SEQUENCE
        "CommentStmt",  # COMMENT ON
    }
)


class WriteError(Exception):
    """Raised when a write is rejected or fails to execute."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The outcome of a write.

    ``rows`` holds any rows produced by a ``RETURNING`` clause; a plain write
    without ``RETURNING`` returns no rows. ``row_count`` is ``len(rows)``.
    """

    rows: list[dict[str, Any]]
    row_count: int


def _parse_single_statement(sql: str) -> object:
    """Parse ``sql`` and return its single statement node, or raise."""
    try:
        statements = pglast.parse_sql(sql)
    except Exception as exc:
        raise WriteError(f"could not parse SQL: {exc}") from exc
    if len(statements) != 1:
        raise WriteError(f"exactly one statement is required (got {len(statements)})")
    return statements[0].stmt


def _validate(sql: str, allowed: frozenset[str], tool: str) -> None:
    """Require ``sql`` to be a single statement of an allowed kind."""
    node = _parse_single_statement(sql)
    name = type(node).__name__
    if name not in allowed:
        raise WriteError(f"{tool} does not accept {name} statements")


async def _execute(driver: SqlDriver, sql: str, allowed: frozenset[str], tool: str) -> WriteResult:
    """Validate ``sql`` against ``allowed`` and run it read-write."""
    _validate(sql, allowed, tool)
    try:
        rows = await driver.execute_query(sql, force_readonly=False)
    except Exception as exc:
        raise WriteError(str(exc)) from exc

    result_rows = [dict(row.cells) for row in rows or []]
    return WriteResult(rows=result_rows, row_count=len(result_rows))


async def run_write(driver: SqlDriver, sql: str) -> WriteResult:
    """Validate and execute a single INSERT, UPDATE, or DELETE statement.

    The statement runs in a read-write transaction that is committed on
    success. Add a ``RETURNING`` clause to receive affected rows back.

    Raises:
        WriteError: If the statement is not a single DML statement, or
            execution fails.
    """
    return await _execute(driver, sql, _DML_STATEMENTS, "run_write")


async def run_ddl(driver: SqlDriver, sql: str) -> WriteResult:
    """Validate and execute a single DDL statement (CREATE/ALTER/DROP/...).

    Runs in a read-write transaction committed on success. This is gated to
    unrestricted access mode *and* the ``MCPG_ALLOW_DDL`` opt-in; see
    :mod:`mcpg.policy`.

    Raises:
        WriteError: If the statement is not a single supported DDL statement,
            or execution fails.
    """
    return await _execute(driver, sql, _DDL_STATEMENTS, "run_ddl")
