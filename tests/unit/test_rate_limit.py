"""Unit tests for tool execution rate limiting middleware."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session
from test_health import _HEALTHY_ROUTES

from mcpg.config import load_settings
from mcpg.middleware.rate_limit import RateLimiter
from mcpg.server import create_server
from mcpg.tenancy import current_role

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


@pytest.mark.anyio
async def test_rate_limiter_disabled_allows_unlimited() -> None:
    limiter = RateLimiter(enabled=False, global_max=2, global_window=60)
    for _ in range(5):
        assert await limiter.consume("summarize_table") is True


@pytest.mark.anyio
async def test_rate_limiter_throttles_after_limit() -> None:
    # 2 requests per 60 seconds
    limiter = RateLimiter(enabled=True, global_max=2, global_window=60)

    # First two calls should succeed
    assert await limiter.consume("summarize_table") is True
    assert await limiter.consume("summarize_table") is True

    # Third call should be throttled
    assert await limiter.consume("summarize_table") is False


@pytest.mark.anyio
async def test_rate_limiter_heavy_tool_throttling() -> None:
    # Heavy tools capped at 1 request per 60s
    limiter = RateLimiter(
        enabled=True,
        global_max=10,
        global_window=60,
        heavy_max=1,
        heavy_window=60,
    )

    # Calling heavy tool once succeeds
    assert await limiter.consume("analyze_workload") is True

    # Second heavy tool call is throttled
    assert await limiter.consume("analyze_workload") is False

    # Calling a standard tool is still allowed under global bucket
    assert await limiter.consume("summarize_table") is True


@pytest.mark.anyio
async def test_rate_limiter_tenant_partitioning() -> None:
    limiter = RateLimiter(enabled=True, global_max=1, global_window=60)

    # Set client role context to tenant_a
    token_a = current_role.set("tenant_a")
    try:
        assert await limiter.consume("summarize_table") is True
        # tenant_a is now throttled
        assert await limiter.consume("summarize_table") is False
    finally:
        current_role.reset(token_a)

    # Set client role context to tenant_b
    token_b = current_role.set("tenant_b")
    try:
        # tenant_b should be allowed 1 request despite tenant_a being throttled
        assert await limiter.consume("summarize_table") is True
        assert await limiter.consume("summarize_table") is False
    finally:
        current_role.reset(token_b)


@pytest.mark.anyio
async def test_server_call_tool_enforces_rate_limits() -> None:
    # Build settings with rate limiter enabled and max of 1 request
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_RATE_LIMIT_ENABLED": "true",
            "MCPG_RATE_LIMIT_MAX_REQUESTS": "1",
        }
    )

    server = create_server(settings, database=FakeDatabase(FakeRoutingDriver(_HEALTHY_ROUTES)))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        # First call succeeds
        result1 = await client.call_tool("check_database_health", {})
        assert result1.isError is False

        # Second call is throttled, returning an error response
        result2 = await client.call_tool("check_database_health", {})
        assert result2.isError is True
        assert any("Rate limit exceeded" in content.text for content in result2.content)
