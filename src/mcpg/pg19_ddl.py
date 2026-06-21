"""PG 19 DDL helpers — `validate_check_constraint` + `pg_get_*def()` family.

Two related operator workflows, bundled here because the PG 19 release
blog ships them in the same "DDL & schema management" group:

* **Validate a `NOT VALID` constraint** — issuing the
  ``ALTER TABLE … VALIDATE CONSTRAINT`` follow-up after a constraint
  was first added with ``NOT VALID``. The SQL has worked since PG 9.x,
  so this tool is *not* gated on PG 19; it ships now because it closes
  the "create-NOT-VALID, validate later" loop that the agent surface
  has been missing.
* **Dump DDL for roles / databases / tablespaces** — PG 19 ships new
  ``pg_get_roledef(oid)``, ``pg_get_databasedef(oid)``, and
  ``pg_get_tablespacedef(oid)`` SQL functions that return the
  ``CREATE``-statement text for the cluster-level objects that used to
  require shelling out to ``pg_dumpall``. On PG ≤ 18 the read tools
  surface ``available=False`` with a diagnostic pointing the agent at
  ``pg_dumpall --roles-only`` / ``--globals-only`` / ``--tablespaces-only``.

Backward compatibility
----------------------
Additive. ``validate_check_constraint`` works on every supported PG
version. The ``get_*_ddl`` tools degrade to ``available=False`` on
PG ≤ 18 — the agent gets a useful diagnostic instead of a crash.

Security posture
----------------
* ``validate_check_constraint`` quotes every identifier (schema, table,
  constraint). The status probe of ``pg_constraint`` is parameter-bound
  and read-only; the ``ALTER TABLE`` runs through the regular driver,
  not ``run_unmanaged`` (no autocommit requirement).
* The ``pg_get_*def()`` reads use parameter-bound lookups on
  ``pg_authid`` / ``pg_database`` / ``pg_tablespace`` — no caller
  identifiers in identifier slots.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.database import Database

# PG 19 ships the new pg_get_*def() functions. The version-num boundary.
_MIN_PG19_DDL_VERSION = 190000


class Pg19DdlError(Exception):
    """Raised when a PG 19 DDL helper operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pg19DdlStatus:
    """Reports whether PG 19's `pg_get_*def()` DDL functions are usable.

    ``available`` is True when ``server_version_num`` >= 190000. The
    individual ``has_*`` flags let the agent know which of the three
    helpers a given build actually ships (Beta 1 ships all three; this
    keeps the shape stable if a future PG release adds more).
    """

    available: bool
    server_version_num: int
    server_version: str
    has_pg_get_roledef: bool
    has_pg_get_databasedef: bool
    has_pg_get_tablespacedef: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ValidateCheckConstraintResult:
    """Outcome of `validate_check_constraint`.

    ``was_valid`` reflects the pre-call ``pg_constraint.convalidated``
    state; ``now_valid`` reflects the post-call state. When the
    constraint was already valid ``changed=False`` and no DDL is
    emitted. ``validate_sql`` is the rendered ``ALTER TABLE`` text.
    """

    schema: str
    table: str
    constraint_name: str
    was_valid: bool | None
    now_valid: bool
    changed: bool
    validate_sql: str


@dataclass(frozen=True, slots=True)
class ObjectDdlResult:
    """The DDL text for a cluster-level object (role / database / tablespace).

    ``object_type`` is one of ``"role"``, ``"database"``, ``"tablespace"``.
    ``ddl`` is the verbatim ``CREATE`` statement returned by the
    server-side ``pg_get_*def()`` function — empty string when the
    object doesn't exist (paired with ``found=False``).
    """

    object_type: str
    object_name: str
    found: bool
    ddl: str


# ---------------------------------------------------------------------------
# Shared probes
# ---------------------------------------------------------------------------


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


async def _function_exists(driver: SqlDriver, name: str) -> bool:
    """Return True when ``name`` resolves to a pg_proc row in pg_catalog."""
    rows = await driver.execute_query(
        "SELECT 1 AS present FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'pg_catalog' AND p.proname = %s LIMIT 1",
        params=[name],
        force_readonly=True,
    )
    return bool(rows)


