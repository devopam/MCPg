"""MCP server bootstrap for MCPg.

``create_server`` builds a configured :class:`FastMCP` instance. All shared
state (settings, the database connection) is owned by the server's lifespan
and exposed to tools via :class:`AppContext` — there is no module-level
mutable global state.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock

from mcpg import __version__, about, audit
from mcpg.config import Settings, Transport
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.database import Database
from mcpg.listen import ListenManager
from mcpg.middleware.rate_limit import RateLimiter
from mcpg.observability import get_metrics
from mcpg.otel_tracing import TracerHandle, setup_tracing, tool_span
from mcpg.tools import register_tools

SERVER_NAME = "mcpg"
SERVER_INSTRUCTIONS = (
    "MCPg: a PostgreSQL MCP server for inspecting, querying, operating, and tuning a Postgres database."
)

__all__ = ["SERVER_NAME", "AppContext", "AuditedFastMCP", "create_server", "make_lifespan", "run"]


class AuditedFastMCP(FastMCP[AppContext]):
    """A FastMCP server that records an audit event for every tool call."""

    rate_limiter: RateLimiter
    mcpg_settings: Settings
    in_flight_calls: int = 0
    # OpenTelemetry tracer. ``None`` when MCPG_OTEL_ENABLED=false or
    # the ``mcpg[otel]`` extra isn't installed — :func:`tool_span`
    # treats both cases as no-ops so ``call_tool`` doesn't branch.
    otel_tracer: TracerHandle | None = None

    def __init__(self, *args: Any, version: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # FastMCP's constructor doesn't forward a version to the low-level
        # MCP server, so the ``initialize`` handshake reports the MCP SDK's
        # version in ``serverInfo`` rather than ours. This subclass exists
        # to extend FastMCP, so it's the right place to hold that one bit of
        # SDK-internal knowledge: pin the advertised version to mcpg's own.
        if version is not None:
            self._mcp_server.version = version

    def _log_if_slow(self, name: str, duration: float) -> None:
        if not hasattr(self, "mcpg_settings"):
            return
        threshold_ms = self.mcpg_settings.slow_call_threshold_ms
        if threshold_ms <= 0:
            return
        threshold_sec = threshold_ms / 1000.0
        if duration > threshold_sec:
            import logging

            logger = logging.getLogger("mcpg.server")
            logger.warning(
                "Slow tool call: %s took %.3fs (threshold: %.3fs)",
                name,
                duration,
                threshold_sec,
            )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        self.in_flight_calls += 1
        try:
            # Enforce rate limiting if configured
            if hasattr(self, "rate_limiter"):
                allowed = await self.rate_limiter.consume(name)
                if not allowed:
                    raise RuntimeError(f"Rate limit exceeded for tool {name!r}. Please try again later.")

            metrics = get_metrics()
            # Resolve the capability bucket once per call so both the
            # OTel span attribute and the Prometheus counter carry the
            # same label. `classify_tool` returns None for tools that
            # don't match any override / pattern — defensively use
            # "unknown" so the label dimension stays cardinality-stable.
            bucket = about.classify_tool(name) or "unknown"
            start = time.monotonic()
            try:
                with tool_span(self.otel_tracer, name, arguments, bucket=bucket):
                    result = await super().call_tool(name, arguments)
            except Exception as exc:
                duration = time.monotonic() - start
                self._log_if_slow(name, duration)
                audit.record(audit.AuditEvent(tool=name, arguments=arguments, status="error", error=str(exc)))
                metrics.record_call(name, "error", duration, bucket=bucket)
                raise
            duration = time.monotonic() - start
            self._log_if_slow(name, duration)
            audit.record(audit.AuditEvent(tool=name, arguments=arguments, status="ok"))
            metrics.record_call(name, "ok", duration, bucket=bucket)
            return result
        finally:
            self.in_flight_calls -= 1


def make_lifespan(
    settings: Settings,
    database: Database,
    listen_manager: ListenManager,
    cursor_manager: CursorManager,
) -> Callable[[FastMCP[AppContext]], AbstractAsyncContextManager[AppContext]]:
    """Build the server lifespan: open the database on start, close on stop.

    The listen manager is created eagerly (cheap — it doesn't open the
    listener connection until the first ``subscribe_channel`` call) and
    torn down on lifespan exit so subscriptions can't outlive the
    server. The cursor manager holds dedicated connections per open
    server-side cursor and is closed-out symmetrically.
    """

    @asynccontextmanager
    async def lifespan(_server: FastMCP[AppContext]) -> AsyncIterator[AppContext]:
        from mcpg.cache import CacheManager, cache_namespace

        cache_manager = CacheManager(
            enabled=settings.cache_enabled,
            ttl_seconds=settings.cache_ttl_seconds,
            maxsize=settings.cache_maxsize,
            redis_url=settings.redis_url,
            namespace=cache_namespace(settings.database_url),
        )
        await cache_manager.start()
        try:
            async with database, listen_manager, cursor_manager:
                yield AppContext(
                    settings=settings,
                    database=database,
                    listen_manager=listen_manager,
                    cursor_manager=cursor_manager,
                    cache=cache_manager,
                )
        finally:
            if hasattr(_server, "in_flight_calls"):
                import asyncio
                import logging

                logger = logging.getLogger("mcpg.server")

                drain_start = time.monotonic()
                drain_timeout = settings.shutdown_drain_seconds

                while _server.in_flight_calls > 0:
                    elapsed = time.monotonic() - drain_start
                    if elapsed >= drain_timeout:
                        logger.warning(
                            "Shutdown drain timed out after %ds; force exiting with %d tool calls in-flight",
                            drain_timeout,
                            _server.in_flight_calls,
                        )
                        break
                    logger.info("Waiting for %d in-flight tool calls to drain...", _server.in_flight_calls)
                    await asyncio.sleep(0.1)

            await cache_manager.close()

            # Flush pending OTel spans so a clean shutdown doesn't
            # drop the last batch of traces. Tracer is process-wide
            # global but the provider hung off the server lets us
            # invoke shutdown only when we actually own it.
            if hasattr(_server, "otel_tracer") and _server.otel_tracer is not None:
                _server.otel_tracer.shutdown()

    return lifespan


def create_server(
    settings: Settings,
    *,
    database: Database | None = None,
    listen_manager: ListenManager | None = None,
    cursor_manager: CursorManager | None = None,
) -> FastMCP[AppContext]:
    """Construct a configured FastMCP server.

    Args:
        settings: Validated server configuration.
        database: Optional pre-built database (used by tests); otherwise one
            is created from ``settings``.
        listen_manager: Optional pre-built listen manager (used by tests
            to inject a fake connection factory); otherwise a default
            one is created from ``settings``.
    """
    from mcpg.obs_logging import setup_logging

    setup_logging(settings)

    db = database if database is not None else Database(settings)
    lm = (
        listen_manager
        if listen_manager is not None
        else ListenManager(database_url=settings.database_url, queue_max=settings.listen_queue_max)
    )
    cm = cursor_manager if cursor_manager is not None else CursorManager(database_url=settings.database_url)
    server: AuditedFastMCP = AuditedFastMCP(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=make_lifespan(settings, db, lm, cm),
        host=settings.http_host,
        port=settings.http_port,
    )
    server.mcpg_settings = settings
    server.otel_tracer = setup_tracing(settings)
    # Instantiate and register the RateLimiter
    server.rate_limiter = RateLimiter(
        enabled=settings.rate_limit_enabled,
        global_max=settings.rate_limit_max_requests,
        global_window=settings.rate_limit_window_seconds,
        heavy_max=settings.rate_limit_heavy_max,
        heavy_window=settings.rate_limit_heavy_window,
    )
    register_tools(server, settings)
    return server


def run(settings: Settings) -> None:
    """Create and run the server using the transport from ``settings``.

    HTTP transports (``streamable-http`` and ``sse``) go through
    :mod:`mcpg.http_runtime` so the ``/metrics`` endpoint and optional
    bearer-token auth attach to the served app.
    """
    server = create_server(settings)
    match settings.transport:
        case Transport.STDIO:
            # stdout carries the JSON-RPC stream, so this reassuring banner
            # goes to the logger (stderr) — otherwise a first-time user who
            # runs `mcpg` just to see it work stares at a silent, blocked
            # process and assumes it hung.
            logging.getLogger("mcpg.server").info(
                "mcpg %s ready on stdio (%s mode) — waiting for an MCP client to connect",
                __version__,
                settings.access_mode.value,
            )
            server.run(transport="stdio")
        case Transport.STREAMABLE_HTTP:
            from mcpg.http_runtime import run_http

            run_http(server, settings, kind="streamable-http")
        case Transport.SSE:
            from mcpg.http_runtime import run_http

            run_http(server, settings, kind="sse")
