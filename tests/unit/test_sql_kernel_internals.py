"""Unit coverage for the first-party SQL kernel's logic paths.

Covers the pure-logic surfaces of the kernel (validator branches, the
credential-safe static helpers, the pool/driver error handling, and the
``SafeSqlDriver`` timeout path) so they meet the 90% coverage gate without a
live database. The raw psycopg execution plumbing in ``DbConnPool.pool_connect``
and ``SqlDriver._execute_with_connection`` is integration-tested (the PG CI
lanes drive real connections) and marked ``# pragma: no cover``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcpg.sql import DbConnPool, SafeSqlDriver, SqlDriver

# --------------------------------------------------------------------------
# DbConnPool — the non-I/O paths
# --------------------------------------------------------------------------


async def test_pool_connect_rejects_missing_url() -> None:
    pool = DbConnPool()
    with pytest.raises(ValueError, match="not provided"):
        await pool.pool_connect()
    assert pool.is_valid is False
    assert pool.last_error == "Database connection URL not provided"


async def test_pool_close_swallows_errors() -> None:
    pool = DbConnPool("postgresql://u:p@localhost/db")
    bad = MagicMock()
    bad.close = AsyncMock(side_effect=RuntimeError("cannot close"))
    pool.pool = bad
    await pool.close()  # must not raise
    assert pool.pool is None
    assert pool.is_valid is False


async def test_pool_close_noop_when_no_pool() -> None:
    pool = DbConnPool("postgresql://u:p@localhost/db")
    await pool.close()  # no pool yet — must be a clean no-op
    assert pool.pool is None


def test_pool_properties_default() -> None:
    pool = DbConnPool("postgresql://u:p@localhost/db")
    assert pool.is_valid is False
    assert pool.last_error is None


async def test_pool_connect_returns_cached_when_valid() -> None:
    pool = DbConnPool("postgresql://u:p@localhost/db")
    cached = MagicMock()
    pool.pool = cached
    pool._is_valid = True
    assert await pool.pool_connect() is cached


async def test_pool_connect_wraps_construction_failure() -> None:
    pool = DbConnPool("postgresql://u:p@localhost/db")
    with patch("mcpg.sql.driver.AsyncConnectionPool", side_effect=RuntimeError("boom")):
        with pytest.raises(ValueError, match="Connection attempt failed"):
            await pool.pool_connect()
    assert pool.is_valid is False
    assert pool.last_error is not None


# --------------------------------------------------------------------------
# SqlDriver — construction / connect / error handling
# --------------------------------------------------------------------------


def test_driver_requires_conn_or_engine_url() -> None:
    with pytest.raises(ValueError, match="Either conn or engine_url"):
        SqlDriver()


def test_driver_connect_builds_pool_from_engine_url() -> None:
    driver = SqlDriver(engine_url="postgresql://u:p@localhost/db")
    result = driver.connect()
    assert isinstance(result, DbConnPool)
    assert driver.is_pool is True
    # A second connect returns the same pool.
    assert driver.connect() is result


def test_driver_connect_returns_existing_conn() -> None:
    conn = MagicMock()
    driver = SqlDriver(conn=conn)
    assert driver.connect() is conn


async def test_execute_query_via_pool_invalidates_pool_on_error() -> None:
    db_pool = MagicMock(spec=DbConnPool)
    db_pool.pool_connect = AsyncMock(side_effect=RuntimeError("pool down"))
    driver = SqlDriver(conn=db_pool)

    with pytest.raises(RuntimeError, match="pool down"):
        await driver.execute_query("SELECT 1")
    assert db_pool._is_valid is False
    assert db_pool._last_error == "pool down"


async def test_execute_query_lazily_connects_from_engine_url() -> None:
    # execute_query with only an engine_url must build a pool (self.connect)
    # then fail cleanly when the pool can't open — exercising the lazy-connect
    # branch without a live database.
    driver = SqlDriver(engine_url="postgresql://u:p@localhost/db")
    with patch("mcpg.sql.driver.AsyncConnectionPool", side_effect=RuntimeError("no db")):
        with pytest.raises(ValueError, match="Connection attempt failed"):
            await driver.execute_query("SELECT 1")
    assert driver.is_pool is True


# --------------------------------------------------------------------------
# SafeSqlDriver — execute path (marker + timeout) + static helpers
# --------------------------------------------------------------------------


async def test_safe_execute_prefixes_marker_and_forces_readonly() -> None:
    inner = MagicMock()
    inner.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={"n": 1})])
    safe = SafeSqlDriver(inner)

    await safe.execute_query("SELECT 1")
    inner.execute_query.assert_awaited_once_with("/* crystaldba */ SELECT 1", params=None, force_readonly=True)


async def test_safe_execute_with_timeout_success() -> None:
    inner = MagicMock()
    inner.execute_query = AsyncMock(return_value=None)
    safe = SafeSqlDriver(inner, timeout=5.0)

    await safe.execute_query("SELECT 1")
    inner.execute_query.assert_awaited_once()


async def test_safe_execute_timeout_raises_value_error() -> None:
    async def _slow(*args, **kwargs):
        await asyncio.sleep(1)

    inner = MagicMock()
    inner.execute_query = _slow
    safe = SafeSqlDriver(inner, timeout=0.01)

    with pytest.raises(ValueError, match="timed out"):
        await safe.execute_query("SELECT 1")


async def test_safe_execute_timeout_reraises_other_errors() -> None:
    inner = MagicMock()
    inner.execute_query = AsyncMock(side_effect=RuntimeError("db error"))
    safe = SafeSqlDriver(inner, timeout=5.0)

    with pytest.raises(RuntimeError, match="db error"):
        await safe.execute_query("SELECT 1")


async def test_param_helpers_bind_literals() -> None:
    q = SafeSqlDriver.param_sql_to_query("SELECT * FROM t WHERE id = {}", [7])
    assert "7" in q

    inner = MagicMock()
    inner.execute_query = AsyncMock(return_value=None)
    await SafeSqlDriver.execute_param_query(inner, "SELECT * FROM t WHERE id = {}", [7])
    inner.execute_query.assert_awaited_once()

    inner.execute_query.reset_mock()
    await SafeSqlDriver.execute_param_query(inner, "SELECT 1")
    inner.execute_query.assert_awaited_once_with("SELECT 1")


def test_validate_accepts_constant_like_pattern() -> None:
    # A LIKE against a constant string exercises the LIKE-validation branch and
    # is allowed (the branch only rejects non-constant patterns).
    safe = SafeSqlDriver(MagicMock())
    safe._validate("SELECT * FROM t WHERE name LIKE 'foo%'")  # must not raise
