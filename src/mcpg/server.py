"""MCP server bootstrap for MCPg.

``create_server`` builds a configured :class:`MCPServer` instance. All shared
state (settings, the database connection) is owned by the server's lifespan
and exposed to tools via :class:`AppContext` — there is no module-level
mutable global state.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from pydantic import BaseModel, Field, ValidationError

from mcpg import __version__, about, audit
from mcpg.analytical import AnalyticalRunner
from mcpg.config import Settings, Transport
from mcpg.context import AppContext
from mcpg.cursors import CursorManager
from mcpg.database import Database
from mcpg.listen import ListenManager
from mcpg.middleware.rate_limit import RateLimiter
from mcpg.observability import get_metrics
from mcpg.otel_tracing import TracerHandle, setup_tracing, tool_span
from mcpg.tenancy import TenantRoleContextMiddleware
from mcpg.tools import register_tools


def _friendly_validation_error(tool_name: str, error: ValidationError) -> str:
    """Build a short, actionable message from a pydantic ``ValidationError``.

    The MCP SDK's tool-argument validation (a required argument missing, or
    an argument of the wrong type) raises a raw ``pydantic``
    ``ValidationError`` that the SDK stringifies verbatim into a
    ``ToolError`` — a multi-line dump including a noisy
    ``errors.pydantic.dev`` link (see GH #287). This collapses it into one
    line per error using each error's already-human-readable ``msg`` (e.g.
    "Field required"), keyed by its field path.
    """
    details = "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" if err["loc"] else err["msg"]
        for err in error.errors()
    )
    return f"{tool_name}: {details}"


SERVER_NAME = "mcpg"
SERVER_INSTRUCTIONS = (
    "MCPg: a PostgreSQL MCP server for inspecting, querying, operating, and tuning a Postgres database."
)

__all__ = ["SERVER_NAME", "AppContext", "AuditedMCPServer", "create_server", "make_lifespan", "run"]


class _ConfirmMutation(BaseModel):
    """Elicitation schema for the write-tier confirmation gate.

    Elicitation schemas must contain only primitive types per the MCP
    spec (see ``Context.elicit``'s docstring) — a single boolean field
    is all this gate needs.
    """

    confirm: bool = Field(description="Set true to proceed with this write/DDL operation.")


class AuditedMCPServer(MCPServer[AppContext]):
    """An MCPServer that records an audit event for every tool call."""

    rate_limiter: RateLimiter
    mcpg_settings: Settings
    in_flight_calls: int = 0
    # OpenTelemetry tracer. ``None`` when MCPG_OTEL_ENABLED=false or
    # the ``mcpg[otel]`` extra isn't installed — :func:`tool_span`
    # treats both cases as no-ops so ``call_tool`` doesn't branch.
    otel_tracer: TracerHandle | None = None

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

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[AppContext, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        self.in_flight_calls += 1
        try:
            metrics = get_metrics()
            # Resolve the capability bucket once per call so both the
            # OTel span attribute and the Prometheus counter carry the
            # same label. `classify_tool` returns None for tools that
            # don't match any override / pattern — defensively use
            # "unknown" so the label dimension stays cardinality-stable.
            bucket = about.classify_tool(name) or "unknown"

            # Enforce rate limiting if configured
            if hasattr(self, "rate_limiter"):
                allowed = await self.rate_limiter.consume(name)
                if not allowed:
                    raise RuntimeError(f"Rate limit exceeded for tool {name!r}. Please try again later.")

            # Opt-in interactive confirmation gate for write-tier tools
            # (MCPG_ELICIT_CONFIRM_WRITES). Centralized here rather than in
            # any of the 254 individual tool bodies: reuses the
            # readOnlyHint annotation tools.py already stamps on every
            # registered tool, and skips the elicit round-trip entirely
            # for clients that didn't declare elicitation support during
            # initialize, so non-interactive/automated clients are
            # unaffected unless they opt in.
            if (
                getattr(self, "mcpg_settings", None) is not None
                and self.mcpg_settings.elicit_confirm_writes
                and context is not None
            ):
                tool = self._tool_manager.get_tool(name)
                is_read_only = tool is not None and tool.annotations is not None and tool.annotations.read_only_hint
                if tool is not None and not is_read_only:
                    capabilities = context.client_capabilities
                    supports_elicitation = capabilities is not None and capabilities.elicitation is not None
                    if supports_elicitation:
                        # Timestamp taken here (not shared with the tool-body
                        # `start` timer below, which is never set on this
                        # path) so a denied call's duration includes the
                        # elicitation round-trip. Scoped to this branch only —
                        # it must not add an extra `time.monotonic()` call to
                        # every invocation, since other tests (test_slow_call.py)
                        # patch `time.monotonic` with a fixed-length side_effect
                        # list sized to the normal (non-gated) call path.
                        gate_start = time.monotonic()
                        confirmation = await context.elicit(
                            f"{name!r} will modify the database. Proceed?",
                            _ConfirmMutation,
                        )
                        if confirmation.action != "accept" or not confirmation.data.confirm:
                            # Declined/cancelled writes never reach the
                            # normal audit.record()/metrics.record_call()
                            # calls below (the tool body never runs) — record
                            # an equivalent event here so a denied write is
                            # never invisible to the audit log or
                            # render_prometheus(). Deliberately does not call
                            # `self._log_if_slow` — the tool body never ran,
                            # so a "slow call" warning here would be
                            # measuring user think-time, not tool latency.
                            denied_duration = time.monotonic() - gate_start
                            audit.record(audit.AuditEvent(tool=name, arguments=arguments, status="denied"))
                            metrics.record_call(name, "denied", denied_duration, bucket=bucket)
                            return CallToolResult(
                                content=[
                                    TextContent(
                                        type="text",
                                        text=f"{name!r} was not confirmed; no changes made.",
                                    )
                                ],
                                is_error=True,
                            )

            start = time.monotonic()
            try:
                with tool_span(self.otel_tracer, name, arguments, bucket=bucket):
                    result = await super().call_tool(name, arguments, context)
            except Exception as exc:
                duration = time.monotonic() - start
                self._log_if_slow(name, duration)
                # The SDK's argument-validation failure surfaces here as a
                # ToolError whose message is str(ValidationError). The known
                # SDK path chains it via `raise ... from e` (__cause__), but
                # an implicit `raise` inside an except block (no `from`)
                # only sets __context__ -- check both defensively, plus the
                # case where the ValidationError reaches us directly. Either
                # way, replace it with a friendly message before it's used
                # below, since both the audit log (`error=`) and the
                # client-visible message (further up the call stack) read
                # from this same exception object. See GH #287.
                validation_error = exc if isinstance(exc, ValidationError) else (exc.__cause__ or exc.__context__)
                if isinstance(validation_error, ValidationError):
                    friendly_exc = ToolError(_friendly_validation_error(name, validation_error))
                    audit.record(
                        audit.AuditEvent(tool=name, arguments=arguments, status="error", error=str(friendly_exc))
                    )
                    metrics.record_call(name, "error", duration, bucket=bucket)
                    raise friendly_exc from exc
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
    analytical_runner: AnalyticalRunner | None = None,
) -> Callable[[MCPServer[AppContext]], AbstractAsyncContextManager[AppContext]]:
    """Build the server lifespan: open the database on start, close on stop.

    The listen manager is created eagerly (cheap — it doesn't open the
    listener connection until the first ``subscribe_channel`` call) and
    torn down on lifespan exit so subscriptions can't outlive the
    server. The cursor manager holds dedicated connections per open
    server-side cursor and is closed-out symmetrically.
    """

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        from mcpg.cache import CacheManager, cache_namespace

        cache_manager = CacheManager(
            enabled=settings.cache_enabled,
            ttl_seconds=settings.cache_ttl_seconds,
            maxsize=settings.cache_maxsize,
            redis_url=settings.redis_url,
            namespace=cache_namespace(settings.database_url),
        )
        await cache_manager.start()
        if analytical_runner is not None:
            await analytical_runner.start()
        try:
            async with database, listen_manager, cursor_manager:
                yield AppContext(
                    settings=settings,
                    database=database,
                    listen_manager=listen_manager,
                    cursor_manager=cursor_manager,
                    cache=cache_manager,
                    analytical_runner=analytical_runner,
                )
        finally:
            if analytical_runner is not None:
                await analytical_runner.close()
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
    analytical_runner: AnalyticalRunner | None = None,
) -> MCPServer[AppContext]:
    """Construct a configured MCPServer.

    Args:
        settings: Validated server configuration.
        database: Optional pre-built database (used by tests); otherwise one
            is created from ``settings``.
        listen_manager: Optional pre-built listen manager (used by tests
            to inject a fake connection factory); otherwise a default
            one is created from ``settings``.
        analytical_runner: Optional pre-built analytical runner. When omitted
            a real one is built only in production (i.e. when ``database`` is
            also omitted) and analytical queries are enabled. Tests inject one
            here to exercise ``run_analytical_query`` against a fake database
            without standing up a second real pool.
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
    # ``enable_analytical_queries`` is the authoritative off-switch: when it's
    # false we neither build nor honour an injected runner, so the tool is
    # never registered regardless of injection (an operator's config wins over
    # a caller-supplied runner). When it's true, use an injected runner if
    # given; otherwise build the real (second-pool) one — but only in
    # production (no injected ``database``). Tests inject a mock ``database``;
    # building a real analytical pool there would try to connect its own pool
    # to the (fake) DSN and hang, so a test that wants a functional
    # ``run_analytical_query`` injects its own runner. Registration is gated on
    # the runner actually existing (below), so the surface never advertises a
    # tool that has no runner behind it.
    ar = analytical_runner if settings.enable_analytical_queries else None
    if ar is None and database is None and settings.enable_analytical_queries:
        ar = AnalyticalRunner(settings)
    server: AuditedMCPServer = AuditedMCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=make_lifespan(settings, db, lm, cm, ar),
        middleware=[TenantRoleContextMiddleware()],
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
    register_tools(server, settings, analytical_available=ar is not None)
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
