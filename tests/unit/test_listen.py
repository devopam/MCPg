"""Tests for the LISTEN/NOTIFY tool-poll bridge (ADR-0005)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.listen import ListenError, ListenManager, Notification
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})
_UNRESTRICTED_LISTEN = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_LISTEN": "true",
    }
)
_UNRESTRICTED_NO_LISTEN = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
    }
)


# --- Fake connection that pretends to be psycopg AsyncConnection ---------


@dataclass(slots=True)
class _FakeNotify:
    channel: str
    payload: str


class _FakeConn:
    """In-memory stand-in for psycopg.AsyncConnection used by ListenManager.

    Records every executed statement and exposes a ``feed()`` helper that
    pushes a notification into the async iterator the reader loop is
    consuming.
    """

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False
        self._inbox: asyncio.Queue[_FakeNotify] = asyncio.Queue()

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)

    async def close(self) -> None:
        self.closed = True
        # Wake any reader so the async generator terminates cleanly.
        await self._inbox.put(_FakeNotify(channel="__close__", payload=""))

    def notifies(self, *, timeout: float | None = None) -> AsyncIterator[_FakeNotify]:
        # ``timeout`` mirrors psycopg's API: when set, the iterator
        # exits after ``timeout`` seconds of silence so the reader loop
        # can periodically check ``_closed`` and release any lock.
        async def _gen() -> AsyncIterator[_FakeNotify]:
            while True:
                try:
                    if timeout is None:
                        msg = await self._inbox.get()
                    else:
                        msg = await asyncio.wait_for(self._inbox.get(), timeout=timeout)
                except TimeoutError:
                    return
                if msg.channel == "__close__":
                    return
                yield msg

        return _gen()

    async def feed(self, channel: str, payload: str) -> None:
        await self._inbox.put(_FakeNotify(channel=channel, payload=payload))


def _manager_with_fake_conn() -> tuple[ListenManager, _FakeConn]:
    conn = _FakeConn()

    async def factory() -> _FakeConn:
        return conn

    manager = ListenManager(database_url="postgresql:///x", connection_factory=factory)
    return manager, conn


# --- subscribe / unsubscribe / poll lifecycle ----------------------------


async def test_subscribe_returns_a_unique_id_and_runs_listen_on_first_sub() -> None:
    manager, conn = _manager_with_fake_conn()
    try:
        sub_id = await manager.subscribe("orders")
        assert isinstance(sub_id, str) and len(sub_id) >= 8
        assert any('LISTEN "orders"' in sql for sql in conn.executed)
    finally:
        await manager.close()


async def test_second_subscribe_on_same_channel_does_not_re_listen() -> None:
    manager, conn = _manager_with_fake_conn()
    try:
        await manager.subscribe("orders")
        await manager.subscribe("orders")
        # Only the first subscribe issues LISTEN — the second reuses it.
        listens = [sql for sql in conn.executed if "LISTEN" in sql and "UNLISTEN" not in sql]
        assert len(listens) == 1
    finally:
        await manager.close()


async def test_subscribe_rejects_unsafe_channel_names() -> None:
    manager, _ = _manager_with_fake_conn()
    try:
        with pytest.raises(ListenError, match="invalid channel name"):
            await manager.subscribe('orders"; DROP TABLE x; --')
        with pytest.raises(ListenError, match="invalid channel name"):
            await manager.subscribe("with space")
    finally:
        await manager.close()


async def test_unsubscribe_drops_listen_when_no_subscriptions_remain() -> None:
    manager, conn = _manager_with_fake_conn()
    try:
        sub_id = await manager.subscribe("orders")
        removed = await manager.unsubscribe(sub_id)
        assert removed is True
        assert any('UNLISTEN "orders"' in sql for sql in conn.executed)
    finally:
        await manager.close()


async def test_unsubscribe_keeps_listen_while_other_subs_on_channel_remain() -> None:
    manager, conn = _manager_with_fake_conn()
    try:
        sub_a = await manager.subscribe("orders")
        await manager.subscribe("orders")
        await manager.unsubscribe(sub_a)
        # No UNLISTEN should have fired — the channel still has a subscriber.
        assert not any("UNLISTEN" in sql for sql in conn.executed)
    finally:
        await manager.close()


async def test_unsubscribe_unknown_id_returns_false() -> None:
    manager, _ = _manager_with_fake_conn()
    try:
        assert await manager.unsubscribe("not-a-real-id") is False
    finally:
        await manager.close()


async def test_poll_unknown_subscription_raises() -> None:
    manager, _ = _manager_with_fake_conn()
    try:
        with pytest.raises(ListenError, match="no such subscription"):
            await manager.poll("missing", timeout_ms=0)
    finally:
        await manager.close()


async def test_poll_returns_empty_when_no_messages_and_no_wait() -> None:
    manager, _ = _manager_with_fake_conn()
    try:
        sub_id = await manager.subscribe("orders")
        assert await manager.poll(sub_id, timeout_ms=0) == []
    finally:
        await manager.close()


async def test_poll_returns_messages_pushed_by_the_reader_loop() -> None:
    manager, conn = _manager_with_fake_conn()
    try:
        sub_id = await manager.subscribe("orders")
        await conn.feed("orders", "hello")
        await conn.feed("orders", "world")
        # Give the reader loop a tick to drain the inbox.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        msgs = await manager.poll(sub_id, timeout_ms=200, max_messages=5)
        payloads = [m.payload for m in msgs]
        assert payloads == ["hello", "world"]
        assert all(isinstance(m, Notification) for m in msgs)
        assert all(m.channel == "orders" for m in msgs)
    finally:
        await manager.close()


async def test_poll_waits_for_first_message_when_timeout_is_set() -> None:
    manager, conn = _manager_with_fake_conn()
    try:
        sub_id = await manager.subscribe("orders")

        # Push a message a moment after the poll starts.
        async def _later() -> None:
            await asyncio.sleep(0.02)
            await conn.feed("orders", "ping")

        producer = asyncio.create_task(_later())
        msgs = await manager.poll(sub_id, timeout_ms=500)
        await producer
        assert [m.payload for m in msgs] == ["ping"]
    finally:
        await manager.close()


async def test_poll_returns_empty_after_timeout_expires_without_messages() -> None:
    manager, _ = _manager_with_fake_conn()
    try:
        sub_id = await manager.subscribe("orders")
        msgs = await manager.poll(sub_id, timeout_ms=20)
        assert msgs == []
    finally:
        await manager.close()


async def test_poll_caps_at_max_messages() -> None:
    manager, conn = _manager_with_fake_conn()
    try:
        sub_id = await manager.subscribe("orders")
        for i in range(10):
            await conn.feed("orders", f"msg{i}")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        msgs = await manager.poll(sub_id, timeout_ms=200, max_messages=3)
        assert len(msgs) == 3
        # The remainder is still in the queue and can be drained on the next poll.
        rest = await manager.poll(sub_id, timeout_ms=200, max_messages=10)
        assert len(rest) == 7
    finally:
        await manager.close()


async def test_poll_validates_max_messages_and_timeout() -> None:
    manager, _ = _manager_with_fake_conn()
    try:
        sub_id = await manager.subscribe("orders")
        with pytest.raises(ListenError, match="max_messages"):
            await manager.poll(sub_id, max_messages=0)
        with pytest.raises(ListenError, match="timeout_ms"):
            await manager.poll(sub_id, timeout_ms=-1)
    finally:
        await manager.close()


# --- queue overflow / dropped_count --------------------------------------


async def test_queue_overflow_drops_oldest_and_surfaces_dropped_count() -> None:
    conn = _FakeConn()

    async def factory() -> _FakeConn:
        return conn

    manager = ListenManager(database_url="postgresql:///x", queue_max=3, connection_factory=factory)
    try:
        sub_id = await manager.subscribe("orders")
        # Push 5 messages into a queue of size 3 — the oldest 2 get dropped.
        for i in range(5):
            await conn.feed("orders", f"m{i}")
        # Let the reader fan everything out.
        for _ in range(10):
            await asyncio.sleep(0)

        msgs = await manager.poll(sub_id, timeout_ms=100, max_messages=10)
        # The 3 newest survived.
        assert [m.payload for m in msgs] == ["m2", "m3", "m4"]
        # The first returned message reports the running drop count (2).
        assert msgs[0].dropped_count == 2
        # Subsequent messages don't double-report the drops.
        assert all(m.dropped_count == 0 for m in msgs[1:])

        # A follow-up poll's first message starts clean again.
        await conn.feed("orders", "later")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        more = await manager.poll(sub_id, timeout_ms=100)
        assert more[0].dropped_count == 0
    finally:
        await manager.close()


# --- closure semantics ---------------------------------------------------


async def test_close_is_idempotent_and_cancels_the_reader_task() -> None:
    manager, conn = _manager_with_fake_conn()
    await manager.subscribe("orders")
    await manager.close()
    await manager.close()  # second call is a no-op
    assert conn.closed is True


async def test_subscribe_after_close_raises() -> None:
    manager, _ = _manager_with_fake_conn()
    await manager.close()
    with pytest.raises(ListenError, match="closed"):
        await manager.subscribe("orders")


async def test_active_subscriptions_returns_open_ids_and_channels() -> None:
    manager, _ = _manager_with_fake_conn()
    try:
        sub_a = await manager.subscribe("orders")
        sub_b = await manager.subscribe("audit")
        active = dict(manager.active_subscriptions())
        assert active == {sub_a: "orders", sub_b: "audit"}
        await manager.unsubscribe(sub_a)
        assert dict(manager.active_subscriptions()) == {sub_b: "audit"}
    finally:
        await manager.close()


# --- tool registration gate ---------------------------------------------


def _server_with_fake_lm(settings: Any) -> Any:
    conn = _FakeConn()

    async def factory() -> _FakeConn:
        return conn

    lm = ListenManager(database_url=settings.database_url, connection_factory=factory)
    return create_server(settings, database=FakeDatabase(FakeDriver()), listen_manager=lm), lm


async def test_listen_tools_hidden_in_read_only_mode() -> None:
    server, lm = _server_with_fake_lm(_SETTINGS)
    try:
        async with create_connected_server_and_client_session(server) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "subscribe_channel" not in listed
        assert "poll_notifications" not in listed
        assert "unsubscribe_channel" not in listed
    finally:
        await lm.close()


async def test_listen_tools_hidden_in_unrestricted_without_allow_listen() -> None:
    # Same defence-in-depth pattern as MCPG_ALLOW_DDL / MCPG_ALLOW_SHELL —
    # unrestricted alone does not expose subscriptions.
    server, lm = _server_with_fake_lm(_UNRESTRICTED_NO_LISTEN)
    try:
        async with create_connected_server_and_client_session(server) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "subscribe_channel" not in listed
    finally:
        await lm.close()


async def test_listen_tools_registered_and_callable_with_allow_listen() -> None:
    server, lm = _server_with_fake_lm(_UNRESTRICTED_LISTEN)
    try:
        async with create_connected_server_and_client_session(server) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
            assert {
                "subscribe_channel",
                "poll_notifications",
                "unsubscribe_channel",
                "list_notification_subscriptions",
            } <= listed

            # subscribe → list → poll → unsubscribe round-trip via the MCP wire.
            sub_result = await client.call_tool("subscribe_channel", {"channel": "orders"})
            assert sub_result.isError is False
            assert sub_result.structuredContent is not None
            sub_id = sub_result.structuredContent["subscription_id"]
            assert sub_result.structuredContent["channel"] == "orders"

            listed_subs = await client.call_tool("list_notification_subscriptions", {})
            assert listed_subs.isError is False
            payload = listed_subs.structuredContent
            assert payload is not None
            # Tool returns a list; FastMCP wraps it under "result".
            entries = payload.get("result") if isinstance(payload, dict) else payload
            assert any(entry["subscription_id"] == sub_id for entry in entries)

            poll_result = await client.call_tool("poll_notifications", {"subscription_id": sub_id})
            assert poll_result.isError is False

            unsub = await client.call_tool("unsubscribe_channel", {"subscription_id": sub_id})
            assert unsub.isError is False
            assert unsub.structuredContent is not None
            assert unsub.structuredContent["removed"] is True
    finally:
        await lm.close()
