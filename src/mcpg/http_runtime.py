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

import hmac
import logging
from typing import TYPE_CHECKING

from mcpg.config import Settings
from mcpg.observability import render_prometheus

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

    if settings.http_auth_token is not None:
        # Starlette's add_middleware appends to the middleware stack
        # that's wrapped around the app at startup. ASGI middleware
        # has signature (app, *args, **kwargs) -> wrapped_app, and
        # Starlette calls it accordingly.
        app.add_middleware(_BearerAuthMiddleware, token=settings.http_auth_token)
    else:
        logger.warning(
            "MCPg HTTP transport %s is running without auth. "
            "Set MCPG_HTTP_AUTH_TOKEN to require Bearer tokens on every request.",
            kind,
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
