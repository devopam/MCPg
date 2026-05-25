"""LISTEN/NOTIFY bridge — tool-poll model per ADR-0005.

This module owns the server-lifetime subscription state for the
``subscribe_channel`` / ``poll_notifications`` / ``unsubscribe_channel``
tool family. A single dedicated PostgreSQL connection (separate from
the request pool) holds every active ``LISTEN``; a background
``asyncio.Task`` drains psycopg's notifies generator and fans each
notification out to every subscription on that channel.

Per ADR-0005, subscriptions live in process memory. On server restart
they're lost — agents must re-subscribe. Cross-replica fanout is out
of scope for v1; an operator wanting durability stands up a broker
between PG and their consumers.

The manager is constructed in the server lifespan and torn down on
shutdown. The PG listener connection opens lazily on first subscribe
so an idle server pays nothing for the feature.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# PostgreSQL identifier policy for channel names. Matches the rest of
# MCPg's identifier allowlist (introspection / data_movement / etc).
# Channels go through SQL as ``LISTEN "name"`` so the double-quoted form
# survives reserved words, but the allowlist still refuses anything
# that would need escape handling.
_CHANNEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ListenError(Exception):
    """Raised when a LISTEN/NOTIFY tool call is rejected or fails."""


@dataclass(frozen=True, slots=True)
class Notification:
    """A single notification delivered to a subscription's poll call.

    ``payload`` is the string the publisher passed to ``pg_notify`` (or
    the empty string for a bare ``NOTIFY channel``). ``delivered_at`` is
    a Unix timestamp recorded when the listener loop received the
    notification, not when it was emitted. ``dropped_count`` is non-zero
    only on the FIRST notification returned after a queue overflow —
    subsequent polls reset it to zero, so the count is a running total
    of drops the caller hasn't yet been informed about.
    """

    channel: str
    payload: str
    delivered_at: float
    dropped_count: int = 0


@dataclass(slots=True)
class _Subscription:
    id: str
    channel: str
    queue: asyncio.Queue[Notification]
    dropped: int = 0


class _AsyncConnLike(Protocol):
    """Minimal interface used by :class:`ListenManager`.

    Modelled on psycopg's ``AsyncConnection`` — kept narrow so a fake
    can stand in during unit tests without dragging in psycopg's full
    API surface.
    """

    async def execute(self, sql: str) -> Any: ...

    async def close(self) -> None: ...

    def notifies(self, *, timeout: float | None = None) -> Any: ...


ConnectionFactory = Callable[[], Awaitable[_AsyncConnLike]]


async def _default_connection_factory(database_url: str) -> _AsyncConnLike:
    """The production connection factory — psycopg async, autocommit on.

    LISTEN must run on an autocommit connection so notifications are
    delivered as they arrive rather than at COMMIT time.
    """
    import psycopg

    return await psycopg.AsyncConnection.connect(database_url, autocommit=True)


@dataclass(slots=True)
class ListenManager:
    """Owns LISTEN subscription state for the server's lifetime.

    Public methods are coroutine-safe; an internal :class:`asyncio.Lock`
    serialises subscription bookkeeping so concurrent ``subscribe`` /
    ``unsubscribe`` calls don't race the listener-task setup. The
    listener connection opens lazily on first ``subscribe`` so an idle
    server pays nothing.

    Args:
        database_url: The libpq URL to connect with.
        queue_max: Maximum buffered notifications per subscription;
            overflow drops the oldest and bumps ``dropped`` on the
            subscription so the next poll can report it.
        connection_factory: Override for :func:`_default_connection_factory`
            so unit tests can supply a fake without touching psycopg.
    """

    database_url: str
    queue_max: int = 1000
    connection_factory: ConnectionFactory | None = None
    _subscriptions: dict[str, _Subscription] = field(default_factory=dict)
    _channels: dict[str, set[str]] = field(default_factory=dict)
    _conn: _AsyncConnLike | None = None
    _task: asyncio.Task[None] | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False

    async def subscribe(self, channel: str) -> str:
        """Register a subscription on ``channel`` and return its id.

        The channel name must match ``[A-Za-z_][A-Za-z0-9_]*`` so the
        ``LISTEN "name"`` statement is safe under double-quoting.
        Multiple subscriptions on the same channel share a single
        underlying ``LISTEN``; each gets its own queue.

        Raises:
            ListenError: When the channel name fails validation or the
                manager is closed.
        """
        _check_channel(channel)
        async with self._lock:
            if self._closed:
                raise ListenError("listen manager is closed")
            sub_id = uuid.uuid4().hex
            queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=self.queue_max)
            sub = _Subscription(id=sub_id, channel=channel, queue=queue)
            self._subscriptions[sub_id] = sub
            subs_on_channel = self._channels.setdefault(channel, set())
            first_for_channel = not subs_on_channel
            subs_on_channel.add(sub_id)
            if first_for_channel:
                await self._listen(channel)
            return sub_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscription. Returns ``True`` if it existed.

        When the removed subscription was the last on its channel, an
        ``UNLISTEN`` is issued so PG stops queuing notifications.
        """
        async with self._lock:
            sub = self._subscriptions.pop(subscription_id, None)
            if sub is None:
                return False
            subs_on_channel = self._channels.get(sub.channel)
            if subs_on_channel is not None:
                subs_on_channel.discard(subscription_id)
                if not subs_on_channel:
                    self._channels.pop(sub.channel, None)
                    await self._unlisten(sub.channel)
            return True

    async def poll(self, subscription_id: str, *, timeout_ms: int = 0, max_messages: int = 100) -> list[Notification]:
        """Drain up to ``max_messages`` notifications for a subscription.

        When the queue is empty, waits at most ``timeout_ms`` for the
        first notification (0 = return immediately). Subsequent
        notifications are pulled non-blocking. The result's first entry
        carries ``dropped_count`` equal to the running drop total; the
        counter resets on a successful poll.

        Raises:
            ListenError: When ``subscription_id`` isn't registered, or
                ``max_messages`` / ``timeout_ms`` are out of range.
        """
        if max_messages < 1:
            raise ListenError("max_messages must be at least 1")
        if timeout_ms < 0:
            raise ListenError("timeout_ms must be non-negative")
        sub = self._subscriptions.get(subscription_id)
        if sub is None:
            raise ListenError(f"no such subscription: {subscription_id!r}")
        result: list[Notification] = []
        try:
            if timeout_ms > 0:
                first = await asyncio.wait_for(sub.queue.get(), timeout=timeout_ms / 1000)
                result.append(first)
            while len(result) < max_messages:
                result.append(sub.queue.get_nowait())
        except (TimeoutError, asyncio.QueueEmpty):
            pass
        # Attach the drop count to the first message we hand back, then
        # zero it so a follow-up poll doesn't double-count.
        if result and sub.dropped > 0:
            result[0] = replace(result[0], dropped_count=sub.dropped)
            sub.dropped = 0
        return result

    def active_subscriptions(self) -> list[tuple[str, str]]:
        """Return ``[(subscription_id, channel)]`` for every live sub.

        A read tool can expose this for visibility; the gate on
        creating subscriptions still applies.
        """
        return [(sub_id, sub.channel) for sub_id, sub in self._subscriptions.items()]

    async def close(self) -> None:
        """Tear down the listener task and connection. Idempotent.

        Closing the connection first interrupts ``conn.notifies()`` —
        that generator can block deep inside libpq's socket wait, and a
        bare ``task.cancel()`` doesn't always propagate cleanly through
        it. The connection close raises an exception inside the reader,
        which terminates the loop; we then await the task with a short
        bound so a misbehaving driver can't wedge shutdown.
        """
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            task = self._task
            conn = self._conn
            self._task = None
            self._conn = None
            self._subscriptions.clear()
            self._channels.clear()
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass

    # --- internals --------------------------------------------------

    def _dispatch(self, channel: str, payload: str) -> None:
        """Fan a notification out to every subscription on ``channel``.

        Called from the reader loop. On a full queue, drops the oldest
        message and increments the subscription's drop counter so the
        next poll can surface it via ``dropped_count``.
        """
        msg = Notification(channel=channel, payload=payload, delivered_at=time.time())
        for sub_id in list(self._channels.get(channel, ())):
            sub = self._subscriptions.get(sub_id)
            if sub is None:
                continue
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                try:
                    sub.queue.get_nowait()
                    sub.dropped += 1
                    sub.queue.put_nowait(msg)
                except asyncio.QueueEmpty:
                    # Race with a concurrent poll — try once more.
                    try:
                        sub.queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        sub.dropped += 1

    async def _ensure_connection(self) -> _AsyncConnLike:
        if self._conn is not None:
            return self._conn
        factory: ConnectionFactory
        if self.connection_factory is not None:
            factory = self.connection_factory
        else:
            db_url = self.database_url

            async def _default() -> _AsyncConnLike:
                return await _default_connection_factory(db_url)

            factory = _default
        self._conn = await factory()
        self._task = asyncio.create_task(self._reader_loop(), name="mcpg-listen-reader")
        return self._conn

    async def _listen(self, channel: str) -> None:
        conn = await self._ensure_connection()
        await conn.execute(f'LISTEN "{channel}"')

    async def _unlisten(self, channel: str) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(f'UNLISTEN "{channel}"')
        except Exception:
            # The connection may have died between subscribe and
            # unsubscribe; don't let cleanup raise.
            logger.warning("UNLISTEN %s failed", channel, exc_info=True)

    async def _reader_loop(self) -> None:
        """Drain psycopg's notifies generator until cancelled or the conn dies.

        psycopg's ``conn.notifies()`` holds the connection's async lock
        while it waits on the socket; with ``timeout=None`` it would
        wait forever and block any concurrent ``execute("UNLISTEN ...")``
        the subscribe/unsubscribe path needs. Iterating with a short
        timeout releases the lock between waits so admin commands can
        land, at the cost of a tiny per-tick wake-up overhead.
        """
        assert self._conn is not None
        try:
            while not self._closed:
                async for notify in self._conn.notifies(timeout=0.5):
                    self._dispatch(notify.channel, notify.payload)
                # Brief yield so any execute() waiting on the lock can run.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A dead listener connection shouldn't crash the server.
            # Subscriptions will silently stop receiving notifications;
            # next subscribe() will try to re-open.
            logger.exception("listen reader loop terminated")

    async def __aenter__(self) -> ListenManager:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


def _check_channel(channel: str) -> None:
    if not _CHANNEL_NAME.match(channel):
        raise ListenError(f"invalid channel name: {channel!r}")
