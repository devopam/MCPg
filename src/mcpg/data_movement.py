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

from mcpg._vendor.sql import SqlDriver
from mcpg.database import Database
from mcpg.query import QueryError, run_select
from mcpg.shell import ShellError, run_pg_binary

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


@dataclass(frozen=True, slots=True)
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
) -> DumpResult:
    """Run ``pg_dump`` against the database in ``database_url`` and capture stdout.

    Credentials are extracted from the URL and passed to ``pg_dump``
    via the libpq env vars (PGHOST/PGUSER/PGPASSWORD/...), never on
    the command line. The dump shape is controlled by ``format`` (only
    ``plain`` returns parseable text; the binary formats land as
    base64-stringified bytes in the result and need a corresponding
    ``pg_restore`` call to consume them).

    Raises:
        ShellError: ``pg_dump`` is not on the allowlist or PATH, the
            URL is unparseable, the format is not supported, or the
            subprocess fails to spawn.
    """
    if format not in _PG_DUMP_FORMATS:
        raise ShellError(f"unsupported pg_dump format {format!r}; expected one of {sorted(_PG_DUMP_FORMATS)}")
    env = _libpq_env_from_url(database_url)
    if "PGDATABASE" not in env:
        raise ShellError("database_url must specify a database name")

    argv = [f"--format={format}"]
    if schema_only:
        argv.append("--schema-only")
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


@dataclass(frozen=True, slots=True)
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


# --- bulk imports (in-process; gated behind WRITE) -----------------------


class ImportDataError(Exception):
    """Raised when an import call is rejected or fails.

    Named ``ImportDataError`` so it does not shadow the builtin
    ``ImportError`` for callers who do ``from mcpg.data_movement
    import *``.
    """


@dataclass(frozen=True, slots=True)
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
