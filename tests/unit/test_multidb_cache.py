"""Regression guard: the read cache must be scoped by target database.

A single process-wide cache serves every configured database (primary +
``MCPG_SECONDARY_DATABASE_URLS`` secondaries — roadmap 13.1). If the cache key
omits the ``database`` selector, a read against a secondary collides with the
primary's entry and returns the *primary's* result — the exact
"``audit_database`` on a secondary returned the primary's data" symptom.

These tests exercise ``mcpg.tools._cached_call`` directly with a real
in-memory cache: two calls that differ *only* in ``database`` must run
independently and return distinct results, while a repeat call for the same
database is served from cache.
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


async def test_cached_call_scopes_by_database() -> None:
    """Same key_args, different ``database`` → distinct results, both executed."""
    ctx, cache = await _make_ctx()
    try:
        calls: list[str | None] = []

        def _runner(tag: str):
            async def _run() -> str:
                calls.append(tag)
                return f"result-from-{tag}"

            return _run

        # Primary populates the cache first (this is what a real prior call does).
        primary = await _cached_call(ctx, "audit_database", _runner("primary"), "public", None, database=None)
        # Secondary MUST NOT be served the primary's cached report.
        secondary = await _cached_call(
            ctx, "audit_database", _runner("analytics"), "public", None, database="analytics"
        )

        assert primary == "result-from-primary"
        assert secondary == "result-from-analytics"
        # Both underlying functions actually ran — no cross-database cache hit.
        assert calls == ["primary", "analytics"]
    finally:
        await cache.close()


async def test_cached_call_still_caches_within_one_database() -> None:
    """A repeat call for the SAME database is served from cache (func runs once)."""
    ctx, cache = await _make_ctx()
    try:
        calls: list[int] = []

        async def _run() -> str:
            calls.append(1)
            return "cached-value"

        first = await _cached_call(ctx, "list_tables", _run, "public", database="analytics")
        second = await _cached_call(ctx, "list_tables", _run, "public", database="analytics")

        assert first == second == "cached-value"
        assert calls == [1]  # second call was a cache hit
    finally:
        await cache.close()


async def test_cached_call_passthrough_when_cache_disabled() -> None:
    """With caching off, ``_cached_call`` just runs ``func`` (no key work)."""
    cache = CacheManager(enabled=False)
    await cache.start()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cache=cache,
                settings=SimpleNamespace(default_role=None),
            )
        )
    )
    try:
        calls: list[int] = []

        async def _run() -> str:
            calls.append(1)
            return "live"

        a = await _cached_call(ctx, "list_tables", _run, "public", database="analytics")
        b = await _cached_call(ctx, "list_tables", _run, "public", database="analytics")

        assert a == b == "live"
        assert calls == [1, 1]  # no caching: func runs every time
    finally:
        await cache.close()


async def test_cached_call_none_and_primary_share_one_entry() -> None:
    """``database=None`` and ``database="primary"`` are the same target."""
    ctx, cache = await _make_ctx()
    try:
        calls: list[int] = []

        async def _run() -> str:
            calls.append(1)
            return "primary-value"

        a = await _cached_call(ctx, "list_schemas", _run, True, database=None)
        b = await _cached_call(ctx, "list_schemas", _run, True, database="primary")

        assert a == b == "primary-value"
        assert calls == [1]  # None normalises to "primary" — one shared entry
    finally:
        await cache.close()