def _quote_identifier(name: str) -> str:
    """Quote a SQL identifier, escaping embedded double-quotes."""
    if not name or "\x00" in name:
        raise Pg19DdlError(f"invalid identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Status probe
# ---------------------------------------------------------------------------


async def get_pg19_ddl_status(driver: SqlDriver) -> Pg19DdlStatus:
    """Report whether PG 19's `pg_get_*def()` DDL helpers are usable.

    Read-only; never raises. The probe checks each function
    independently via ``pg_proc`` so the result is robust against
    incremental PG releases that ship the family piecemeal. On PG ≤ 18
    every flag is ``False`` and ``detail`` points at the ``pg_dumpall``
    fallback path.
    """
    try:
        ver_num, ver = await _server_version(driver)
    except Exception as exc:
        return Pg19DdlStatus(
            available=False,
            server_version_num=0,
            server_version="",
            has_pg_get_roledef=False,
            has_pg_get_databasedef=False,
            has_pg_get_tablespacedef=False,
            detail=(
                f"PG 19 DDL helpers unavailable (version probe failed: {exc}). Re-run after the server is back online."
            ),
        )
    if ver_num < _MIN_PG19_DDL_VERSION:
        return Pg19DdlStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            has_pg_get_roledef=False,
            has_pg_get_databasedef=False,
            has_pg_get_tablespacedef=False,
            detail=(
                "PG 19's pg_get_roledef() / pg_get_databasedef() / "
                "pg_get_tablespacedef() functions require PostgreSQL 19 "
                "or newer; this server is older. Fall back to "
                "`pg_dumpall --roles-only` / `--globals-only` / "
                "`--tablespaces-only` for cluster-level DDL."
            ),
        )
    try:
        has_role = await _function_exists(driver, "pg_get_roledef")
        has_database = await _function_exists(driver, "pg_get_databasedef")
        has_tablespace = await _function_exists(driver, "pg_get_tablespacedef")
    except Exception as exc:
        return Pg19DdlStatus(
            available=False,
            server_version_num=ver_num,
            server_version=ver,
            has_pg_get_roledef=False,
            has_pg_get_databasedef=False,
            has_pg_get_tablespacedef=False,
            detail=f"PG 19 reachable but pg_proc probe failed: {exc}. Re-run after the server is back online.",
        )
    available = has_role or has_database or has_tablespace
    if available:
        detail = (
            "PG 19 DDL helpers available: "
            f"role={'yes' if has_role else 'no'}, "
            f"database={'yes' if has_database else 'no'}, "
            f"tablespace={'yes' if has_tablespace else 'no'}. "
            "Call get_role_ddl / get_database_ddl / get_tablespace_ddl."
        )
    else:
        detail = (
            "PG 19 reachable but none of the pg_get_*def() functions "
            "are present in pg_catalog — your build may have stripped "
            "them. Fall back to pg_dumpall for cluster-level DDL."
        )
    return Pg19DdlStatus(
        available=available,
        server_version_num=ver_num,
        server_version=ver,
        has_pg_get_roledef=has_role,
        has_pg_get_databasedef=has_database,
        has_pg_get_tablespacedef=has_tablespace,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# validate_check_constraint — works on every supported PG version
# ---------------------------------------------------------------------------


async def _read_constraint_validated(
    driver: SqlDriver,
    *,
    schema: str,
    table: str,
    constraint_name: str,
) -> bool | None:
    """Return ``pg_constraint.convalidated`` for the named constraint.

    Returns ``None`` when the constraint doesn't exist (the caller
    surfaces that as a ``Pg19DdlError`` with a useful message). The
    join through ``pg_class`` / ``pg_namespace`` keeps the lookup
    scoped to the exact (schema, table, constraint) triple — two
    tables in different schemas can legally share a constraint name.
    """
    rows = await driver.execute_query(
        "SELECT c.convalidated FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = %s AND t.relname = %s AND c.conname = %s LIMIT 1",
        params=[schema, table, constraint_name],
        force_readonly=True,
    )
    if not rows:
        return None
    raw = rows[0].cells.get("convalidated")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"on", "true", "yes", "t", "1"}


async def validate_check_constraint(
    database: Database,
    *,
    schema: str,
    table: str,
    constraint_name: str,
) -> ValidateCheckConstraintResult:
    """Issue ``ALTER TABLE … VALIDATE CONSTRAINT`` for a `NOT VALID` constraint.

    Closes the "added NOT VALID at low cost, now actually validate"
    loop. Idempotent — if the constraint is already valid, no DDL is
    emitted and ``changed=False`` is returned. Works on every supported
    PG version (the SQL has been around since 9.x); not gated on PG 19.

    Raises :class:`Pg19DdlError` when the named constraint doesn't
    exist or when the underlying ``ALTER TABLE`` fails (lock timeout,
    permission denied, constraint actually violated by data, etc.).
    """
    driver = database.driver()
    was_valid = await _read_constraint_validated(
        driver,
        schema=schema,
        table=table,
        constraint_name=constraint_name,
    )
    if was_valid is None:
        raise Pg19DdlError(f"constraint {constraint_name!r} not found on {schema}.{table} (no row in pg_constraint).")
    qualified = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
    validate_sql = f"ALTER TABLE {qualified} VALIDATE CONSTRAINT {_quote_identifier(constraint_name)}"
    if was_valid:
        return ValidateCheckConstraintResult(
            schema=schema,
            table=table,
            constraint_name=constraint_name,
            was_valid=True,
            now_valid=True,
            changed=False,
            validate_sql=f"-- {constraint_name} already validated; no-op",
        )
    try:
        await driver.execute_query(validate_sql, force_readonly=False)
    except Exception as exc:
        raise Pg19DdlError(f"VALIDATE CONSTRAINT {constraint_name} on {schema}.{table} failed: {exc}") from exc
    return ValidateCheckConstraintResult(
        schema=schema,
        table=table,
        constraint_name=constraint_name,
        was_valid=False,
        now_valid=True,
        changed=True,
        validate_sql=validate_sql,
    )


