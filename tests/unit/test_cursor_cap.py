"""Regression guard: CursorManager.open() must not overshoot max_open.

The cap check and the cursor insert happen under two separate lock
acquisitions with the connection `await` in between. Without counting
in-flight opens, N concurrent open() calls all pass the check (none has
inserted yet) and all succeed, exceeding the hard cap. The fix reserves a
slot under the first lock, so only `max_open` opens can be in flight at once.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcpg.cursors import CursorError, CursorManager


class _FakeCursor:
    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, *_a: Any, **_k: Any) -> None:
        return None


class _FakeConn:
    def cursor(self, *_a: Any, **_k: Any) -> _FakeCursor:
        return _FakeCursor()

    async def close(self) -> None:
        return None


async def test_concurrent_open_respects_max_open(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()

    async def _fake_connect(_url: str) -> _FakeConn:
        # Hold the first (reserved) open in flight so the others race the cap
        # check — exactly the interleaving that used to overshoot.
        await gate.wait()
        return _FakeConn()

    monkeypatch.setattr("psycopg.AsyncConnection.connect", _fake_connect)

    mgr = CursorManager("postgresql://u:p@localhost/db", max_open=1)

    async def _release() -> None:
        await asyncio.sleep(0.01)
        gate.set()

    releaser = asyncio.ensure_future(_release())
    results = await asyncio.gather(
        *[mgr.open(MagicMock(), "SELECT 1") for _ in range(3)],
        return_exceptions=True,
    )
    await releaser

    successes = [r for r in results if not isinstance(r, BaseException)]
    rejections = [r for r in results if isinstance(r, CursorError)]
    assert len(successes) == 1  # would be 3 under the TOCTOU bug
    assert len(rejections) == 2
    assert "too many open cursors" in str(rejections[0])


async def test_open_failure_releases_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_url: str) -> _FakeConn:
        raise RuntimeError("connection refused")

    monkeypatch.setattr("psycopg.AsyncConnection.connect", _boom)
    mgr = CursorManager("postgresql://u:p@localhost/db", max_open=1)

    # A failed open must not permanently consume the single slot.
    for _ in range(3):
        with pytest.raises(CursorError):
            await mgr.open(MagicMock(), "SELECT 1")
    assert mgr._reserved == set()  # reservation released each time
