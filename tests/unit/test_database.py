"""Tests for the database connection lifecycle wrapper."""

import pytest

from mcpg._vendor.sql import DbConnPool, SqlDriver
from mcpg.config import load_settings
from mcpg.database import Database, DatabaseError

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


class FakePool:
    """Stand-in for the vendored DbConnPool that records calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.connect_calls = 0
        self.close_calls = 0
        self._is_valid = False

    async def pool_connect(self, connection_url: str | None = None) -> object:
        self.connect_calls += 1
        if self.fail:
            raise ValueError("connection refused")
        self._is_valid = True
        return object()

    async def close(self) -> None:
        self.close_calls += 1
        self._is_valid = False

    @property
    def is_valid(self) -> bool:
        return self._is_valid


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


def test_database_builds_its_own_pool_from_settings() -> None:
    db = Database(_SETTINGS)
    # No pool injected: it should construct one from the settings URL.
    assert isinstance(db._pool, DbConnPool)