# ---------------------------------------------------------------------------
# pg_get_*def() — cluster-level DDL dumps (PG 19+)
# ---------------------------------------------------------------------------


async def _ensure_pg19(driver: SqlDriver, feature: str) -> None:
    """Common version gate for the get_*_ddl tools."""
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_PG19_DDL_VERSION:
        raise Pg19DdlError(
            f"{feature} requires PostgreSQL 19 or newer; this server "
            f"reports {ver or 'unknown'} (server_version_num={ver_num}). "
            "Fall back to pg_dumpall for cluster-level DDL."
        )


async def get_role_ddl(driver: SqlDriver, role_name: str) -> ObjectDdlResult:
    """Return the ``CREATE ROLE`` DDL for ``role_name``.

    Wraps PG 19's ``pg_get_roledef(oid)``. Returns ``found=False`` with
    an empty ``ddl`` when no matching role exists. Requires PG 19+;
    raises :class:`Pg19DdlError` on older servers with a pg_dumpall
    fallback hint.
    """
    await _ensure_pg19(driver, "get_role_ddl")
    try:
        rows = await driver.execute_query(
            "SELECT pg_get_roledef(oid) AS ddl FROM pg_authid WHERE rolname = %s LIMIT 1",
            params=[role_name],
            force_readonly=True,
        )
    except Exception as exc:
        raise Pg19DdlError(f"pg_get_roledef({role_name!r}) failed: {exc}") from exc
    if not rows:
        return ObjectDdlResult(object_type="role", object_name=role_name, found=False, ddl="")
    ddl = rows[0].cells.get("ddl")
    return ObjectDdlResult(
        object_type="role",
        object_name=role_name,
        found=True,
        ddl="" if ddl is None else str(ddl),
    )


async def get_database_ddl(driver: SqlDriver, database_name: str) -> ObjectDdlResult:
    """Return the ``CREATE DATABASE`` DDL for ``database_name``.

    Wraps PG 19's ``pg_get_databasedef(oid)``. Returns ``found=False``
    with an empty ``ddl`` when no matching database exists. Requires
    PG 19+.
    """
    await _ensure_pg19(driver, "get_database_ddl")
    try:
        rows = await driver.execute_query(
            "SELECT pg_get_databasedef(oid) AS ddl FROM pg_database WHERE datname = %s LIMIT 1",
            params=[database_name],
            force_readonly=True,
        )
    except Exception as exc:
        raise Pg19DdlError(f"pg_get_databasedef({database_name!r}) failed: {exc}") from exc
    if not rows:
        return ObjectDdlResult(object_type="database", object_name=database_name, found=False, ddl="")
    ddl = rows[0].cells.get("ddl")
    return ObjectDdlResult(
        object_type="database",
        object_name=database_name,
        found=True,
        ddl="" if ddl is None else str(ddl),
    )


async def get_tablespace_ddl(driver: SqlDriver, tablespace_name: str) -> ObjectDdlResult:
    """Return the ``CREATE TABLESPACE`` DDL for ``tablespace_name``.

    Wraps PG 19's ``pg_get_tablespacedef(oid)``. Returns ``found=False``
    with an empty ``ddl`` when no matching tablespace exists. Requires
    PG 19+.
    """
    await _ensure_pg19(driver, "get_tablespace_ddl")
    try:
        rows = await driver.execute_query(
            "SELECT pg_get_tablespacedef(oid) AS ddl FROM pg_tablespace WHERE spcname = %s LIMIT 1",
            params=[tablespace_name],
            force_readonly=True,
        )
    except Exception as exc:
        raise Pg19DdlError(f"pg_get_tablespacedef({tablespace_name!r}) failed: {exc}") from exc
    if not rows:
        return ObjectDdlResult(object_type="tablespace", object_name=tablespace_name, found=False, ddl="")
    ddl = rows[0].cells.get("ddl")
    return ObjectDdlResult(
        object_type="tablespace",
        object_name=tablespace_name,
        found=True,
        ddl="" if ddl is None else str(ddl),
    )


__all__ = [
    "ObjectDdlResult",
    "Pg19DdlError",
    "Pg19DdlStatus",
    "ValidateCheckConstraintResult",
    "get_database_ddl",
    "get_pg19_ddl_status",
    "get_role_ddl",
    "get_tablespace_ddl",
    "validate_check_constraint",
]
