"""Data-movement tools — exports, dumps, restores, and bulk imports.

Read-only half (no subprocess, no new attack surface):

- :func:`export_query`, :func:`export_table` — serialise SELECT
  results to CSV / JSON in-process.

Subprocess-driven half (ADR-0004; gated behind ``MCPG_ALLOW_SHELL``):

- :func:`dump_database` — ``pg_dump``.
- :func:`restore_database` — ``psql`` / ``pg_restore``.

Bulk imports (in-process, no subprocess; gated behind ``WRITE``
capability — unrestricted access mode):

- :func:`import_csv` — ``COPY ... FROM STDIN`` with raw CSV content.
- :func:`import_json` — parametrised ``INSERT`` from a JSON array of
  objects.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from mcpg.database import Database
from mcpg.query import QueryError, run_select
from mcpg.shell import ShellError, SubprocessLimits, run_pg_binary
from mcpg.sql import SqlDriver

# Same identifier allowlist as mcpg.textsearch / mcpg.prisma / mcpg.vector_tuning —
# refuse names that need delimited-identifier quoting, accept plain ones.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

EXPORT_FORMATS = frozenset({"csv", "json"})

# Default ceiling for an export call. Agents wanting more can paginate
# their own LIMIT/OFFSET via ``export_query``.
DEFAULT_EXPORT_LIMIT = 10_000


class ExportError(Exception):
    """Raised when an export call is rejected or fails."""


@dataclass(frozen=True)
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


def _csv_cell(value: Any) -> Any:
    """Coerce a row cell to a CSV-safe value.

    - ``None`` becomes the empty string, the standard CSV NULL marker;
      passing ``None`` straight to ``DictWriter`` would emit the literal
      ``"None"``.
    - ``dict`` / ``list`` (the shape psycopg hands back for ``jsonb`` /
      ``json`` columns) are re-serialised as JSON so the cell holds a
      valid JSON string a downstream consumer can parse. Plain ``str()``
      would produce Python repr — single quotes, ``True`` instead of
      ``true`` — which no JSON reader will accept.
    - Plain scalars (``str``, ``int``, ``float``, ``bool``) pass through.
    - Everything else (datetime, UUID, Decimal, custom types) is
      ``str()``'d so the CSV is always readable, with round-tripping
      left to the consumer.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """Serialise dict rows to CSV with a header row taken from the first row."""
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
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


# --- subprocess-driven half (ADR-0004) ------------------------------------


@dataclass(frozen=True)
class DumpResult:
    """The outcome of a ``pg_dump`` invocation.

    ``content`` is the captured stdout as a UTF-8 string for
    ``format="plain"`` (the default; SQL text). ``stderr_tail`` is the
    last few KiB of stderr — useful for surfacing warnings and errors
    without flooding the result. ``output_truncated`` is ``True`` when
    the dump exceeded ``max_output_bytes``; the caller should re-run
    with a higher cap or dump in narrower chunks.
    """

    exit_code: int
    content: str
    output_bytes: int
    output_truncated: bool
    timed_out: bool
    stderr_tail: str
    binary: str
    argv: list[str]


def _libpq_env_from_url(database_url: str) -> dict[str, str]:
    """Parse a ``postgresql://...`` URL into the libpq ``PG*`` env vars.

    Returns only the env vars implied by the URL — anything the caller
    wants to add (PGOPTIONS, PGSSLMODE) layers on top via the env arg
    to :func:`mcpg.shell.run_pg_binary`. Credentials land in
    ``PGPASSWORD`` (env), never on the command line.
    """
    parsed = urlparse(database_url)
    env: dict[str, str] = {}
    if parsed.hostname:
        env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    if parsed.path and parsed.path != "/":
        env["PGDATABASE"] = parsed.path.lstrip("/")
    return env


_PG_DUMP_FORMATS = frozenset({"plain", "custom", "directory", "tar"})


