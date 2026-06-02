"""Database connection lifecycle for the MCPg server.

Wraps the vendored connection pool with explicit connect/close semantics,
typed errors, and async-context-manager support, so the server owns a single
``Database`` instance instead of relying on module-level global state.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Any

from mcpg._vendor.sql import DbConnPool, SqlDriver, obfuscate_password
from mcpg.config import Settings
from mcpg.replicas import ReplicaPool, RoutedSqlDriver, _make_driver_for_pool


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
        self._connected = True

    async def close(self) -> None:
        """Close the connection pool. Safe to call when not connected."""
        if self._replica_pool is not None:
            await self._replica_pool.close()
        await self._pool.close()
        self._connected = False

    @property
    def replica_pool(self) -> ReplicaPool | None:
        """The configured :class:`ReplicaPool`, or ``None`` when unset."""
        return self._replica_pool

    def driver(self) -> SqlDriver:
        """Return a SQL driver bound to the pool.

        Composes three optional behaviours:

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
