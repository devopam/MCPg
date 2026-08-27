# ruff: noqa: B017
"""Driver / pool tests for the first-party SQL kernel.

Ported from the vendored ``tests/vendor/sql/test_sql_driver.py``, now
exercising :class:`mcpg.sql.SqlDriver` / :class:`mcpg.sql.DbConnPool`.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from mcpg.sql import DbConnPool, SqlDriver


class AsyncContextManagerMock(AsyncMock):
    """A better mock for async context managers."""

    async def __aenter__(self):
        return self.aenter

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_connection():
    """Create a mock for the database connection."""
    connection = MagicMock()
    cursor = AsyncContextManagerMock()
    cursor.aenter = cursor

    cursor_cm = AsyncContextManagerMock()
    cursor_cm.aenter = cursor
    connection.cursor.return_value = cursor_cm

    cursor.description = ["column1", "column2"]
    cursor.fetchall.return_value = [
        {"id": 1, "name": "test1"},
        {"id": 2, "name": "test2"},
    ]
    return connection, cursor


@pytest.fixture
def mock_db_pool():
    """Create a mock for DbConnPool with a mock connection."""
    pool = MagicMock()

    connection = AsyncContextManagerMock()
    connection.aenter = connection

    cursor = AsyncContextManagerMock()
    cursor.aenter = cursor

    cursor_cm = AsyncContextManagerMock()
    cursor_cm.aenter = cursor
    connection.cursor.return_value = cursor_cm

    cursor.description = ["column1", "column2"]
    cursor.fetchall.return_value = [
        {"id": 1, "name": "test1"},
        {"id": 2, "name": "test2"},
    ]

    conn_cm = AsyncContextManagerMock()
    conn_cm.aenter = connection
    pool.connection.return_value = conn_cm

    db_pool = MagicMock(spec=DbConnPool)
    db_pool.pool_connect.return_value = pool
    db_pool._is_valid = True
    return db_pool, connection, cursor


async def _txn_mock_impl(cursor):
    async def mock_impl(connection, query, params, force_readonly):
        await cursor.execute("BEGIN TRANSACTION READ ONLY" if force_readonly else "BEGIN TRANSACTION")
        if params:
            await cursor.execute(query, params)
        else:
            await cursor.execute(query)
        rows = await cursor.fetchall()
        if force_readonly:
            await cursor.execute("ROLLBACK")
        else:
            await cursor.execute("COMMIT")
        return [SqlDriver.RowResult(cells=dict(row)) for row in rows]

    return mock_impl


@pytest.mark.asyncio
async def test_execute_query_readonly_transaction(mock_connection):
    connection, cursor = mock_connection
    driver = SqlDriver(conn=connection)
    driver._execute_with_connection = await _txn_mock_impl(cursor)  # type: ignore[method-assign]

    result = await driver._execute_with_connection(connection, "SELECT * FROM test", None, force_readonly=True)

    assert cursor.execute.call_count >= 3
    assert call("BEGIN TRANSACTION READ ONLY") in cursor.execute.call_args_list
    assert call("ROLLBACK") in cursor.execute.call_args_list
    assert result is not None
    assert len(result) == 2
    assert result[0].cells["id"] == 1
    assert result[1].cells["name"] == "test2"


@pytest.mark.asyncio
async def test_execute_query_writeable_transaction(mock_connection):
    connection, cursor = mock_connection
    driver = SqlDriver(conn=connection)
    driver._execute_with_connection = await _txn_mock_impl(cursor)  # type: ignore[method-assign]

    result = await driver._execute_with_connection(
        connection, "UPDATE test SET name = 'updated'", None, force_readonly=False
    )

    assert call("COMMIT") in cursor.execute.call_args_list
    assert result is not None


@pytest.mark.asyncio
async def test_execute_query_error_handling(mock_connection):
    connection, cursor = mock_connection
    cursor.execute.side_effect = [None, Exception("Query execution failed")]
    driver = SqlDriver(conn=connection)

    async def mock_execute_error(connection, query, params, force_readonly):
        raise Exception("Query execution failed")

    driver._execute_with_connection = mock_execute_error  # type: ignore[method-assign]

    with pytest.raises(Exception) as excinfo:
        await driver._execute_with_connection(connection, "SELECT * FROM nonexistent", None, force_readonly=True)
    assert "Query execution failed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_execute_query_no_results(mock_connection):
    connection, cursor = mock_connection
    cursor.description = None
    driver = SqlDriver(conn=connection)

    async def mock_impl(connection, query, params, force_readonly):
        await cursor.execute("BEGIN TRANSACTION READ ONLY" if force_readonly else "BEGIN TRANSACTION")
        if params:
            await cursor.execute(query, params)
        else:
            await cursor.execute(query)
        if force_readonly:
            await cursor.execute("ROLLBACK")
        else:
            await cursor.execute("COMMIT")
        return None

    driver._execute_with_connection = mock_impl  # type: ignore[method-assign]

    result = await driver._execute_with_connection(connection, "DELETE FROM test", None, force_readonly=False)
    assert result is None
    assert call("COMMIT") in cursor.execute.call_args_list


@pytest.mark.asyncio
async def test_execute_query_with_params(mock_connection):
    connection, cursor = mock_connection
    driver = SqlDriver(conn=connection)
    driver._execute_with_connection = await _txn_mock_impl(cursor)  # type: ignore[method-assign]

    await driver._execute_with_connection(connection, "SELECT * FROM test WHERE id = %s", [1], force_readonly=True)
    assert call("SELECT * FROM test WHERE id = %s", [1]) in cursor.execute.call_args_list


@pytest.mark.asyncio
async def test_execute_query_from_pool(mock_db_pool):
    db_pool, _connection, _cursor = mock_db_pool

    async def mock_pool_execute(*args, **kwargs):
        return [
            SqlDriver.RowResult(cells={"id": 1, "name": "test1"}),
            SqlDriver.RowResult(cells={"id": 2, "name": "test2"}),
        ]

    driver = SqlDriver(conn=db_pool)
    driver.execute_query = mock_pool_execute  # type: ignore[method-assign]

    result = await driver.execute_query("SELECT * FROM test")
    assert result is not None
    assert len(result) == 2
    assert result[0].cells["id"] == 1
    assert result[1].cells["name"] == "test2"


@pytest.mark.asyncio
async def test_connection_error_marks_pool_invalid(mock_db_pool):
    db_pool, _connection, _cursor = mock_db_pool
    db_pool.pool_connect.side_effect = Exception("Connection failed")
    driver = SqlDriver(conn=db_pool)

    with pytest.raises(Exception):
        await driver.execute_query("SELECT * FROM test")

    assert db_pool._is_valid is False
    assert isinstance(db_pool._last_error, str)


@pytest.mark.asyncio
async def test_execute_query_error_log_redacts_sql_literal_secret(caplog):
    """Regression test for CodeQL alert #5 (py/clear-text-logging-sensitive-data).

    ``_execute_with_connection``'s error-path ``logger.error`` call must
    never leak a literal secret embedded in SQL-syntax password clauses.
    The real traced call chain: ``mcpg.redis_fdw.create_redis_user_mapping``
    resolves a real value from the configured secrets backend
    (``mcpg.secrets.build_secrets_provider`` / ``SecretsProvider.get``) and
    interpolates it into a ``CREATE USER MAPPING ... OPTIONS (password
    '...')`` statement (no ``=`` sign — the DSN-style
    ``obfuscate_password`` patterns wouldn't have caught this before the
    fix). If that ``execute_query`` call ever raises, the query text must
    reach the log redacted, not verbatim.

    This test exercises the real ``_execute_with_connection`` (not a
    monkey-patched stand-in like the other tests in this file), so it
    needs its own async-context-manager mock for ``connection.cursor()``:
    the shared ``mock_connection`` fixture's ``AsyncContextManagerMock``
    subclass overrides ``__aenter__``/``__aexit__`` as plain methods, but
    on current Python/``unittest.mock`` that override is not honored by
    the ``async with`` protocol on an ``AsyncMock`` instance -- it silently
    falls back to an auto-generated ``__aenter__`` whose return value is
    an unrelated mock, disconnected from any ``side_effect`` configured on
    the fixture's cursor. That previously sent this test into a genuine
    infinite loop (``while cursor.nextset(): pass`` looping forever on an
    always-truthy, never-awaited coroutine returned by the auto mock's
    ``nextset()``) instead of raising. Configuring ``__aenter__`` /
    ``__aexit__`` as explicit ``AsyncMock`` attributes (the currently
    correct idiom) avoids that trap.

    Also attaches ``caplog.handler`` directly to the ``mcpg.sql.driver``
    logger rather than relying on ``caplog.at_level`` + root-logger
    propagation: at least one other test in this suite (observed:
    ``tests/unit/test_obs_logging.py``'s ``setup_logging``/
    ``configure_log_format`` coverage) mutates the ancestor ``mcpg``
    logger's ``propagate``/handlers as global ``logging`` module state
    and doesn't always restore it, which silently breaks
    ``caplog``-via-propagation for any ``mcpg.*`` logger in tests that
    happen to run afterward in the same session (this test passed in
    isolation but failed — ``caplog.text`` empty despite the redacted
    line genuinely being emitted, visible in "Captured stderr call" —
    when run as part of the full suite). Attaching the handler directly
    makes this test's assertions independent of that ordering.
    """
    secret = "sup3r-sekret-token-xyz"
    query = f"CREATE USER MAPPING IF NOT EXISTS FOR PUBLIC SERVER \"redis_primary\" OPTIONS (password '{secret}')"

    cursor = AsyncMock()
    cursor.execute.side_effect = Exception("boom")

    cursor_cm = MagicMock()
    cursor_cm.__aenter__ = AsyncMock(return_value=cursor)
    cursor_cm.__aexit__ = AsyncMock(return_value=False)

    connection = MagicMock()
    connection.cursor = MagicMock(return_value=cursor_cm)

    driver = SqlDriver(conn=connection)

    driver_logger = logging.getLogger("mcpg.sql.driver")
    driver_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger="mcpg.sql.driver"), pytest.raises(Exception, match="boom"):
            await driver._execute_with_connection(connection, query, None, force_readonly=False)
    finally:
        driver_logger.removeHandler(caplog.handler)

    assert secret not in caplog.text
    assert "****" in caplog.text


@pytest.mark.asyncio
async def test_engine_url_connection():
    db_pool = MagicMock(spec=DbConnPool)
    with patch("mcpg.sql.driver.DbConnPool", return_value=db_pool):
        driver = SqlDriver(engine_url="postgresql://user:pass@localhost/db")
        driver.connect()
        assert driver.is_pool is True
        assert driver.conn is not None
