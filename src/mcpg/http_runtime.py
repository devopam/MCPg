"""HTTP-transport extensions: bearer-token auth + Prometheus /metrics.

FastMCP's ``streamable_http_app()`` / ``sse_app()`` return Starlette
applications. We don't need to fork that surface — we wrap the
returned app with two pieces of middleware-style infrastructure:

1. **Bearer-token auth.** When ``Settings.http_auth_token`` is set,
   every request to the MCP transport routes through a check that the
   ``Authorization: Bearer <token>`` header matches. Missing or wrong
   token → ``401 Unauthorized``. ``/metrics`` is exempted so a
   Prometheus scraper can hit it without holding the MCP token (a
   common operational split). The scraper still needs network reach;
   set ``MCPG_HTTP_HOST`` to ``0.0.0.0`` only if you've front-proxied
   the endpoint.
2. **Prometheus ``/metrics``.** A small ``Route`` that emits the text
   exposition format from :mod:`mcpg.observability`.

The ``stdio`` transport is unaffected — there's no HTTP surface to
guard. ``run_http`` builds the wrapped app and serves it via uvicorn.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from mcpg.config import Settings
from mcpg.observability import render_prometheus
from mcpg.oidc import OIDCError, OIDCVerifier
from mcpg.tenancy import TenancyError, current_role, validate_role

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

# Standard Prometheus text-format content type. Compatible with both
# OpenMetrics-aware and legacy scrapers.
_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Paths the auth middleware skips. /metrics ships its own auth story
# (network-level; see module docstring) so the operational split
# between the MCP token and the scraper works without a second
# credential.
_AUTH_EXEMPT_PATHS = frozenset({"/metrics", "/healthz", "/readyz"})


def _metrics_response_factory() -> Callable[[Request], Awaitable[Response]]:
    """Build the Starlette endpoint that serves the metrics text."""
    from starlette.responses import PlainTextResponse

    async def metrics_endpoint(_request: Request) -> Response:
        return PlainTextResponse(render_prometheus(), media_type=_PROMETHEUS_CONTENT_TYPE)

    return metrics_endpoint


def _health_response_factory() -> Callable[[Request], Awaitable[Response]]:
    from starlette.responses import PlainTextResponse

    async def healthz(_request: Request) -> Response:
        return PlainTextResponse("ok\n")

    return healthz


class _BearerAuthMiddleware:
    """ASGI middleware that enforces ``Authorization: Bearer <token>``.

    Uses :func:`hmac.compare_digest` so token comparison is
    constant-time — a tiny precaution against timing oracles, but a
    free one given how easy it is to write.
    """

    def __init__(self, app: object, *, token: str, exempt_paths: frozenset[str] = _AUTH_EXEMPT_PATHS) -> None:
        self._app = app
        self._token = token
        self._exempt = exempt_paths

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope["type"] != "http":
            # Non-HTTP scopes (lifespan) pass straight through; the auth
            # gate only makes sense for actual requests.
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        path = str(scope.get("path", ""))
        if path in self._exempt:
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])  # type: ignore[assignment]
        auth_header = b""
        for key, value in headers:
            if key.lower() == b"authorization":
                auth_header = value
                break

        if not auth_header.startswith(b"Bearer "):
            await _send_401(send, "missing Authorization: Bearer header")
            return

        presented = auth_header[len(b"Bearer ") :].decode("utf-8", errors="ignore").strip()
        if not hmac.compare_digest(presented, self._token):
            await _send_401(send, "invalid bearer token")
            return

        await self._app(scope, receive, send)  # type: ignore[operator]


class _OIDCAuthMiddleware:
    """ASGI middleware that validates the bearer JWT against an OIDC issuer.

    When ``role_claim`` is configured AND the JWT carries that claim,
    the claim's value is also stashed in
    :data:`mcpg.tenancy.current_role` so the tenanted driver issues
    ``SET LOCAL ROLE`` for the request. This replaces the
    ``X-MCPG-Role`` header path for OIDC deployments — the issuer
    becomes the single source of truth for which role the caller can
    assume.
    """

    def __init__(
        self,
        app: object,
        *,
        verifier: OIDCVerifier,
        exempt_paths: frozenset[str] = _AUTH_EXEMPT_PATHS,
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._exempt = exempt_paths

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        path = str(scope.get("path", ""))
        if path in self._exempt:
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])  # type: ignore[assignment]
        auth_header = b""
        for key, value in headers:
            if key.lower() == b"authorization":
                auth_header = value
                break

        if not auth_header.startswith(b"Bearer "):
            await _send_401(send, "missing Authorization: Bearer header")
            return

        token = auth_header[len(b"Bearer ") :].decode("utf-8", errors="ignore").strip()
        try:
            verified = await self._verifier.verify(token)
        except OIDCError as exc:
            logger.warning("OIDC verification failed: %s", exc)
            await _send_401(send, "invalid bearer token")
            return

        if verified.role is None:
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        # JWT carried a role claim — validate it as an identifier
        # before stashing so an attacker-controlled value can't break
        # the SQL the tenancy driver inlines.
        try:
            validate_role(verified.role)
        except TenancyError:
            logger.warning("OIDC role claim has unsafe identifier: %r", verified.role)
            await _send_401(send, "role claim contains an invalid identifier")
            return

        reset_token = current_role.set(verified.role)
        try:
            await self._app(scope, receive, send)  # type: ignore[operator]
        finally:
            current_role.reset(reset_token)


class _TenantRoleMiddleware:
    """ASGI middleware that parses ``X-MCPG-Role`` into a ContextVar.

    Per-request multi-tenancy: when the header is present, validate it
    against the allowlist (when one is configured) and set
    :data:`mcpg.tenancy.current_role`. The
    :class:`mcpg.tenancy.TenantSqlDriver` then issues
    ``SET LOCAL ROLE`` inside every query's transaction so the role
    auto-resets when the transaction ends.

    Skips non-HTTP scopes and the same exempt paths as
    :class:`_BearerAuthMiddleware` so probes don't try to acquire a
    role they don't need.
    """

    def __init__(
        self,
        app: object,
        *,
        allowed_roles: tuple[str, ...] = (),
        exempt_paths: frozenset[str] = _AUTH_EXEMPT_PATHS,
    ) -> None:
        self._app = app
        self._allowed = frozenset(allowed_roles)
        self._exempt = exempt_paths

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        path = str(scope.get("path", ""))
        if path in self._exempt:
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])  # type: ignore[assignment]
        role_header: bytes | None = None
        for key, value in headers:
            if key.lower() == b"x-mcpg-role":
                role_header = value
                break

        if role_header is None:
            # No per-request override; the driver will fall back to
            # the static default_role from Settings.
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        role = role_header.decode("utf-8", errors="ignore").strip()
        try:
            validate_role(role)
        except TenancyError:
            await _send_403(send, "X-MCPG-Role contains an invalid identifier")
            return

        if self._allowed and role not in self._allowed:
            await _send_403(send, "X-MCPG-Role is not in the allowed-roles list")
            return

        token = current_role.set(role)
        try:
            await self._app(scope, receive, send)  # type: ignore[operator]
        finally:
            current_role.reset(token)


async def _send_403(send: object, reason: str) -> None:
    """Emit a minimal 403 response."""
    body = f'{{"error": "forbidden", "reason": "{reason}"}}\n'.encode()
    await send(  # type: ignore[operator]
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})  # type: ignore[operator]


async def _send_401(send: object, reason: str) -> None:
    """Emit a minimal 401 response without leaking server internals."""
    body = f'{{"error": "unauthorized", "reason": "{reason}"}}\n'.encode()
    await send(  # type: ignore[operator]
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="mcpg"'),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})  # type: ignore[operator]


async def _send_413(send: object, reason: str) -> None:
    """Emit a minimal 413 response."""
    body = f'{{"error": "request_entity_too_large", "reason": "{reason}"}}\n'.encode()
    await send(  # type: ignore[operator]
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})  # type: ignore[operator]


async def _send_504(send: object, reason: str) -> None:
    """Emit a minimal 504 response."""
    body = f'{{"error": "gateway_timeout", "reason": "{reason}"}}\n'.encode()
    await send(  # type: ignore[operator]
        {
            "type": "http.response.start",
            "status": 504,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})  # type: ignore[operator]


class _SecurityHeadersMiddleware:
    """ASGI middleware that enforces standard security headers."""

    def __init__(self, app: object, *, hsts_max_age: int = 31536000) -> None:
        self._app = app
        self._hsts_max_age = hsts_max_age

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        async def send_wrapper(message: dict[str, object]) -> None:
            if message["type"] == "http.response.start":
                raw_headers = message.get("headers") or []
                headers: list[tuple[bytes, bytes]] = list(cast(Iterable[Any], raw_headers))
                existing_keys = {k.lower() for k, _ in headers}

                if b"content-security-policy" not in existing_keys:
                    headers.append((b"content-security-policy", b"default-src 'self'"))
                if b"x-frame-options" not in existing_keys:
                    headers.append((b"x-frame-options", b"DENY"))
                if b"x-content-type-options" not in existing_keys:
                    headers.append((b"x-content-type-options", b"nosniff"))
                if b"referrer-policy" not in existing_keys:
                    headers.append((b"referrer-policy", b"no-referrer"))
                if self._hsts_max_age > 0 and b"strict-transport-security" not in existing_keys:
                    hsts_val = f"max-age={self._hsts_max_age}; includeSubDomains".encode()
                    headers.append((b"strict-transport-security", hsts_val))

                message["headers"] = headers

            await send(message)  # type: ignore[operator]

        await self._app(scope, receive, send_wrapper)  # type: ignore[operator]


class _RequestTooLargeError(Exception):
    """Raised when the request body exceeds the configured maximum size."""

    pass


class _RequestSizeLimitMiddleware:
    """ASGI middleware that caps request body size to prevent DoS."""

    def __init__(self, app: object, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])  # type: ignore[assignment]
        content_length = -1
        for key, value in headers:
            if key.lower() == b"content-length":
                try:
                    content_length = int(value)
                except ValueError:
                    pass
                break

        if content_length > self._max_bytes:
            await _send_413(send, "request body too large")
            return

        bytes_received = 0

        async def receive_wrapper() -> dict[str, object]:
            nonlocal bytes_received
            message = cast(dict[str, object], await receive())  # type: ignore[operator]
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    bytes_received += len(body)
                if bytes_received > self._max_bytes:
                    raise _RequestTooLargeError()
            return message

        try:
            await self._app(scope, receive_wrapper, send)  # type: ignore[operator]
        except _RequestTooLargeError:
            await _send_413(send, "request body too large")


class _RequestTimeoutMiddleware:
    """ASGI middleware that caps wall-clock time for a single request.

    Off by default (``MCPG_HTTP_REQUEST_TIMEOUT_SECONDS=0``); only
    installed when a positive timeout is configured. Intended for
    request/response deployments — note that a hard cap will also cut
    off long-lived streaming responses, so leave it disabled if you
    rely on long SSE / streamable-http streams.

    On expiry, if the downstream app has not started the response yet,
    a 504 is emitted; if bytes were already sent we can only abort the
    stream (the status line is long gone).
    """

    def __init__(self, app: object, *, timeout_seconds: int) -> None:
        self._app = app
        self._timeout = timeout_seconds

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        started = False

        async def send_wrapper(message: dict[str, object]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)  # type: ignore[operator]

        try:
            async with asyncio.timeout(self._timeout):
                await self._app(scope, receive, send_wrapper)  # type: ignore[operator]
        except TimeoutError:
            # Only safe to write a status line if the app hasn't already.
            if not started:
                await _send_504(send, f"request exceeded {self._timeout}s")


def build_http_app(server: object, settings: Settings, *, kind: str) -> Starlette:
    """Wrap a FastMCP HTTP app with metrics + optional auth.

    Args:
        server: A ``FastMCP`` instance.
        settings: Active server settings.
        kind: ``"streamable-http"`` or ``"sse"``.

    Returns:
        A Starlette app with ``/metrics`` mounted and (when
        ``settings.http_auth_token`` is set) bearer-token auth on
        everything except the exempt paths.
    """
    from starlette.routing import Route

    if kind == "streamable-http":
        app = server.streamable_http_app()  # type: ignore[attr-defined]
    elif kind == "sse":
        app = server.sse_app()  # type: ignore[attr-defined]
    else:
        raise ValueError(f"unknown HTTP transport kind: {kind!r}")

    # Mount /metrics + /healthz on the existing router. Starlette
    # exposes the underlying router via .router; appending Route
    # objects is the idiomatic way to extend the route table without
    # subclassing.
    app.router.routes.append(Route("/metrics", _metrics_response_factory(), methods=["GET"]))
    app.router.routes.append(Route("/healthz", _health_response_factory(), methods=["GET"]))

    # Middleware stack ordering:
    #   In OIDC mode, the OIDC middleware verifies the JWT AND stashes
    #   the role-claim into current_role itself — so the tenant
    #   X-MCPG-Role middleware is skipped (the issuer is the source of
    #   truth for the role). In static mode, the X-MCPG-Role
    #   middleware sits ABOVE the bearer-token middleware so
    #   unauthenticated requests can't reach the role parser; Starlette
    #   applies the most-recently-added middleware first, so we add
    #   the auth layer LAST.
    if settings.auth_mode == "oidc":
        assert settings.oidc_issuer is not None  # validated by load_settings
        assert settings.oidc_audience is not None
        verifier = OIDCVerifier(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
            role_claim=settings.oidc_role_claim,
            allowed_roles=settings.allowed_roles,
        )
        app.add_middleware(_OIDCAuthMiddleware, verifier=verifier)
    else:
        if settings.default_role is not None or settings.allowed_roles:
            app.add_middleware(_TenantRoleMiddleware, allowed_roles=settings.allowed_roles)
        if settings.http_auth_token is not None:
            app.add_middleware(_BearerAuthMiddleware, token=settings.http_auth_token)
        else:
            logger.warning(
                "MCPg HTTP transport %s is running without auth. "
                "Set MCPG_HTTP_AUTH_TOKEN or MCPG_AUTH_MODE=oidc to require "
                "bearer tokens on every request.",
                kind,
            )

    # Outer middlewares (processed first on request)
    app.add_middleware(_SecurityHeadersMiddleware, hsts_max_age=settings.http_hsts_max_age)
    app.add_middleware(_RequestSizeLimitMiddleware, max_bytes=settings.http_max_body_bytes)
    # Opt-in per-request wall-clock cap. Disabled (0) by default so the
    # streamable-http / sse long-lived streams keep working untouched.
    if settings.http_request_timeout_seconds > 0:
        app.add_middleware(_RequestTimeoutMiddleware, timeout_seconds=settings.http_request_timeout_seconds)
    if settings.http_allowed_origins:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.http_allowed_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # FastMCP types streamable_http_app() / sse_app() as Starlette already,
    # but mypy under strict mode loses the type through the .add_middleware
    # call (returns None). Cast through Any to settle the return type
    # without hiding real type errors.
    from typing import cast as _cast

    return _cast("Starlette", app)


def run_http(server: object, settings: Settings, *, kind: str) -> None:
    """Build the wrapped HTTP app and serve it via uvicorn."""
    import uvicorn

    app = build_http_app(server, settings, kind=kind)
    uvicorn.run(app, host=settings.http_host, port=settings.http_port)
