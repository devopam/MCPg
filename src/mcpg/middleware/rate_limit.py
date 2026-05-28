"""Async-safe Token Bucket Rate Limiter for MCPg tool execution."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from mcpg.tenancy import current_role

# Advanced or computationally heavy tools subject to strict rate limits
HEAVY_TOOLS = frozenset(
    {
        "analyze_workload",
        "generate_test_data",
        "audit_database",
        "export_table",
        "export_query",
    }
)


@dataclass
class TokenBucket:
    """Tracks token levels and last refill times for a bucket."""

    tokens: float
    last_update: float


class RateLimiter:
    """Token Bucket rate limiter partitioned by client/tenant and tool category."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        global_max: int = 60,
        global_window: int = 60,
        heavy_max: int = 5,
        heavy_window: int = 60,
    ) -> None:
        self.enabled = enabled
        self.global_max = global_max
        self.global_window = global_window
        self.heavy_max = heavy_max
        self.heavy_window = heavy_window

        # Storage for client rate-limiting state
        self._buckets: dict[tuple[str, bool], TokenBucket] = {}
        self._lock: asyncio.Lock | None = None

    async def consume(self, tool_name: str) -> bool:
        """Attempt to consume 1 token. Returns True if allowed, False if throttled."""
        if self._lock is None:
            self._lock = asyncio.Lock()

        if not self.enabled:
            return True

        # Fall back to "global" if current_role is not active/configured
        client_id = current_role.get() or "global"
        is_heavy = tool_name in HEAVY_TOOLS

        max_tokens = self.heavy_max if is_heavy else self.global_max
        window = self.heavy_window if is_heavy else self.global_window

        if max_tokens <= 0 or window <= 0:
            return True

        fill_rate = max_tokens / window
        now = time.monotonic()
        key = (client_id, is_heavy)

        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # Initialize fully loaded bucket
                bucket = TokenBucket(tokens=float(max_tokens), last_update=now)
                self._buckets[key] = bucket
            else:
                # Refill tokens based on elapsed time
                elapsed = now - bucket.last_update
                refill = elapsed * fill_rate
                bucket.tokens = min(float(max_tokens), bucket.tokens + refill)
                bucket.last_update = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True

            return False
