"""Cache-freshness guards for the ``fresh`` bypass and manual invalidation.

MCPg's read cache is TTL-based and invalidated by MCPg's own write/DDL tools,
but it is blind to *out-of-band* schema changes until the TTL expires. Two
escape hatches address that: the per-call ``fresh=True`` argument on the
introspection/advisor read tools, and the ``clear_cache`` tool (a full flush).
These tests exercise ``mcpg.tools._cached_call`` and the cache directly.
"""

from __future__ import annotations

from types import SimpleNamespace

from mcpg.cache import CacheManager
from mcpg.tools import _cached_call


async def _make_ctx() -> tuple[object, CacheManager]:
    cache = CacheManager(enabled=True, ttl_seconds=300, maxsize=64)
    await cache.start()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cache=cache,
                settings=SimpleNamespace(default_role=None),
            )
        )
    )
    return ctx, cache


def _counter_runner(calls: list[int]):
    """A runner whose result changes each time it actually executes."""

    async def _run() -> str:
        calls.append(1)
        return f"result-{len(calls)}"

    return _run


async def test_fresh_bypasses_cached_read_and_refreshes_entry() -> None:
    """``fresh=True`` re-runs the query even on a cache hit, and updates the entry."""
    ctx, cache = await _make_ctx()
    try:
        calls: list[int] = []

        # 1st call populates the cache.
        first = await _cached_call(ctx, "list_constraints", _counter_runner(calls), "public", "orders")
        assert first == "result-1"
        assert calls == [1]

        # 2nd call (no fresh) is served from cache — func does NOT run.
        cached = await _cached_call(ctx, "list_constraints", _counter_runner(calls), "public", "orders")
        assert cached == "result-1"
        assert calls == [1]

        # 3rd call with fresh=True bypasses the read and re-runs.
        refreshed = await _cached_call(ctx, "list_constraints", _counter_runner(calls), "public", "orders", fresh=True)
        assert refreshed == "result-2"
        assert calls == [1, 1]

        # And it overwrote the entry: the next ordinary call sees the fresh value.
        after = await _cached_call(ctx, "list_constraints", _counter_runner(calls), "public", "orders")
        assert after == "result-2"
        assert calls == [1, 1]  # served from cache again
    finally:
        await cache.close()


async def test_fresh_does_not_leak_into_the_cache_key() -> None:
    """A ``fresh`` call and an ordinary call must hit the SAME entry (fresh is not a key arg)."""
    ctx, cache = await _make_ctx()
    try:
        calls: list[int] = []
        await _cached_call(ctx, "describe_table", _counter_runner(calls), "public", "t", fresh=True)
        # Ordinary call with identical key_args is served from the entry the fresh call wrote.
        again = await _cached_call(ctx, "describe_table", _counter_runner(calls), "public", "t")
        assert again == "result-1"
        assert calls == [1]  # the ordinary call did NOT re-run → same key
    finally:
        await cache.close()


async def test_clear_flushes_so_next_call_reruns() -> None:
    """The behavior behind the ``clear_cache`` tool: a full flush forces re-execution."""
    ctx, cache = await _make_ctx()
    try:
        calls: list[int] = []
        await _cached_call(ctx, "recommend_indexes", _counter_runner(calls), 10000)
        assert calls == [1]

        await cache.clear()  # what the clear_cache tool does

        after = await _cached_call(ctx, "recommend_indexes", _counter_runner(calls), 10000)
        assert after == "result-2"
        assert calls == [1, 1]  # re-ran after the flush
    finally:
        await cache.close()
