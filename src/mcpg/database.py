"""Database connection lifecycle for the MCPg server.

Wraps the vendored connection pool with explicit connect/close semantics,
typed errors, and async-context-manager support, so the server owns a single
``Database`` instance instead of relying on module-level global state.
"""

from __future__ import annotations

from types import TracebackType

from mcpg._vendor.sql import DbConnPool, SqlDriver, obfuscate_password
from mcpg.config import Settings


class DatabaseError(Exception):
    """Raised when the database cannot be connected to or used."""


class Database:
    """Owns the PostgreSQL connection pool for the server's lifetime."""

    def __init__(self, settings: Settings, *, pool: DbConnPool | None = None) -> None:
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
        self._connected = True

    async def close(self) -> None:
        """Close the connection pool. Safe to call when not connected."""
        await self._pool.close()
        self._connected = False

    def driver(self) -> SqlDriver:
        """Return a SQL driver bound to the pool.

        Raises:
            DatabaseError: If called before :meth:`connect`.
        """
        if not self._connected:
            raise DatabaseError("database is not connected; call connect() first")
        return SqlDriver(conn=self._pool)

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
