"""PostgreSQL connection pool + query driver — first-party SQL kernel.

This is MCPg's own connection/pool/execution layer, re-authored from the
formerly-vendored ``crystaldba/postgres-mcp`` ``sql_driver.py`` (MIT). It
carries **no** SQL-safety policy — the allowlist lives in
:mod:`mcpg.sql.safety`. Three public names:

* :func:`obfuscate_password` — redact credentials from URLs / error text.
* :class:`DbConnPool` — a lazily-opened ``psycopg`` async connection pool.
* :class:`SqlDriver` — the execute-query adapter every ``mcpg`` module
  depends on, returning :class:`SqlDriver.RowResult` rows.

Behaviour is preserved verbatim from the vendored version (the adversarial
``tests/`` suite pins it); this rewrite only modernises typing and
separates concerns.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, LiteralString
from urllib.parse import urlparse, urlunparse

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


def obfuscate_password(text: str | None) -> str | None:
    """Obfuscate any password embedded in connection info.

    Works on connection URLs, libpq DSN strings, and free-form error
    messages — anywhere a ``postgres://user:pass@host`` URL or a
    ``password=…`` parameter might leak. Returns the input unchanged when
    there's nothing to redact (and ``None`` passes through).
    """
    if text is None:
        return None
    if not text:
        return text

    # Try first as a proper URL.
    try:
        parsed = urlparse(text)
        if parsed.scheme and parsed.netloc and parsed.password:
            netloc = parsed.netloc.replace(parsed.password, "****")
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass

    # Strings that contain a connection URL but aren't a clean URL:
    # postgres://user:password@host:port/dbname
    url_pattern = re.compile(r"(postgres(?:ql)?:\/\/[^:]+:)([^@]+)(@[^\/\s]+)")
    text = re.sub(url_pattern, r"\1****\3", text)

    # DSN parameter without quotes: password=xxx
    param_pattern = re.compile(r'(password=)([^\s&;"\']+)', re.IGNORECASE)
    text = re.sub(param_pattern, r"\1****", text)

    # DSN parameter, single-quoted value.
    dsn_single_quote = re.compile(r"(password\s*=\s*')([^']+)(')", re.IGNORECASE)
    text = re.sub(dsn_single_quote, r"\1****\3", text)

    # DSN parameter, double-quoted value.
    dsn_double_quote = re.compile(r'(password\s*=\s*")([^"]+)(")', re.IGNORECASE)
    text = re.sub(dsn_double_quote, r"\1****\3", text)

    return text


class DbConnPool:
    """A lazily-opened ``psycopg`` async connection pool.

    ``min_size`` / ``max_size`` are MCPg-configurable (ADR-0003); the
    defaults (1 / 5) reproduce the original hardcoded behaviour.
    """

    def __init__(self, connection_url: str | None = None, min_size: int = 1, max_size: int = 5) -> None:
        self.connection_url = connection_url
        self.min_size = min_size
        self.max_size = max_size
        self.pool: AsyncConnectionPool | None = None
        self._is_valid = False
        self._last_error: str | None = None

    async def pool_connect(self, connection_url: str | None = None) -> AsyncConnectionPool:
        """Open (once) and return the pool, testing it with ``SELECT 1``."""
        if self.pool and self._is_valid:
            return self.pool

        url = connection_url or self.connection_url
        self.connection_url = url
        if not url:
            self._is_valid = False
            self._last_error = "Database connection URL not provided"
            raise ValueError(self._last_error)

        # Close any existing pool before creating a new one.
        await self.close()

        try:
            self.pool = AsyncConnectionPool(
                conninfo=url,
                min_size=self.min_size,
                max_size=self.max_size,
                open=False,  # open explicitly below
            )
            await self.pool.open()

            # Prove the pool works before handing it out.
            async with self.pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1")

            self._is_valid = True
            self._last_error = None
            return self.pool
        except Exception as e:
            self._is_valid = False
            self._last_error = str(e)
            await self.close()
            raise ValueError(f"Connection attempt failed: {obfuscate_password(str(e))}") from e

    async def close(self) -> None:
        """Close the pool if open; swallow shutdown errors."""
        if self.pool:
            try:
                await self.pool.close()
            except Exception as e:
                logger.warning("Error closing connection pool: %s", e)
            finally:
                self.pool = None
                self._is_valid = False

    @property
    def is_valid(self) -> bool:
        """Whether the last ``pool_connect`` succeeded and the pool is live."""
        return self._is_valid

    @property
    def last_error(self) -> str | None:
        """The most recent connection error, or ``None``."""
        return self._last_error


class SqlDriver:
    """Execute-query adapter over a raw connection or a :class:`DbConnPool`."""

    @dataclass
    class RowResult:
        """One result row as a ``column -> value`` mapping."""

        cells: dict[str, Any]

    def __init__(self, conn: Any = None, engine_url: str | None = None) -> None:
        """Wrap a connection / pool, or defer to an ``engine_url``.

        Exactly one of ``conn`` (a live connection or :class:`DbConnPool`)
        or ``engine_url`` (a DSN, connected lazily) must be given.
        """
        if conn:
            self.conn = conn
            self.is_pool = isinstance(conn, DbConnPool)
        elif engine_url:
            # Defer connecting — the pool opens on first query (async).
            self.engine_url = engine_url
            self.conn = None
            self.is_pool = False
        else:
            raise ValueError("Either conn or engine_url must be provided")

    def connect(self) -> Any:
        """Return the connection/pool, creating a pool from ``engine_url`` if needed."""
        if self.conn is not None:
            return self.conn
        if getattr(self, "engine_url", None):
            self.conn = DbConnPool(self.engine_url)
            self.is_pool = True
            return self.conn
        raise ValueError("Connection not established. Either conn or engine_url must be provided")

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = False,
    ) -> list[RowResult] | None:
        """Run ``query`` and return its rows (or ``None`` for no result set).

        ``force_readonly`` wraps execution in ``BEGIN TRANSACTION READ ONLY``
        so the server rejects any write the statement attempts. On a pool a
        connection is checked out per call; on error the pool is marked
        invalid (or a direct connection is dropped) and the exception
        re-raised.
        """
        try:
            if self.conn is None:
                self.connect()
                if self.conn is None:
                    raise ValueError("Connection not established")

            if self.is_pool:
                pool = await self.conn.pool_connect()
                async with pool.connection() as connection:
                    return await self._execute_with_connection(connection, query, params, force_readonly=force_readonly)
            return await self._execute_with_connection(self.conn, query, params, force_readonly=force_readonly)
        except Exception as e:
            # A connection-level failure invalidates the pool / drops the conn.
            if self.conn and self.is_pool:
                self.conn._is_valid = False
                self.conn._last_error = str(e)
            elif self.conn and not self.is_pool:
                self.conn = None
            raise

    async def _execute_with_connection(
        self, connection: Any, query: Any, params: Any, force_readonly: bool
    ) -> list[RowResult] | None:
        """Execute on a specific connection with read-only + txn handling."""
        transaction_started = False
        try:
            async with connection.cursor(row_factory=dict_row) as cursor:
                if force_readonly:
                    await cursor.execute("BEGIN TRANSACTION READ ONLY")
                    transaction_started = True

                if params:
                    await cursor.execute(query, params)
                else:
                    await cursor.execute(query)

                # For multi-statement input, advance to the last result set.
                while cursor.nextset():
                    pass

                if cursor.description is None:  # no result set (e.g. DDL)
                    if not force_readonly:
                        await cursor.execute("COMMIT")
                    elif transaction_started:
                        await cursor.execute("ROLLBACK")
                        transaction_started = False
                    return None

                rows = await cursor.fetchall()

                if not force_readonly:
                    await cursor.execute("COMMIT")
                elif transaction_started:
                    await cursor.execute("ROLLBACK")
                    transaction_started = False

                return [SqlDriver.RowResult(cells=dict(row)) for row in rows]
        except Exception as e:
            if transaction_started:
                try:
                    await connection.rollback()
                except Exception as rollback_error:
                    logger.error("Error rolling back transaction: %s", rollback_error)
            logger.error("Error executing query (%s): %s", query, e)
            raise