async def dump_database(
    database_url: str,
    *,
    timeout_sec: int,
    max_output_bytes: int,
    format: str = "plain",
    schema_only: bool = False,
    schemas: list[str] | None = None,
    limits: SubprocessLimits | None = None,
) -> DumpResult:
    """Run ``pg_dump`` against the database in ``database_url`` and capture stdout.

    Credentials are extracted from the URL and passed to ``pg_dump``
    via the libpq env vars (PGHOST/PGUSER/PGPASSWORD/...), never on
    the command line. The dump shape is controlled by ``format`` (only
    ``plain`` returns parseable text; the binary formats land as
    base64-stringified bytes in the result and need a corresponding
    ``pg_restore`` call to consume them). Pass ``schemas`` to scope the
    dump to specific schemas (one ``--schema=NAME`` flag per entry)
    instead of the whole database — useful to re-run with a narrower
    scope, or to sidestep schemas the caller doesn't want captured
    (e.g. an MPP catalog schema like WarehousePG's ``gp_toolkit``).

    Raises:
        ShellError: ``pg_dump`` is not on the allowlist or PATH, the
            URL is unparseable, the format is not supported, a name in
            ``schemas`` is not a valid identifier, or the subprocess
            fails to spawn.
    """
    if format not in _PG_DUMP_FORMATS:
        raise ShellError(f"unsupported pg_dump format {format!r}; expected one of {sorted(_PG_DUMP_FORMATS)}")
    env = _libpq_env_from_url(database_url)
    if "PGDATABASE" not in env:
        raise ShellError("database_url must specify a database name")
    if schemas is not None:
        for name in schemas:
            if not _IDENTIFIER.match(name):
                raise ShellError(f"invalid schema name: {name!r}")

    argv = [f"--format={format}"]
    if schema_only:
        argv.append("--schema-only")
    if schemas is not None:
        argv.extend(f"--schema={name}" for name in schemas)
    # Always pipe to stdout (the default for plain/custom/tar; directory
    # writes to disk and isn't supported in v1).
    if format == "directory":
        raise ShellError("pg_dump format 'directory' writes to disk; not supported in v1")

    result = await run_pg_binary(
        "pg_dump",
        *argv,
        env=env,
        timeout_sec=timeout_sec,
        max_output_bytes=max_output_bytes,
        limits=limits,
    )

    # For plain format the stdout is SQL text; decode as UTF-8. For
    # custom/tar it's a binary archive — return a base64 representation
    # so the result is JSON-transportable.
    if format == "plain":
        content = result.stdout.decode("utf-8", errors="replace")
    else:
        import base64

        content = base64.b64encode(result.stdout).decode("ascii")

    stderr_tail = result.stderr.decode("utf-8", errors="replace")
    if len(stderr_tail) > 4096:
        stderr_tail = stderr_tail[-4096:]

    return DumpResult(
        exit_code=result.exit_code,
        content=content,
        output_bytes=result.output_bytes,
        output_truncated=result.output_truncated,
        timed_out=result.timed_out,
        stderr_tail=stderr_tail,
        binary=result.binary,
        argv=result.argv,
    )


@dataclass(frozen=True)
class RestoreResult:
    """The outcome of a ``pg_restore`` or ``psql`` invocation."""

    exit_code: int
    output_bytes: int
    output_truncated: bool
    timed_out: bool
    stderr_tail: str
    binary: str
    argv: list[str]


_PG_RESTORE_FORMATS = frozenset({"plain", "custom", "tar"})


