"""``REPACK`` — PG 19 in-server online table rebuild.

PG 19 introduces an in-server ``REPACK`` SQL command that rebuilds a
relation with much lower operational overhead than the long-standing
``pg_repack`` extension shell-out. The ``CONCURRENTLY`` variant performs
the rebuild without blocking writers — the headline operational win
("online table rebuild without pg_repack — one tool, one transaction"
on the PG 19 release blog).

This module provides a thin, safe wrapper:

* ``get_repack_status`` — version probe; never raises. Reports whether
  the in-server command is usable on this server.
* ``repack_table`` — runs ``REPACK <relation> [CONCURRENTLY]``. The
  command cannot run inside a transaction block (same constraint as
  ``VACUUM``), so it dispatches through :meth:`Database.run_unmanaged`
  on an autocommit connection.

Backward compatibility
----------------------
This module does **not** remove or replace the existing pg_repack
shell-out path (``pg_repack`` is on the safe-SQL allowlist for
operators who still rely on it via ``run_ddl``). PG ≤ 18 deployments
continue with pg_repack; PG 19 deployments can opt into the in-server
command. See ``docs/plans/pg19-readiness.md`` for the no-deprecation
policy.

Security posture
----------------
* PG 19+ version gate via ``server_version_num >= 190000``. Reads
  degrade to ``available=False`` with a useful diagnostic; writes raise
  a descriptive ``RepackError``.
* Identifier validation on schema and table — the relation name is
  composed into a SQL identifier slot that cannot be parameter-bound.
  We use the same ``_quote_identifier`` helper as ``mcpg.maintenance``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.database import Database

# PG 19 ships REPACK; older versions don't recognise the keyword.
_MIN_REPACK_VERSION = 190000


class RepackError(Exception):
    """Raised when a REPACK request is rejected or fails."""


@dataclass(frozen=True, slots=True)
class RepackStatus:
    """Reports whether ``REPACK`` is usable on this server.

    ``available`` is True when ``server_version_num`` >= 190000. ``detail``
    is a human-readable explanation suitable for surfacing to an LLM
    agent — when the answer is "not available" it points at the
    pg_repack-shell-out fallback path.
    """

    available: bool
    server_version_num: int
    server_version: str
    detail: str


@dataclass(frozen=True, slots=True)
class RepackResult:
    """The outcome of a ``REPACK`` run.

    ``repack_sql`` carries the rendered DDL that actually executed —
    mirrors the ``maintenance_sql`` / ``create_sql`` convention in
    `mcpg.maintenance` / `mcpg.indexing` so audit / change-review
    callers get a consistent shape across every DDL-emitting tool.
    """

    schema: str
    table: str
    concurrently: bool
    repack_sql: str


def _quote_identifier(name: str) -> str:
    """Quote a SQL identifier, escaping embedded double-quotes."""
    if not name or "\x00" in name:
        raise RepackError(f"invalid identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


async def _server_version(driver: SqlDriver) -> tuple[int, str]:
    """Return ``(server_version_num, server_version)`` in one round trip."""
    rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::int AS ver_num, current_setting('server_version') AS ver",
        force_readonly=True,
    )
    if not rows:
        return 0, ""
    cells = rows[0].cells
    return int(cells.get("ver_num") or 0), str(cells.get("ver") or "")


async def get_repack_status(driver: SqlDriver) -> RepackStatus:
    """Report whether the in-server ``REPACK`` command is usable.

    Read-only; never raises. On PG < 19 returns ``available=False`` with
    a diagnostic pointing at the long-standing pg_repack shell-out path
    so the agent can fall back without operator intervention.
    """
    ver_num, ver = await _server_version(driver)
    available = ver_num >= _MIN_REPACK_VERSION
    detail = (
        "In-server REPACK is available — use repack_table() with concurrently=True for online rebuild."
        if available
        else (
            "In-server REPACK requires PostgreSQL 19 or newer; this server is older. "
            "Fall back to the pg_repack extension shell-out path (run as an unrestricted-mode "
            "operator via run_ddl with the pg_repack extension installed)."
        )
    )
    return RepackStatus(
        available=available,
        server_version_num=ver_num,
        server_version=ver,
        detail=detail,
    )


async def repack_table(
    database: Database,
    *,
    schema: str,
    table: str,
    concurrently: bool = True,
) -> RepackResult:
    """Rebuild a table using PG 19's in-server ``REPACK`` command.

    Defaults to ``CONCURRENTLY`` because the non-blocking variant is the
    common operational choice — operators who explicitly want the
    blocking variant set ``concurrently=False``.

    Cannot run inside a transaction block (same as ``VACUUM``), so it
    dispatches through :meth:`Database.run_unmanaged` on an autocommit
    connection.

    Requires PG 19+. Raises :class:`RepackError` on older versions; the
    error message points the caller at the pg_repack fallback. Identifier
    validation guards the schema and table names — the relation name
    cannot be parameter-bound on a DDL statement.
    """
    driver = database.driver()
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_REPACK_VERSION:
        raise RepackError(
            f"In-server REPACK requires PostgreSQL 19 or newer; this server reports "
            f"{ver or 'unknown'} (server_version_num={ver_num}). "
            "Use the pg_repack extension shell-out path instead."
        )
    qualified = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
    concurrently_clause = " CONCURRENTLY" if concurrently else ""
    repack_sql = f"REPACK {qualified}{concurrently_clause}"
    await database.run_unmanaged(repack_sql)
    return RepackResult(
        schema=schema,
        table=table,
        concurrently=concurrently,
        repack_sql=repack_sql,
    )


__all__ = [
    "RepackError",
    "RepackResult",
    "RepackStatus",
    "get_repack_status",
    "repack_table",
]
