"""Tests for the transactional DDL dry-run (roadmap 2.8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from mcpg.ddl_dryrun import DdlDryRunResult, dry_run_ddl


class _FakeLockError(Exception):
    """Stand-in for a psycopg lock_timeout error carrying a sqlstate."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("canceling statement due to lock timeout")
        self.sqlstate = sqlstate


class FakeCursor:
    """Records executed SQL and serves canned fetch results per query.

    ``ddl_error`` (if set) is raised when the DDL statement runs — used to
    exercise the lock-timeout / error paths.
    """

    def __init__(
        self,
        *,
        ddl_sql: str,
        locks: list[tuple[str, str, bool, str | None]],
        wal_now: str = "0/200",
        wal_baseline: str = "0/100",
        wal_bytes: int = 256,
        ddl_error: Exception | None = None,
    ) -> None:
        self.executed: list[str] = []
        self._ddl_sql = ddl_sql
        self._locks = locks
        self._wal_now = wal_now
        self._wal_baseline = wal_baseline
        self._wal_bytes = wal_bytes
        self._ddl_error = ddl_error
        self._last: str = ""
        self._lsn_calls = 0

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)
        self._last = sql
        if sql == self._ddl_sql and self._ddl_error is not None:
            raise self._ddl_error

    async def fetchone(self) -> tuple[Any, ...]:
        if "pg_current_wal_lsn" in self._last:
            self._lsn_calls += 1
            # First LSN read is the baseline, second is "now".
            return (self._wal_baseline if self._lsn_calls == 1 else self._wal_now,)
        if "pg_wal_lsn_diff" in self._last:
            return (self._wal_bytes,)
        return (None,)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        if "pg_locks" in self._last:
            return list(self._locks)
        return []


class FakeConnection:
    """Stand-in psycopg connection that yields a FakeCursor and records rollback."""

    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.rolled_back = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    async def rollback(self) -> None:
        self.rolled_back = True


def _acquirer(conn: FakeConnection):
    @asynccontextmanager
    async def _acquire() -> AsyncIterator[FakeConnection]:
        yield conn

    return _acquire


async def test_dry_run_rejects_concurrently_as_ineligible() -> None:
    # No connection needed — rejected before acquisition.
    result = await dry_run_ddl(
        database=None,  # type: ignore[arg-type]
        ddl_sql="CREATE INDEX CONCURRENTLY idx ON t (a)",
    )

    assert result == DdlDryRunResult(
        eligible=False,
        executed=False,
        rolled_back=False,
        lock_timed_out=False,
        locks=[],
        max_lock_mode=None,
        duration_ms=None,
        wal_bytes=None,
        error=None,
        detail=result.detail,
    )
    assert "CONCURRENTLY" in result.detail


async def test_dry_run_rejects_empty_sql() -> None:
    result = await dry_run_ddl(database=None, ddl_sql="   ")  # type: ignore[arg-type]

    assert result.eligible is False
    assert "must not be empty" in result.detail


async def test_dry_run_executes_alter_then_rolls_back() -> None:
    ddl = "ALTER TABLE t ADD COLUMN c int"
    cursor = FakeCursor(
        ddl_sql=ddl,
        locks=[
            ("relation", "AccessExclusiveLock", True, "t"),
            ("relation", "AccessShareLock", True, "t"),
        ],
        wal_bytes=512,
    )
    conn = FakeConnection(cursor)

    result = await dry_run_ddl(database=None, ddl_sql=ddl, acquire=_acquirer(conn))  # type: ignore[arg-type]

    assert result.eligible is True
    assert result.executed is True
    assert result.rolled_back is True
    assert conn.rolled_back is True
    assert result.lock_timed_out is False
    assert result.max_lock_mode == "AccessExclusiveLock"
    assert len(result.locks) == 2
    assert result.wal_bytes == 512
    assert result.duration_ms is not None
    # The DDL ran, a lock_timeout was set, and the statement was issued.
    assert any("SET LOCAL lock_timeout" in s for s in cursor.executed)
    assert ddl in cursor.executed


async def test_dry_run_handles_lock_timeout_gracefully() -> None:
    ddl = "ALTER TABLE busy ADD COLUMN c int"
    cursor = FakeCursor(
        ddl_sql=ddl,
        locks=[],
        ddl_error=_FakeLockError("55P03"),
    )
    conn = FakeConnection(cursor)

    result = await dry_run_ddl(
        database=None,  # type: ignore[arg-type]
        ddl_sql=ddl,
        lock_timeout_ms=250,
        acquire=_acquirer(conn),
    )

    assert result.eligible is True
    assert result.executed is False
    assert result.lock_timed_out is True
    assert result.error is None
    assert result.rolled_back is True
    assert conn.rolled_back is True
    assert "lock_timeout" in result.detail


async def test_dry_run_captures_other_errors_as_structured_result() -> None:
    ddl = "ALTER TABLE t ADD COLUMN c nonsense_type"
    cursor = FakeCursor(ddl_sql=ddl, locks=[], ddl_error=ValueError("type does not exist"))
    conn = FakeConnection(cursor)

    result = await dry_run_ddl(database=None, ddl_sql=ddl, acquire=_acquirer(conn))  # type: ignore[arg-type]

    assert result.eligible is True
    assert result.executed is False
    assert result.lock_timed_out is False
    assert result.error is not None
    assert "type does not exist" in result.error
    assert result.rolled_back is True


async def test_dry_run_never_raises_even_when_cursor_fails() -> None:
    class ExplodingConn:
        def __init__(self) -> None:
            self.rolled_back = False

        def cursor(self) -> Any:
            raise RuntimeError("connection lost")

        async def rollback(self) -> None:
            self.rolled_back = True

    conn = ExplodingConn()

    result = await dry_run_ddl(
        database=None,  # type: ignore[arg-type]
        ddl_sql="ALTER TABLE t ADD COLUMN c int",
        acquire=_acquirer(conn),  # type: ignore[arg-type]
    )

    assert result.eligible is True
    assert result.executed is False
    assert result.error is not None
    assert result.rolled_back is True


@pytest.mark.parametrize(
    "ineligible_sql",
    ["VACUUM t", "ALTER SYSTEM SET work_mem = '64MB'", "REINDEX INDEX CONCURRENTLY idx"],
)
async def test_dry_run_rejects_non_transactional_statements(ineligible_sql: str) -> None:
    result = await dry_run_ddl(database=None, ddl_sql=ineligible_sql)  # type: ignore[arg-type]

    assert result.eligible is False
