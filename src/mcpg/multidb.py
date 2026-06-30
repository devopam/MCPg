"""Multi-database selector — named, read-only secondary databases (roadmap 13.1).

One MCPg server can serve multiple databases. The primary
(``MCPG_DATABASE_URL``) stays the default; additional named secondaries are
configured via ``MCPG_SECONDARY_DATABASE_URLS`` and are **read-only, enforced
at the PostgreSQL level**.

Read-only enforcement
---------------------

:class:`ReadOnlySqlDriver` wraps a per-secondary pool and forces *every*
query to execute inside a ``BEGIN TRANSACTION READ ONLY`` block (by always
passing ``force_readonly=True`` down to the vendored driver, regardless of
what the caller asked for). PostgreSQL then rejects any write or DDL with
``ERROR: cannot execute … in a read-only transaction`` (SQLSTATE 25006). This
makes the read-only boundary a server-side guarantee, not a convention — a
stray ``INSERT`` routed at a secondary fails closed even if the calling tool
forgot to mark itself read-only.

This sidesteps per-database write/DDL/LISTEN gating entirely: the global
write / DDL / shell / listen / migrate gates continue to apply only to the
primary, and secondaries simply can't be written to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import DbConnPool, SqlDriver

# The implicit id of ``MCPG_DATABASE_URL``. Reserved (see config.py) so a
# secondary can never shadow it.
PRIMARY_DATABASE_ID = "primary"


class ReadOnlySqlDriver(SqlDriver):
    """SqlDriver that pins every query to a read-only transaction.

    Used for secondary databases. Overrides :meth:`execute_query` so the
    ``force_readonly`` flag is always ``True`` on the wire — the vendored
    driver then opens a ``BEGIN TRANSACTION READ ONLY`` for every statement
    and PostgreSQL rejects writes / DDL. Also applies the configured
    statement / lock timeouts, mirroring the primary path.
    """

    def __init__(
        self,
        *args: Any,
        statement_timeout_ms: int = 30000,
        lock_timeout_ms: int = 5000,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms

    async def execute_query(
        self,
        query: Any,
        params: list[Any] | None = None,
        force_readonly: bool = False,
    ) -> list[SqlDriver.RowResult] | None:
        # Ignore the caller's flag — a secondary is read-only, full stop.
        del force_readonly
        return await super().execute_query(query, params, force_readonly=True)

    async def _execute_with_connection(  # type: ignore[no-untyped-def]
        self,
        connection,
        query,
        params,
        force_readonly,
    ):
        if not getattr(connection, "_timeouts_configured", False):
            async with connection.cursor() as cursor:
                await cursor.execute(
                    f"SET statement_timeout = {self._statement_timeout_ms}; SET lock_timeout = {self._lock_timeout_ms}"
                )
            try:
                connection._timeouts_configured = True
            except AttributeError:
                pass
        return await super()._execute_with_connection(connection, query, params, force_readonly)


# NOTE: these two return shapes intentionally avoid ``slots=True`` and field
# defaults. FastMCP derives the tool ``outputSchema`` by running pydantic's
# schema builder over the return annotation; a slotted dataclass with a
# ``field(default_factory=...)`` trips pydantic's non-serializable-default
# guard and silently drops the schema. The other nested-return shapes in the
# codebase (``introspection.PartitionSet`` / ``PolicySet``) follow the same
# all-required-fields, no-slots convention for exactly this reason.
@dataclass(frozen=True)
class DatabaseDescriptor:
    """One configured database (primary or secondary) and its live health.

    ``read_only`` is ``True`` for every secondary and ``False`` for the
    primary. ``reachable`` is a per-call ``SELECT 1`` probe result;
    ``detail`` carries an obfuscated error string when the probe failed.
    """

    id: str
    is_primary: bool
    read_only: bool
    reachable: bool
    detail: str | None


@dataclass(frozen=True)
class DatabaseList:
    """Result of ``list_databases`` — every configured database id + health."""

    primary_id: str
    database_ids: list[str]
    databases: list[DatabaseDescriptor]


def make_read_only_driver(
    pool: DbConnPool,
    *,
    statement_timeout_ms: int = 30000,
    lock_timeout_ms: int = 5000,
) -> ReadOnlySqlDriver:
    """Build a read-only driver bound to a secondary's pool."""
    return ReadOnlySqlDriver(
        conn=pool,
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
    )


__all__ = [
    "PRIMARY_DATABASE_ID",
    "DatabaseDescriptor",
    "DatabaseList",
    "ReadOnlySqlDriver",
    "make_read_only_driver",
]
