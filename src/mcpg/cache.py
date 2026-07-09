"""Thread-safe, async-safe caching manager for PostgreSQL introspections and summaries."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Protocol, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def cache_namespace(database_url: str | None) -> str:
    """A short, stable, credential-free id for the primary database.

    A single Redis instance can be shared by several MCPg processes (a fleet).
    When those processes serve *different* physical databases, an un-namespaced
    key collides — the logical selector is ``"primary"`` for all of them — and
    one database's cached result is served for another. Namespacing the cache by
    a hash of the primary's ``host:port/dbname`` (never the password) keeps a
    same-database fleet sharing correctly while different-database instances stay
    isolated. Returns ``""`` when the identity can't be derived (keeps the flat
    key space, unchanged behaviour).
    """
    if not database_url:
        return ""
    try:
        import hashlib

        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(database_url)
        ident = f"{info.get('host', '')}:{info.get('port', '')}/{info.get('dbname', '')}"
        if ident == ":/":
            return ""
        return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


class BaseCache(Protocol):
    """Protocol for cache implementations."""

    async def get(self, key: str) -> Any | None:
        """Retrieve key from the cache."""
        ...

    async def set(self, key: str, value: Any, ttl: int) -> None:
        """Set key in the cache with a Time-To-Live in seconds."""
        ...

    async def clear(self) -> None:
        """Clear all entries from the cache."""
        ...

    async def close(self) -> None:
        """Safely release backing connections or resources."""
        ...


class InMemoryCache:
    """Thread-safe, async-safe in-memory cache with TTL and LRU eviction policy."""

    def __init__(self, maxsize: int = 1024) -> None:
        self._maxsize = maxsize
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            if key not in self._store:
                return None
            val, expire_time = self._store[key]
            if time.time() > expire_time:
                del self._store[key]
                return None
            # Move key to end for LRU (Python dict maintains insertion order)
            self._store[key] = self._store.pop(key)
            return val

    async def set(self, key: str, value: Any, ttl: int) -> None:
        async with self._lock:
            expire_time = time.time() + ttl
            if key in self._store:
                self._store.pop(key)
            elif len(self._store) >= self._maxsize:
                # Evict oldest item (first key in dict)
                oldest = next(iter(self._store))
                self._store.pop(oldest)
            self._store[key] = (value, expire_time)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    async def close(self) -> None:
        pass


class RedisCache:
    """Soft-dependency Redis cache wrapper using JSON serialization."""

    def __init__(self, redis_url: str, namespace: str = "") -> None:
        self._redis_url = redis_url
        # Per-database key prefix so a shared Redis serving multiple physical
        # databases (a fleet) never bleeds one database's cache into another.
        self._prefix = f"mcpg:cache:{namespace}:" if namespace else "mcpg:cache:"
        self._client: Any = None
        self._initialized = False

    async def _init_client(self) -> None:
        if self._initialized:
            return
        try:
            import redis.asyncio as aioredis  # type: ignore

            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
            self._initialized = True
        except ImportError:
            logger.error(
                "Redis caching is configured (MCPG_REDIS_URL), but the 'redis' package is not installed. "
                "Please run 'pip install redis' to enable Redis caching. Falling back to InMemoryCache."
            )
            raise

    async def get(self, key: str) -> Any | None:
        await self._init_client()
        if not self._client:
            return None
        try:
            raw = await self._client.get(f"{self._prefix}{key}")
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"Error fetching from Redis cache for key {key!r}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        await self._init_client()
        if not self._client:
            return
        try:
            raw = json.dumps(value)
            await self._client.set(f"{self._prefix}{key}", raw, ex=ttl)
        except Exception as e:
            logger.warning(f"Error setting Redis cache for key {key!r}: {e}")

    async def clear(self) -> None:
        await self._init_client()
        if not self._client:
            return
        try:
            # Delete only keys belonging to THIS database's cache namespace, so
            # one instance's clear doesn't wipe another database's cache on a
            # shared Redis.
            async for key in self._client.scan_iter(f"{self._prefix}*"):
                await self._client.delete(key)
        except Exception as e:
            logger.warning(f"Error clearing Redis cache: {e}")

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.close()
            except Exception as e:
                logger.warning(f"Error closing Redis client: {e}")
            finally:
                self._client = None
                self._initialized = False


class CacheManager:
    """Coordinates cache driver selection, execution gating, and fallback logic."""

    def __init__(
        self,
        enabled: bool = True,
        ttl_seconds: int = 300,
        maxsize: int = 1024,
        redis_url: str | None = None,
        namespace: str = "",
    ) -> None:
        self._enabled = enabled
        self._ttl_seconds = ttl_seconds
        self._maxsize = maxsize
        self._redis_url = redis_url
        self._namespace = namespace
        self._driver: BaseCache | None = None

    async def start(self) -> None:
        """Start the caching driver based on configuration and availability."""
        if not self._enabled:
            return
        if self._redis_url:
            try:
                redis_driver = RedisCache(self._redis_url, namespace=self._namespace)
                await redis_driver._init_client()
                self._driver = redis_driver
                logger.info("Redis cache backend initialized successfully.")
                return
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"Redis cache initialization failed: {e}. Falling back to InMemoryCache.")

        self._driver = InMemoryCache(maxsize=self._maxsize)
        logger.info("InMemoryCache backend initialized successfully.")

    def is_enabled(self) -> bool:
        """Check if the cache manager is active."""
        return self._enabled and self._driver is not None

    async def get(self, key: str) -> Any | None:
        """Retrieve cached value for a key."""
        if not self.is_enabled():
            return None
        assert self._driver is not None
        return await self._driver.get(key)

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Cache a value under a key with optional TTL override."""
        if not self.is_enabled():
            return
        assert self._driver is not None
        ttl_val = ttl if ttl is not None else self._ttl_seconds
        await self._driver.set(key, value, ttl_val)

    async def clear(self) -> None:
        """Flush all cache items."""
        if not self.is_enabled():
            return
        assert self._driver is not None
        await self._driver.clear()
        logger.info("Cache successfully invalidated (cleared).")

    async def close(self) -> None:
        """Safely release cache drivers."""
        if self._driver:
            await self._driver.close()
            self._driver = None
