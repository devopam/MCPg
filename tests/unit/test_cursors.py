"""Tests for the server-side cursor manager (Phase 3.1).

These tests cover input validation and lifecycle semantics without
opening a real database connection — the path that talks to psycopg
is exercised by the integration suite.
"""

from __future__ import annotations

import pytest
from _fakes import FakeDriver

from mcpg.cursors import (
    DEFAULT_FETCH_BATCH,
    HARD_FETCH_BATCH,
    CursorError,
    CursorManager,
)


def _manager() -> CursorManager:
    # Connection string is never reached — input-validation tests fail
    # before psycopg.AsyncConnection.connect would be called.
    return CursorManager(database_url="postgresql://u:p@localhost/db", max_open=2, ttl_seconds=0.1)


async def test_open_rejects_writes_via_safety_allowlist() -> None:
    manager = _manager()
    with pytest.raises(CursorError, match="rejected"):
        await manager.open(FakeDriver(), "DROP TABLE users")


async def test_open_rejects_unparseable_sql() -> None:
    manager = _manager()
    with pytest.raises(CursorError, match="rejected"):
        await manager.open(FakeDriver(), "this is not sql ;;;;")


async def test_fetch_rejects_zero_batch_size() -> None:
    manager = _manager()
    with pytest.raises(CursorError, match="batch_size"):
        await manager.fetch("mcpg_doesnotmatter", batch_size=0)


async def test_fetch_rejects_batch_size_above_hard_cap() -> None:
    manager = _manager()
    with pytest.raises(CursorError, match="hard cap"):
        await manager.fetch("mcpg_doesnotmatter", batch_size=HARD_FETCH_BATCH + 1)


async def test_fetch_unknown_cursor_id_raises() -> None:
    manager = _manager()
    with pytest.raises(CursorError, match="unknown cursor_id"):
        await manager.fetch("mcpg_does_not_exist")


async def test_close_unknown_cursor_returns_false() -> None:
    """Idempotent close — the agent doesn't need to special-case
    closing a cursor that was already swept by the TTL or never
    existed."""
    manager = _manager()
    assert await manager.close("mcpg_does_not_exist") is False


async def test_list_open_returns_empty_when_no_cursors_have_been_opened() -> None:
    manager = _manager()
    assert await manager.list_open() == []


async def test_default_fetch_batch_is_under_the_hard_cap() -> None:
    # Pin the relationship — a future change to the default must not
    # exceed the hard cap, or every call would error.
    assert DEFAULT_FETCH_BATCH < HARD_FETCH_BATCH


async def test_active_cursor_carries_an_asyncio_lock_for_serialised_access() -> None:
    """Regression for the concurrent-fetch protocol-corruption hazard.

    psycopg AsyncConnection is not safe for concurrent task access;
    each ``_ActiveCursor`` carries its own ``asyncio.Lock`` so the
    manager can serialise FETCH and CLOSE on the same cursor.
    """
    import asyncio

    from mcpg.cursors import _ActiveCursor

    # We can't easily construct a real _ActiveCursor without a live
    # psycopg connection. Use a sentinel for the connection field —
    # the lock attribute is what we're asserting on.
    cursor = _ActiveCursor(
        cursor_id="mcpg_smoke",
        sql="SELECT 1",
        connection=object(),  # type: ignore[arg-type]
    )
    assert isinstance(cursor.use_lock, asyncio.Lock)
    # Distinct cursors must each get their own lock — defaulting to a
    # shared class-level instance would silently re-serialise across
    # unrelated cursors.
    other = _ActiveCursor(
        cursor_id="mcpg_other",
        sql="SELECT 1",
        connection=object(),  # type: ignore[arg-type]
    )
    assert cursor.use_lock is not other.use_lock