async def restore_database(
    database_url: str,
    content: str,
    *,
    timeout_sec: int,
    max_output_bytes: int,
    format: str = "plain",
    limits: SubprocessLimits | None = None,
) -> RestoreResult:
    """Restore a dump into the database in ``database_url``.

    ``format='plain'`` pipes ``content`` (SQL text) into ``psql``;
    ``custom`` and ``tar`` base64-decode ``content`` and pipe the bytes
    into ``pg_restore``. ``--single-transaction`` + ``ON_ERROR_STOP``
    are set for psql so a syntax error rolls the whole restore back
    rather than half-applying. Credentials reach the binary via libpq
    env vars (PGPASSWORD), never on the command line.

    Raises:
        ShellError: pg_restore/psql not on the allowlist or PATH, the
            URL is unparseable, the format is unsupported, or the
            subprocess fails to spawn.
    """
    if format not in _PG_RESTORE_FORMATS:
        raise ShellError(f"unsupported restore format {format!r}; expected one of {sorted(_PG_RESTORE_FORMATS)}")
    env = _libpq_env_from_url(database_url)
    if "PGDATABASE" not in env:
        raise ShellError("database_url must specify a database name")

    if format == "plain":
        binary = "psql"
        argv = [
            "--quiet",
            "--single-transaction",
            "--set=ON_ERROR_STOP=on",
            "--file=-",  # read from stdin
        ]
        stdin = content.encode("utf-8")
    else:
        import base64

        binary = "pg_restore"
        argv = [
            # pg_restore needs --dbname or it switches to "convert to SQL
            # script" mode and demands -f. Pass an empty libpq URI so
            # libpq fills user/host/password/dbname from the PG* env
            # vars we already set — credentials stay off argv.
            "--dbname=postgresql:///",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "--exit-on-error",
            f"--format={format}",
        ]
        try:
            stdin = base64.b64decode(content, validate=True)
        except (ValueError, TypeError) as exc:
            raise ShellError(f"content is not valid base64 for {format!r} format: {exc}") from exc

    result = await run_pg_binary(
        binary,
        *argv,
        env=env,
        timeout_sec=timeout_sec,
        max_output_bytes=max_output_bytes,
        stdin=stdin,
        limits=limits,
    )

    stderr_tail = result.stderr.decode("utf-8", errors="replace")
    if len(stderr_tail) > 4096:
        stderr_tail = stderr_tail[-4096:]

    return RestoreResult(
        exit_code=result.exit_code,
        output_bytes=result.output_bytes,
        output_truncated=result.output_truncated,
        timed_out=result.timed_out,
        stderr_tail=stderr_tail,
        binary=result.binary,
        argv=result.argv,
    )


# --- cross-database table copy (subprocess; gated behind MCPG_ALLOW_SHELL) --


@dataclass(frozen=True)
class CopyTableResult:
    """The outcome of a cross-database ``copy_table_between_databases`` call.

    Both legs (``pg_dump`` on the source, ``pg_restore`` on the dest) are
    recorded. ``timed_out`` is true when either leg hit its time limit.
    ``schema_copied`` / ``data_copied`` echo the input flags so a result
    inspected without its call context is self-describing.
    """

    schema: str
    table: str
    schema_copied: bool
    data_copied: bool
    dump_exit_code: int
    restore_exit_code: int
    dump_output_bytes: int
    restore_output_bytes: int
    dump_stderr_tail: str
    restore_stderr_tail: str
    dump_argv: list[str]
    restore_argv: list[str]
    timed_out: bool


