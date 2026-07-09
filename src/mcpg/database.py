"""Database connection lifecycle for the MCPg server.

Wraps the vendored connection pool with explicit connect/close semantics,
typed errors, and async-context-manager support, so the server owns a single
``Database`` instance instead of relying on module-level global state.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from types import TracebackType
from typing import Any

from mcpg.config import Settings
from mcpg.multidb import PRIMARY_DATABASE_ID, make_read_only_driver, resolve_primary_id
from mcpg.replicas import ReplicaPool, RoutedSqlDriver, _make_driver_for_pool
from mcpg.sql import DbConnPool, SqlDriver, obfuscate_password

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Raised when the database cannot be connected to or used."""


class Database:
    """Owns the PostgreSQL connection pool for the server's lifetime."""

    def __init__(
        self,
        settings: Settings,
        *,
        pool: DbConnPool | None = None,
        replica_pool: ReplicaPool | None = None,
        secondary_pools: dict[str, DbConnPool] | None = None,
    ) -> None:
        self._settings = settings
        self._pool = (
            pool
            if pool is not None
            else DbConnPool(
                settings.database_url,
                min_size=settings.pool_min_size,
                max_size=settings.pool_max_size,
            )
        )
        # Read-replica routing — opt-in via MCPG_REPLICA_URLS. The
        # explicit ``replica_pool`` argument lets tests inject a
        # pre-built / faked pool.
        if replica_pool is not None:
            self._replica_pool: ReplicaPool | None = replica_pool
        elif settings.replica_urls:
            self._replica_pool = ReplicaPool(
                settings.replica_urls,
                pool_min_size=settings.pool_min_size,
                pool_max_size=settings.pool_max_size,
            )
        else:
            self._replica_pool = None
        # Multi-database selector (roadmap 13.1). One read-only pool per
        # configured secondary. Tests may inject pre-built pools via
        # ``secondary_pools`` (keyed by name); otherwise we build one
        # ``DbConnPool`` per ``MCPG_SECONDARY_DATABASE_URLS`` entry. A
        # secondary that fails to connect at startup is marked unavailable
        # (tracked in ``_secondary_available``) but does NOT abort startup.
        if secondary_pools is not None:
            self._secondary_pools: dict[str, DbConnPool] = dict(secondary_pools)
        else:
            self._secondary_pools = {
                name: DbConnPool(
                    dsn,
                    min_size=settings.pool_min_size,
                    max_size=settings.pool_max_size,
                )
                for name, dsn in settings.secondary_database_urls
            }
        self._secondary_available: dict[str, bool] = dict.fromkeys(self._secondary_pools, False)
        # The id the primary is advertised + addressable under: its real
        # database name when derivable (so ``database="lookup"`` just works),
        # else the generic ``"primary"``. ``"primary"`` / ``None`` stay valid
        # aliases either way (roadmap 13.1 papercut).
        self._primary_id = resolve_primary_id(settings.database_url, self._secondary_pools)
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """Whether the pool is open and last validated successfully."""
        return self._connected and self._pool.is_valid

    async def connect(self) -> None:
        """Open and validate the connection pool.

        Raises:
            DatabaseError: If the pool cannot be opened.
        """
        try:
            await self._pool.pool_connect()
        except Exception as exc:
            self._connected = False
            # The vendored pool already obfuscates; re-apply defensively.
            raise DatabaseError(f"could not connect to the database: {obfuscate_password(str(exc))}") from exc
        if self._replica_pool is not None:
            # Replica connect failures are logged + marked degraded
            # individually; they don't abort startup. See
            # ``ReplicaPool.connect``.
            await self._replica_pool.connect()
        # Secondary databases (roadmap 13.1): a connect failure marks that
        # secondary unavailable but does NOT abort startup — mirrors the
        # replica-pool tolerance. ``list_databases`` surfaces the state.
        for name, pool in self._secondary_pools.items():
            try:
                await pool.pool_connect()
            except Exception as exc:
                self._secondary_available[name] = False
                logger.warning(
                    "Secondary database %r failed to open: %s",
                    name,
                    obfuscate_password(str(exc)),
                )
            else:
                self._secondary_available[name] = True
        self._connected = True

    async def close(self) -> None:
        """Close the connection pool. Safe to call when not connected."""
        if self._replica_pool is not None:
            await self._replica_pool.close()
        for name, pool in self._secondary_pools.items():
            try:
                await pool.close()
            except Exception as exc:
                logger.warning("Error closing secondary database %r pool: %s", name, exc)
        await self._pool.close()
        self._connected = False

    @property
    def replica_pool(self) -> ReplicaPool | None:
        """The configured :class:`ReplicaPool`, or ``None`` when unset."""
        return self._replica_pool

    def driver(self, database_id: str | None = None) -> SqlDriver:
        """Return a SQL driver bound to the selected database.

        ``database_id`` selects which configured database to target:

        * ``None`` or ``"primary"`` → the primary driver (UNCHANGED path —
          composes tenancy + replica routing exactly as before).
        * a configured secondary name → a **read-only** driver bound to that
          secondary's pool. Read-only is PostgreSQL-enforced (every query
          runs inside ``BEGIN TRANSACTION READ ONLY``; see
          :class:`mcpg.multidb.ReadOnlySqlDriver`). No tenancy or replica
          layering is applied to secondaries.
        * an unknown name → :class:`DatabaseError` listing the valid ids.

        The primary path composes three optional behaviours:

        * **Tenancy** — when ``settings.default_role`` or
          ``allowed_roles`` is set, the underlying driver is
          :class:`mcpg.tenancy.TenantSqlDriver` so every query runs
          inside ``SET LOCAL ROLE "<role>"``.
        * **Replica routing** — when ``settings.replica_urls`` is
          non-empty, the returned driver is
          :class:`mcpg.replicas.RoutedSqlDriver`, which delegates
          ``force_readonly=True`` queries to a round-robin healthy
          replica and writes to the primary.
        * **Neither** — the bare upstream driver is returned (zero
          overhead path).

        Raises:
            DatabaseError: If called before :meth:`connect`.
        """
        if not self._connected:
            raise DatabaseError("database is not connected; call connect() first")
        # Secondary-database selector (roadmap 13.1). Any explicit, non-primary
        # id routes to a read-only secondary driver; an unknown id is a hard
        # error listing the valid ids so the agent can self-correct. The
        # primary answers to ``None``, the literal ``"primary"`` alias, and its
        # real database name (``self._primary_id``).
        if database_id is not None and database_id not in (PRIMARY_DATABASE_ID, self._primary_id):
            pool = self._secondary_pools.get(database_id)
            if pool is None:
                raise DatabaseError(f"unknown database id {database_id!r}; valid ids: {', '.join(self.database_ids())}")
            return make_read_only_driver(
                pool,
                statement_timeout_ms=self._settings.statement_timeout_ms,
                lock_timeout_ms=self._settings.lock_timeout_ms,
            )
        enable_tenancy = self._settings.default_role is not None or bool(self._settings.allowed_roles)
        primary = _make_driver_for_pool(
            self._pool,
            default_role=self._settings.default_role,
            enable_tenancy=enable_tenancy,
            statement_timeout_ms=self._settings.statement_timeout_ms,
            lock_timeout_ms=self._settings.lock_timeout_ms,
        )
        if self._replica_pool is None:
            primary.settings = self._settings  # type: ignore[attr-defined]
            return primary
        replica_drivers = [
            _make_driver_for_pool(
                state.pool,
                default_role=self._settings.default_role,
                enable_tenancy=enable_tenancy,
                statement_timeout_ms=self._settings.statement_timeout_ms,
                lock_timeout_ms=self._settings.lock_timeout_ms,
            )
            for state in self._replica_pool._states
        ]
        driver = RoutedSqlDriver(
            primary=primary,
            replicas=replica_drivers,
            replica_pool=self._replica_pool,
        )
        driver.settings = self._settings  # type: ignore[attr-defined]
        return driver

    @property
    def primary_id(self) -> str:
        """The id the primary is advertised under (its real DB name, or ``"primary"``)."""
        return self._primary_id

    def database_ids(self) -> list[str]:
        """Return every configured database id, primary first.

        The primary's id is its real database name when derivable from
        ``MCPG_DATABASE_URL`` (else the generic ``"primary"``); secondaries
        follow in ``MCPG_SECONDARY_DATABASE_URLS`` order. The literal
        ``"primary"`` remains an accepted alias regardless.
        """
        return [self._primary_id, *self._secondary_pools.keys()]

    async def probe(self, database_id: str | None = None) -> tuple[bool, str | None]:
        """Probe one database with ``SELECT 1``.

        Returns ``(reachable, detail)`` — ``detail`` is an obfuscated error
        string when the probe failed, else ``None``. An unknown id raises
        :class:`DatabaseError` (same contract as :meth:`driver`).
        """
        try:
            driver = self.driver(database_id)
            await driver.execute_query("SELECT 1", force_readonly=True)
        except DatabaseError:
            raise
        except Exception as exc:
            return False, obfuscate_password(str(exc))
        return True, None

    async def describe_databases(self) -> list[tuple[str, bool, bool, bool, str | None]]:
        """Describe every configured database for ``list_databases``.

        Yields one ``(id, is_primary, read_only, reachable, detail)`` tuple
        per database, primary first. ``reachable`` is a live ``SELECT 1``
        probe; secondaries are always ``read_only=True``.
        """
        out: list[tuple[str, bool, bool, bool, str | None]] = []
        for db_id in self.database_ids():
            is_primary = db_id == self._primary_id
            reachable, detail = await self.probe(db_id)
            out.append((db_id, is_primary, not is_primary, reachable, detail))
        return out

    async def copy_from_stdin(self, sql: str, data: bytes) -> int:
        """Stream ``data`` into a ``COPY ... FROM STDIN`` statement.

        Returns the row count the server reports (``cursor.rowcount``).
        The caller is responsible for SQL safety — the SQL is executed
        verbatim, so identifiers in it must already be validated.

        Raises:
            DatabaseError: If called before :meth:`connect`.
        """
        if not self._connected:
            raise DatabaseError("database is not connected; call connect() first")
        pool = await self._pool.pool_connect()
        async with pool.connection() as connection, connection.cursor() as cursor:
            async with cursor.copy(sql) as copy:
                if data:
                    await copy.write(data)
            return cursor.rowcount

    async def execute_many(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
        """Run ``sql`` once per row of ``params_seq`` via ``executemany``.

        Returns the total row count the server reports across all rows.
        The caller is responsible for SQL safety; values are passed as
        bound parameters, so they cannot inject SQL.

        Raises:
            DatabaseError: If called before :meth:`connect`.
        """
        if not self._connected:
            raise DatabaseError("database is not connected; call connect() first")
        pool = await self._pool.pool_connect()
        async with pool.connection() as connection, connection.cursor() as cursor:
            await cursor.executemany(sql, params_seq)
            return cursor.rowcount

    async def run_unmanaged(self, sql: str) -> None:
        """Execute a statement on an autocommit connection — no transaction.

        For maintenance commands such as ``VACUUM`` that cannot run inside a
        transaction block. The caller is responsible for SQL safety.

        Raises:
            DatabaseError: If called before :meth:`connect`.
        """
        if not self._connected:
            raise DatabaseError("database is not connected; call connect() first")
        pool = await self._pool.pool_connect()
        async with pool.connection() as connection:
            await connection.set_autocommit(True)
            try:
                await connection.execute(sql)
            finally:
                await connection.set_autocommit(False)

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
