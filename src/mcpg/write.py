"""Write execution: gated DML (and, in ``write_ddl``, DDL).

Unlike :mod:`mcpg.query`, these statements modify the database, so they run
with a read-write transaction. The vendored read-only allowlist cannot be
used here; instead each statement is parsed with ``pglast`` and required to
be exactly one statement of an expected kind. This blocks statement stacking
(the vendored driver would otherwise happily run ``INSERT ...; DROP ...``).

When ``Settings.audit_persist`` is enabled, every ``run_write`` /
``run_ddl`` call appends a row to ``mcpg_audit.events`` via
:mod:`mcpg.audit_trail`. ``run_ddl`` additionally takes optional
``schema`` and ``table`` hints; when both are supplied, the call
captures a column snapshot before and after the DDL so the caller (and
the audit row) sees a structured "what changed" alongside the result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pglast

from mcpg.audit_trail import SchemaDiffSnapshot, capture_columns, record_audit
from mcpg.sql import SqlDriver

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


@dataclass(frozen=True)
class WriteResult:
    """The outcome of a write.

    ``rows`` holds any rows produced by a ``RETURNING`` clause; a plain write
    without ``RETURNING`` returns no rows. ``row_count`` is ``len(rows)``.
    ``schema_diff`` is populated only by ``run_ddl`` when a schema/table
    hint is supplied and audit persistence is enabled.
    """

    rows: list[dict[str, Any]]
    row_count: int
    schema_diff: SchemaDiffSnapshot | None = field(default=None)


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


async def _persist_audit(
    driver: SqlDriver,
    *,
    tool: str,
    arguments: dict[str, Any],
    result: WriteResult | None,
    error: str | None,
) -> None:
    """Best-effort audit persistence — failures must not mask the real result."""
    result_payload = asdict(result) if result is not None else None
    try:
        await record_audit(
            driver,
            tool=tool,
            arguments=arguments,
            status="error" if error is not None else "ok",
            error=error,
            result=result_payload,
        )
    except Exception:
        # Audit persistence is best-effort; never let it shadow the real
        # write error or fabricate one for a successful write.
        pass


async def run_write(driver: SqlDriver, sql: str, *, audit_persist: bool = False) -> WriteResult:
    """Validate and execute a single INSERT, UPDATE, or DELETE statement.

    The statement runs in a read-write transaction that is committed on
    success. Add a ``RETURNING`` clause to receive affected rows back.

    When ``audit_persist`` is True, one row is appended to
    ``mcpg_audit.events`` for every call (success or error).

    Raises:
        WriteError: If the statement is not a single DML statement, or
            execution fails.
    """
    try:
        result = await _execute(driver, sql, _DML_STATEMENTS, "run_write")
    except WriteError as exc:
        if audit_persist:
            await _persist_audit(driver, tool="run_write", arguments={"sql": sql}, result=None, error=str(exc))
        raise
    if audit_persist:
        await _persist_audit(driver, tool="run_write", arguments={"sql": sql}, result=result, error=None)
    return result


async def run_ddl(
    driver: SqlDriver,
    sql: str,
    *,
    audit_persist: bool = False,
    schema: str | None = None,
    table: str | None = None,
) -> WriteResult:
    """Validate and execute a single DDL statement (CREATE/ALTER/DROP/...).

    Runs in a read-write transaction committed on success. Gated by
    unrestricted access mode *and* the ``MCPG_ALLOW_DDL`` opt-in; see
    :mod:`mcpg.policy`.

    When ``schema`` and ``table`` are both supplied, the call captures
    a column snapshot before and after the DDL and attaches the
    before/after lists to the result as a :class:`SchemaDiffSnapshot`.
    With ``audit_persist=True``, the snapshot also lands in the
    persisted audit row.

    Raises:
        WriteError: If the statement is not a single supported DDL statement,
            or execution fails.
    """
    capture = schema is not None and table is not None
    columns_before: list[dict[str, Any]] = []
    if capture:
        # ``schema``/``table`` are non-None here under ``capture``.
        assert schema is not None and table is not None
        columns_before = await capture_columns(driver, schema, table)

    try:
        result = await _execute(driver, sql, _DDL_STATEMENTS, "run_ddl")
    except WriteError as exc:
        if audit_persist:
            await _persist_audit(
                driver,
                tool="run_ddl",
                arguments={"sql": sql, "schema": schema, "table": table},
                result=None,
                error=str(exc),
            )
        raise

    if capture:
        assert schema is not None and table is not None
        columns_after = await capture_columns(driver, schema, table)
        result = WriteResult(
            rows=result.rows,
            row_count=result.row_count,
            schema_diff=SchemaDiffSnapshot(
                schema=schema,
                table=table,
                columns_before=columns_before,
                columns_after=columns_after,
            ),
        )

    if audit_persist:
        await _persist_audit(
            driver,
            tool="run_ddl",
            arguments={"sql": sql, "schema": schema, "table": table},
            result=result,
            error=None,
        )
    return result