async def copy_table_between_databases(
    source_url: str,
    dest_url: str,
    schema: str,
    table: str,
    *,
    include_schema: bool,
    include_data: bool,
    timeout_sec: int,
    max_output_bytes: int,
    limits: SubprocessLimits | None = None,
) -> CopyTableResult:
    """Copy ``schema.table`` from ``source_url`` into ``dest_url``.

    Runs ``pg_dump --format=custom --table=schema.table`` against the
    source, captures the binary archive in memory, then pipes it into
    ``pg_restore --format=custom --single-transaction --exit-on-error``
    against the destination. Credentials reach both binaries via libpq
    env vars (``PGPASSWORD`` etc.), never on the command line.

    Args:
        source_url: ``postgresql://...`` URL of the source database;
            must specify a database name.
        dest_url: ``postgresql://...`` URL of the destination database;
            must specify a database name.
        schema: Schema name; must be a plain identifier.
        table: Table name; must be a plain identifier.
        include_schema: Copy the table's ``CREATE TABLE`` (schema). The
            target table must NOT exist when this is true and
            ``include_data`` is false (pg_restore --single-transaction
            aborts on duplicate-table errors).
        include_data: Copy the table's rows. At least one of
            ``include_schema`` / ``include_data`` must be true.
        timeout_sec: Hard wall-clock limit on EACH leg of the copy
            (dump and restore are timed separately).
        max_output_bytes: Cap on the captured pg_dump archive. Exceeding
            the cap raises :class:`ShellError` BEFORE pg_restore runs —
            we never feed a truncated custom-format archive to
            pg_restore, since the result would be a half-restored
            table that's hard to roll back.

    Raises:
        ShellError: When inputs fail validation, the pg_dump output
            exceeds ``max_output_bytes``, or either subprocess fails
            to spawn. A non-zero exit from either binary is NOT raised
            here — it's returned in the result so the caller can see
            both legs' stderr_tail in one place.
    """
    if not include_schema and not include_data:
        raise ShellError("copy_table_between_databases requires include_schema or include_data (or both)")
    _check_identifier_for_copy(schema, "schema")
    _check_identifier_for_copy(table, "table")

    source_env = _libpq_env_from_url(source_url)
    if "PGDATABASE" not in source_env:
        raise ShellError("source_url must specify a database name")
    dest_env = _libpq_env_from_url(dest_url)
    if "PGDATABASE" not in dest_env:
        raise ShellError("dest_url must specify a database name")

    # pg_dump's --table pattern accepts a literal schema-qualified name
    # since we've already validated both halves against the plain-
    # identifier allowlist (no wildcard chars).
    dump_argv = ["--format=custom", f"--table={schema}.{table}"]
    if not include_data:
        dump_argv.append("--schema-only")
    if not include_schema:
        dump_argv.append("--data-only")

    dump = await run_pg_binary(
        "pg_dump",
        *dump_argv,
        env=source_env,
        timeout_sec=timeout_sec,
        max_output_bytes=max_output_bytes,
        limits=limits,
    )
    if dump.output_truncated:
        raise ShellError(
            f"pg_dump output exceeded max_output_bytes ({max_output_bytes}); refusing to restore a truncated archive"
        )
    # Refuse to restore if the dump itself failed — pg_restore on an
    # incomplete custom archive will either error obscurely or, worse,
    # silently restore a partial table.
    if dump.exit_code != 0 or dump.timed_out:
        return CopyTableResult(
            schema=schema,
            table=table,
            schema_copied=include_schema,
            data_copied=include_data,
            dump_exit_code=dump.exit_code,
            restore_exit_code=-1,
            dump_output_bytes=dump.output_bytes,
            restore_output_bytes=0,
            dump_stderr_tail=_tail(dump.stderr),
            restore_stderr_tail="",
            dump_argv=dump.argv,
            restore_argv=[],
            timed_out=dump.timed_out,
        )

    # pg_restore requires --dbname even with PGDATABASE set: without -d
    # it switches to "convert to SQL script" mode and demands -f. Pass
    # an empty libpq URI as the dbname; libpq fills user/host/password/
    # dbname from the PG* env vars we already set, so credentials still
    # never appear on argv.
    restore_argv = [
        "--format=custom",
        "--dbname=postgresql:///",
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
    ]
    if not include_schema:
        restore_argv.append("--data-only")
    if not include_data:
        restore_argv.append("--schema-only")
    restore = await run_pg_binary(
        "pg_restore",
        *restore_argv,
        env=dest_env,
        timeout_sec=timeout_sec,
        max_output_bytes=max_output_bytes,
        stdin=dump.stdout,
        limits=limits,
    )

    return CopyTableResult(
        schema=schema,
        table=table,
        schema_copied=include_schema,
        data_copied=include_data,
        dump_exit_code=dump.exit_code,
        restore_exit_code=restore.exit_code,
        dump_output_bytes=dump.output_bytes,
        restore_output_bytes=restore.output_bytes,
        dump_stderr_tail=_tail(dump.stderr),
        restore_stderr_tail=_tail(restore.stderr),
        dump_argv=dump.argv,
        restore_argv=restore.argv,
        timed_out=dump.timed_out or restore.timed_out,
    )


