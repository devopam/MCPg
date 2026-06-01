import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcpg.cache import CacheManager, InMemoryCache, RedisCache


@pytest.mark.asyncio
async def test_in_memory_cache_basic() -> None:
    cache = InMemoryCache(maxsize=3)
    await cache.set("k1", "v1", ttl=60)
    assert await cache.get("k1") == "v1"
    assert await cache.get("k2") is None


@pytest.mark.asyncio
async def test_in_memory_cache_ttl() -> None:
    cache = InMemoryCache(maxsize=3)
    
    # Use patch to control time.time()
    with patch("time.time") as mock_time:
        mock_time.return_value = 1000.0
        await cache.set("k1", "v1", ttl=10)
        
        # Still valid at time 1005
        mock_time.return_value = 1005.0
        assert await cache.get("k1") == "v1"
        
        # Expired at time 1011
        mock_time.return_value = 1011.0
        assert await cache.get("k1") is None


@pytest.mark.asyncio
async def test_in_memory_cache_lru() -> None:
    cache = InMemoryCache(maxsize=3)
    await cache.set("k1", "v1", ttl=60)
    await cache.set("k2", "v2", ttl=60)
    await cache.set("k3", "v3", ttl=60)
    
    # Access k1 to make it recently used
    assert await cache.get("k1") == "v1"
    
    # Adding k4 should evict k2 (as k1 was recently accessed, and k2 is now the oldest)
    await cache.set("k4", "v4", ttl=60)
    
    assert await cache.get("k1") == "v1"
    assert await cache.get("k2") is None
    assert await cache.get("k3") == "v3"
    assert await cache.get("k4") == "v4"


@pytest.mark.asyncio
async def test_in_memory_cache_clear() -> None:
    cache = InMemoryCache(maxsize=3)
    await cache.set("k1", "v1", ttl=60)
    await cache.clear()
    assert await cache.get("k1") is None


@pytest.mark.asyncio
async def test_in_memory_cache_concurrency() -> None:
    cache = InMemoryCache(maxsize=10)
    
    # Run multiple concurrent set/get operations
    async def task(i: int) -> None:
        await cache.set(f"key_{i}", f"val_{i}", ttl=10)
        val = await cache.get(f"key_{i}")
        assert val == f"val_{i}"

    tasks = [task(i) for i in range(50)]
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_cache_manager_in_memory() -> None:
    manager = CacheManager(enabled=True, ttl_seconds=300, maxsize=10)
    await manager.start()
    assert manager.is_enabled() is True
    
    await manager.set("key", "val")
    assert await manager.get("key") == "val"
    
    await manager.clear()
    assert await manager.get("key") is None
    await manager.close()


@pytest.mark.asyncio
async def test_cache_manager_disabled() -> None:
    manager = CacheManager(enabled=False)
    await manager.start()
    assert manager.is_enabled() is False
    
    # Operations should be safe NOPs
    await manager.set("key", "val")
    assert await manager.get("key") is None
    await manager.clear()
    await manager.close()


@pytest.mark.asyncio
async def test_cache_manager_redis_fallback() -> None:
    # Set redis_url but make redis import fail
    manager = CacheManager(enabled=True, redis_url="redis://localhost:6379/0")
    
    # Patch the redis import or init inside RedisCache
    with patch("mcpg.cache.RedisCache._init_client", side_effect=ImportError("No module named 'redis'")):
        await manager.start()
        # Should fallback to InMemoryCache successfully
        assert manager.is_enabled() is True
        assert isinstance(manager._driver, InMemoryCache)
        
        await manager.set("key", "val")
        assert await manager.get("key") == "val"
        await manager.close()
