"""Tests for the database connection lifecycle wrapper."""

import pytest
from _fakes import FakePool

from mcpg._vendor.sql import DbConnPool, SqlDriver
from mcpg.config import load_settings
from mcpg.database import Database, DatabaseError

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_connect_opens_pool_and_marks_connected() -> None:
    pool = FakePool()
    db = Database(_SETTINGS, pool=pool)  # type: ignore[arg-type]

    assert db.is_connected is False
    await db.connect()

    assert pool.connect_calls == 1
    assert db.is_connected is True


async def test_connect_failure_raises_database_error_and_stays_disconnected() -> None:
    db = Database(_SETTINGS, pool=FakePool(fail=True))  # type: ignore[arg-type]

    with pytest.raises(DatabaseError, match="could not connect"):
        await db.connect()
    assert db.is_connected is False


async def test_close_closes_pool_and_marks_disconnected() -> None:
    pool = FakePool()
    db = Database(_SETTINGS, pool=pool)  # type: ignore[arg-type]
    await db.connect()

    await db.close()

    assert pool.close_calls == 1
    assert db.is_connected is False


async def test_async_context_manager_connects_and_closes() -> None:
    pool = FakePool()

    async with Database(_SETTINGS, pool=pool) as db:  # type: ignore[arg-type]
        assert db.is_connected is True

    assert pool.close_calls == 1


async def test_context_manager_closes_even_when_body_raises() -> None:
    pool = FakePool()

    with pytest.raises(RuntimeError):
        async with Database(_SETTINGS, pool=pool):  # type: ignore[arg-type]
            raise RuntimeError("boom")

    assert pool.close_calls == 1


async def test_driver_before_connect_raises() -> None:
    db = Database(_SETTINGS, pool=FakePool())  # type: ignore[arg-type]

    with pytest.raises(DatabaseError, match="not connected"):
        db.driver()


async def test_driver_after_connect_returns_a_sql_driver() -> None:
    db = Database(_SETTINGS, pool=FakePool())  # type: ignore[arg-type]
    await db.connect()

    assert isinstance(db.driver(), SqlDriver)


class _FakeConnection:
    """Stand-in for an async psycopg connection used by run_unmanaged."""

    def __init__(self) -> None:
        self.autocommit_calls: list[bool] = []
        self.executed: list[str] = []

    async def set_autocommit(self, value: bool) -> None:
        self.autocommit_calls.append(value)

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)


class _FakeConnectionContext:
    """Async context manager yielding a fake connection, like pool.connection()."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, *exc: object) -> None:
        return None


class _UnmanagedPool(FakePool):
    """FakePool whose pool_connect yields a handle with a fake connection."""

    def __init__(self, connection: _FakeConnection) -> None:
        super().__init__()
        self._connection = connection

    async def pool_connect(self, connection_url: str | None = None) -> object:
        await super().pool_connect(connection_url)
        return self

    def connection(self) -> _FakeConnectionContext:
        return _FakeConnectionContext(self._connection)


async def test_run_unmanaged_executes_on_an_autocommit_connection() -> None:
    connection = _FakeConnection()
    db = Database(_SETTINGS, pool=_UnmanagedPool(connection))  # type: ignore[arg-type]
    await db.connect()

    await db.run_unmanaged('VACUUM "app"."widget"')

    assert connection.executed == ['VACUUM "app"."widget"']
    # Autocommit is switched on for the statement, then restored.
    assert connection.autocommit_calls == [True, False]


async def test_run_unmanaged_before_connect_raises() -> None:
    db = Database(_SETTINGS, pool=FakePool())  # type: ignore[arg-type]

    with pytest.raises(DatabaseError, match="not connected"):
        await db.run_unmanaged("VACUUM x")


def test_database_builds_its_own_pool_from_settings() -> None:
    db = Database(_SETTINGS)
    # No pool injected: it should construct one from the settings URL.
    assert isinstance(db._pool, DbConnPool)


def test_database_applies_configured_pool_sizes() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_POOL_MIN_SIZE": "2",
            "MCPG_POOL_MAX_SIZE": "12",
        }
    )

    db = Database(settings)

    assert db._pool.min_size == 2
    assert db._pool.max_size == 12