def _check_identifier_for_copy(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise ShellError(f"invalid {kind} name: {name!r}")


def _tail(buf: bytes, *, max_bytes: int = 4096) -> str:
    text = buf.decode("utf-8", errors="replace")
    return text if len(text) <= max_bytes else text[-max_bytes:]


# --- bulk imports (in-process; gated behind WRITE) -----------------------
class ImportDataError(Exception):
    """Raised when an import call is rejected or fails.

    Named ``ImportDataError`` so it does not shadow the builtin
    ``ImportError`` for callers who do ``from mcpg.data_movement
    import *``.
    """


@dataclass(frozen=True)
class ImportResult:
    """The outcome of an import call.

    ``rows_imported`` is the row count the server reports — useful for
    confirming the agent wrote what it expected. ``format`` echoes the
    input format so a result inspected without its call context is
    self-describing.
    """

    schema: str
    table: str
    format: str
    rows_imported: int


def _build_copy_sql(schema: str, table: str, columns: list[str] | None, *, header: bool, delimiter: str) -> str:
    """Compose a ``COPY ... FROM STDIN`` statement with safe identifiers.

    The schema, table, and (optional) column names are validated against
    the plain-identifier allowlist before this is called; delimited
    quoting still applies so reserved words don't break the statement.
    The delimiter is restricted to a single non-newline character so it
    can't terminate the COPY options list early.
    """
    column_clause = ""
    if columns:
        joined = ", ".join(f'"{col}"' for col in columns)
        column_clause = f" ({joined})"
    options = [
        "FORMAT csv",
        f"HEADER {'true' if header else 'false'}",
        f"DELIMITER '{delimiter}'",
    ]
    return f'COPY "{schema}"."{table}"{column_clause} FROM STDIN WITH ({", ".join(options)})'


async def import_csv(
    database: Database,
    schema: str,
    table: str,
    content: str,
    *,
    header: bool = True,
    delimiter: str = ",",
    columns: list[str] | None = None,
) -> ImportResult:
    """Bulk-load CSV ``content`` into ``schema.table`` via ``COPY FROM STDIN``.

    The CSV text is sent verbatim; the caller is responsible for its
    correctness (matching column count, proper quoting). ``header=True``
    tells PostgreSQL to skip the first line.

    Args:
        database: The connected MCPg ``Database``.
        schema: Target schema; must be a plain identifier.
        table: Target table; must be a plain identifier.
        content: CSV text to load. Encoded as UTF-8 on the wire.
        header: When true, the first CSV row is treated as a header
            and skipped by COPY.
        delimiter: One-character field separator. Newlines and quote
            characters are rejected to keep the COPY options safe.
        columns: Optional explicit column list. When supplied, each
            name must be a plain identifier; rows are loaded into the
            named columns in order, with any unlisted columns taking
            their default. When omitted, COPY uses the table's
            declared column order.

    Raises:
        ImportError: When an identifier fails validation, the delimiter
            is illegal, or the underlying COPY fails.
    """
    _check_identifier_for_import(schema, "schema")
    _check_identifier_for_import(table, "table")
    if columns is not None:
        if not columns:
            raise ImportDataError("columns, if supplied, must not be empty")
        for col in columns:
            _check_identifier_for_import(col, "column")
    if len(delimiter) != 1 or delimiter in "\n\r\"'":
        raise ImportDataError(f"invalid delimiter {delimiter!r}; must be a single non-newline, non-quote character")

    copy_sql = _build_copy_sql(schema, table, columns, header=header, delimiter=delimiter)
    try:
        rowcount = await database.copy_from_stdin(copy_sql, content.encode("utf-8"))
    except Exception as exc:  # psycopg / DatabaseError / OS error
        raise ImportDataError(f"COPY failed: {exc}") from exc
    return ImportResult(schema=schema, table=table, format="csv", rows_imported=max(rowcount, 0))


async def import_json(
    database: Database,
    schema: str,
    table: str,
    content: str,
    *,
    columns: list[str] | None = None,
) -> ImportResult:
    """Bulk-load a JSON array of objects into ``schema.table``.

    Parses ``content`` as a JSON array, derives the column list from
    ``columns`` (when supplied) or from the keys of the first row
    (otherwise), then executes a parametrised
    ``INSERT INTO "schema"."table" (col, ...) VALUES (%s, ...)`` once
    per row via ``executemany``. Values are bound — never spliced into
    SQL — so they cannot inject statements.

    Missing keys in a later row are bound as ``NULL``; nested
    ``dict`` / ``list`` values are JSON-serialised so they round-trip
    into a ``jsonb`` column.

    Raises:
        ImportError: When ``content`` is not a JSON array of objects,
            an identifier fails validation, or the INSERT fails.
    """
    _check_identifier_for_import(schema, "schema")
    _check_identifier_for_import(table, "table")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ImportDataError(f"content is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ImportDataError("content must be a JSON array of objects")
    if not parsed:
        return ImportResult(schema=schema, table=table, format="json", rows_imported=0)
    if not all(isinstance(row, dict) for row in parsed):
        raise ImportDataError("every row in the JSON array must be an object")

    if columns is None:
        columns = list(parsed[0].keys())
    if not columns:
        raise ImportDataError("could not derive a column list from the JSON rows")
    for col in columns:
        _check_identifier_for_import(col, "column")

    placeholders = ", ".join(["%s"] * len(columns))
    column_clause = ", ".join(f'"{col}"' for col in columns)
    sql = f'INSERT INTO "{schema}"."{table}" ({column_clause}) VALUES ({placeholders})'
    params_seq = [tuple(_json_cell(row.get(col)) for col in columns) for row in parsed]
    try:
        rowcount = await database.execute_many(sql, params_seq)
    except Exception as exc:
        raise ImportDataError(f"INSERT failed: {exc}") from exc
    # psycopg's executemany sometimes reports -1 for the rowcount on certain
    # backends. Fall back to the number of rows we sent.
    if rowcount < 0:
        rowcount = len(params_seq)
    return ImportResult(schema=schema, table=table, format="json", rows_imported=rowcount)


def _check_identifier_for_import(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise ImportDataError(f"invalid {kind} name: {name!r}")


def _json_cell(value: Any) -> Any:
    """Coerce a JSON cell to a psycopg-friendly bound value.

    Nested ``dict`` / ``list`` become JSON strings so they survive
    insertion into a ``jsonb`` column without a round-trip through
    Python ``repr``. Scalars pass through; the psycopg adapter handles
    them natively.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


# --- bulk vector import (in-process; gated behind WRITE) ------------------


_VECTOR_FORMATS = frozenset({"json", "csv"})


async def _vector_column_dimension(driver: SqlDriver, schema: str, table: str, column: str) -> int | None:
    """Return the declared dimension of a ``vector(N)`` column, or ``None``.

    A return of ``None`` means either the column doesn't exist or it
    isn't a pgvector ``vector`` type (anything else is the caller's
    problem to validate). pgvector encodes ``N`` in ``pg_attribute``'s
    ``atttypmod`` directly when present.
    """
    rows = await driver.execute_query(
        "SELECT t.typname AS type_name, a.atttypmod AS type_mod "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s AND a.attnum > 0",
        params=[schema, table, column],
        force_readonly=True,
    )
    if not rows:
        return None
    cell = rows[0].cells
    if cell.get("type_name") != "vector":
        return None
    type_mod = cell.get("type_mod")
    return int(type_mod) if isinstance(type_mod, int) and type_mod > 0 else None


def _coerce_vector(value: Any, *, dimension: int, row_idx: int) -> str:
    """Validate one row's embedding and return the pgvector text literal.

    ``row_idx`` is 0-based for code; error messages render it as 1-based
    (`row N`) so the number lines up with how a human counts rows in a
    CSV / JSON payload — gemini noted this caused real debugging pain.
    """
    row_num = row_idx + 1
    if value is None:
        raise ImportDataError(f"row {row_num} has a NULL embedding; import_vectors requires every row to carry a value")
    if isinstance(value, str):
        # CSV cells arrive as strings — accept either a bracketed pgvector
        # literal ("[0.1,0.2]") or a comma-separated list ("0.1,0.2").
        stripped = value.strip().lstrip("[").rstrip("]").strip()
        if not stripped:
            raise ImportDataError(f"row {row_num} has an empty embedding")
        try:
            floats = [float(piece.strip()) for piece in stripped.split(",")]
        except ValueError as exc:
            raise ImportDataError(f"row {row_num} has a non-numeric value in its embedding: {exc}") from exc
    elif isinstance(value, (list, tuple)):
        try:
            floats = [float(v) for v in value]
        except (TypeError, ValueError) as exc:
            raise ImportDataError(f"row {row_num} has a non-numeric value in its embedding: {exc}") from exc
    else:
        raise ImportDataError(
            f"row {row_num} has an embedding of unsupported type {type(value).__name__}; "
            "expected a list/tuple of numbers or a pgvector text literal"
        )
    if len(floats) != dimension:
        raise ImportDataError(
            f"row {row_num} embedding has dimension {len(floats)}, but the column expects {dimension}"
        )
    # pgvector accepts a bracketed text literal cast to ``vector``.
    return "[" + ",".join(str(v) for v in floats) + "]"


def _parse_vector_payload(
    content: str,
    *,
    format: str,
    embedding_column: str,
    id_column: str | None,
) -> list[tuple[Any, Any]]:
    """Decode a JSON-array or CSV payload into ``(id, embedding)`` pairs.

    For CSV the first row must be a header; the embedding column is
    required, the id column is optional. The embedding cell can be a
    bracketed pgvector literal or a comma-separated list of numbers.
    For JSON the input must be an array of objects whose embedding
    field is a list of numbers (or a literal string).
    """
    if format == "json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ImportDataError(f"content is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ImportDataError("content must be a JSON array of objects")
        if parsed and not all(isinstance(row, dict) for row in parsed):
            raise ImportDataError("every row in the JSON array must be an object")
        pairs: list[tuple[Any, Any]] = []
        for idx, row in enumerate(parsed):
            if embedding_column not in row:
                raise ImportDataError(f"row {idx + 1} is missing the embedding column {embedding_column!r}")
            ident = row.get(id_column) if id_column else None
            pairs.append((ident, row[embedding_column]))
        return pairs

    # CSV — parse with the stdlib reader so quoted bracketed literals
    # round-trip correctly (e.g. ``id,vec\n1,"[0.1,0.2]"``).
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or embedding_column not in reader.fieldnames:
        raise ImportDataError(f"CSV header is missing the embedding column {embedding_column!r}")
    if id_column and id_column not in reader.fieldnames:
        raise ImportDataError(f"CSV header is missing the id column {id_column!r}")
    return [(row.get(id_column) if id_column else None, row[embedding_column]) for row in reader]


async def import_vectors(
    database: Database,
    schema: str,
    table: str,
    embedding_column: str,
    content: str,
    *,
    format: str = "json",
    id_column: str | None = None,
) -> ImportResult:
    """Bulk-load embeddings into ``schema.table.embedding_column``.

    Specialised sibling of :func:`import_csv` / :func:`import_json` for
    pgvector columns. The target column's declared ``vector(N)``
    dimension is read from the catalog at call time and every row in
    ``content`` is validated against it BEFORE any INSERT runs — a
    dimension mismatch on row 1000 fails the whole call rather than
    leaving 999 partial inserts behind.

    Args:
        embedding_column: The pgvector column receiving the values.
        content: JSON array of objects (default) or CSV text. For JSON
            the embedding field is a list of numbers (or a pgvector
            text literal). For CSV the header must include
            ``embedding_column`` (and ``id_column`` if supplied); cells
            can be bracketed literals or comma-separated numbers.
        format: ``"json"`` (default) or ``"csv"``.
        id_column: When set, the parallel column receiving each row's
            identifier from the payload. Both columns get a parametrised
            ``INSERT (id, embedding) VALUES (%s, %s::vector)``.

    Raises:
        ImportDataError: When the format is unknown, an identifier is
            invalid, the embedding column isn't a pgvector ``vector(N)``
            (and so dimension validation can't happen), the payload is
            malformed, or any row's dimension doesn't match the column.
    """
    _check_identifier_for_import(schema, "schema")
    _check_identifier_for_import(table, "table")
    _check_identifier_for_import(embedding_column, "column")
    if id_column is not None:
        _check_identifier_for_import(id_column, "column")
    fmt = format.strip().lower()
    if fmt not in _VECTOR_FORMATS:
        raise ImportDataError(f"unsupported format {format!r}; expected one of {sorted(_VECTOR_FORMATS)}")

    # Discover the column's declared dimension up-front so every row in
    # the payload can be validated before we open a transaction.
    driver = database.driver()
    dimension = await _vector_column_dimension(driver, schema, table, embedding_column)
    if dimension is None:
        raise ImportDataError(
            f"{schema}.{table}.{embedding_column} is not a pgvector vector(N) column "
            "(either the column doesn't exist or it isn't typed vector); "
            "import_vectors validates dimension against the column's declared N"
        )

    pairs = _parse_vector_payload(content, format=fmt, embedding_column=embedding_column, id_column=id_column)
    if not pairs:
        return ImportResult(schema=schema, table=table, format=fmt, rows_imported=0)

    literals: list[tuple[Any, ...]] = []
    for idx, (ident, raw) in enumerate(pairs):
        literal = _coerce_vector(raw, dimension=dimension, row_idx=idx)
        literals.append((ident, literal) if id_column else (literal,))

    if id_column:
        sql = f'INSERT INTO "{schema}"."{table}" ("{id_column}", "{embedding_column}") VALUES (%s, %s::vector)'
    else:
        sql = f'INSERT INTO "{schema}"."{table}" ("{embedding_column}") VALUES (%s::vector)'

    try:
        rowcount = await database.execute_many(sql, literals)
    except Exception as exc:
        raise ImportDataError(f"INSERT failed: {exc}") from exc
    if rowcount < 0:
        rowcount = len(literals)
    return ImportResult(schema=schema, table=table, format=fmt, rows_imported=rowcount)
