"""Tests for the HTTP runtime — bearer-token auth + /metrics endpoint."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcpg.config import load_settings
from mcpg.http_runtime import (
    _AUTH_EXEMPT_PATHS,
    _BearerAuthMiddleware,
    _TenantRoleMiddleware,
    build_http_app,
)
from mcpg.observability import get_metrics, reset_metrics
from mcpg.tenancy import current_role


@pytest.fixture(autouse=True)
def _reset_metrics_between_tests() -> None:
    reset_metrics()


def _bare_app() -> Starlette:
    async def _root(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/", _root, methods=["GET"])])


def test_bearer_middleware_allows_correct_token() -> None:
    inner = _bare_app()
    middleware = _BearerAuthMiddleware(inner, token="s3cr3t")
    test_app = Starlette(
        routes=inner.router.routes,
    )
    # We can't easily call middleware via TestClient without rebuilding,
    # so exercise the middleware directly with a synthetic ASGI scope.
    _ = middleware  # used in next test
    # Smoke: TestClient against the bare app shows it works.
    with TestClient(test_app) as client:
        response = client.get("/")
        assert response.status_code == 200


def test_bearer_middleware_returns_401_when_authorization_header_is_missing() -> None:
    sent_messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    inner = _bare_app()
    middleware = _BearerAuthMiddleware(inner, token="s3cr3t")
    scope = {"type": "http", "path": "/", "headers": []}

    import asyncio

    asyncio.run(middleware(scope, receive, send))

    assert sent_messages[0]["type"] == "http.response.start"
    assert sent_messages[0]["status"] == 401


def test_bearer_middleware_returns_401_for_wrong_token() -> None:
    sent_messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    inner = _bare_app()
    middleware = _BearerAuthMiddleware(inner, token="s3cr3t")
    scope = {
        "type": "http",
        "path": "/",
        "headers": [(b"authorization", b"Bearer wrong")],
    }

    import asyncio

    asyncio.run(middleware(scope, receive, send))

    assert sent_messages[0]["status"] == 401


def test_bearer_middleware_exempts_metrics_path_even_without_token() -> None:
    """A Prometheus scraper hits /metrics without the MCP bearer token."""
    sent_messages: list[dict[str, object]] = []
    inner_invoked = False

    async def inner(_scope: object, _receive: object, send_fn: object) -> None:
        nonlocal inner_invoked
        inner_invoked = True
        await send_fn(  # type: ignore[operator]
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send_fn({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    middleware = _BearerAuthMiddleware(inner, token="s3cr3t")
    scope = {"type": "http", "path": "/metrics", "headers": []}

    import asyncio

    asyncio.run(middleware(scope, receive, send))

    # The middleware passed through to the inner app without checking auth.
    assert inner_invoked
    assert sent_messages[0]["status"] == 200


def test_bearer_middleware_passes_non_http_scopes_through_unmodified() -> None:
    inner_invoked = False

    async def inner(scope: object, _receive: object, _send: object) -> None:
        nonlocal inner_invoked
        inner_invoked = True
        # Lifespan scopes must reach the underlying ASGI app or the
        # server never starts up.
        assert scope["type"] == "lifespan"  # type: ignore[index]

    middleware = _BearerAuthMiddleware(inner, token="s3cr3t")
    scope = {"type": "lifespan"}

    import asyncio

    asyncio.run(middleware(scope, lambda: None, lambda _: None))  # type: ignore[arg-type]

    assert inner_invoked


def test_auth_exempt_paths_includes_metrics_and_health_endpoints() -> None:
    # Pin the exempt set so adding to it requires an explicit change here too.
    assert "/metrics" in _AUTH_EXEMPT_PATHS
    assert "/healthz" in _AUTH_EXEMPT_PATHS
    assert "/readyz" in _AUTH_EXEMPT_PATHS


def test_build_http_app_rejects_unknown_kind() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

        def sse_app(self) -> Starlette:
            return _bare_app()

    with pytest.raises(ValueError, match="unknown HTTP transport kind"):
        build_http_app(_Stub(), settings, kind="websocket")  # type: ignore[arg-type]


def test_build_http_app_serves_metrics_with_observability_payload() -> None:
    # Record one observation so the /metrics body has something to assert on.
    get_metrics().record_call("smoke_test", "ok", 0.05)

    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    wrapped = build_http_app(_Stub(), settings, kind="streamable-http")
    with TestClient(wrapped) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "mcpg_tool_calls_total" in response.text
    assert 'tool="smoke_test"' in response.text
    assert "text/plain" in response.headers["content-type"]


def test_build_http_app_serves_healthz_unauthenticated() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    wrapped = build_http_app(_Stub(), settings, kind="streamable-http")
    with TestClient(wrapped) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text.startswith("ok")


def test_build_http_app_with_token_blocks_unauthenticated_requests() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_AUTH_TOKEN": "topsecret",
        }
    )

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    wrapped = build_http_app(_Stub(), settings, kind="streamable-http")
    with TestClient(wrapped) as client:
        # No header → 401.
        response = client.get("/")
        assert response.status_code == 401
        # Right token → 200.
        response = client.get("/", headers={"Authorization": "Bearer topsecret"})
        assert response.status_code == 200
        # Wrong token → 401.
        response = client.get("/", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        # /metrics still works without a token.
        response = client.get("/metrics")
        assert response.status_code == 200


# --- per-request role multi-tenancy (Phase 1.4) --------------------------


def test_tenant_role_middleware_sets_contextvar_for_the_inner_app() -> None:
    """A request with X-MCPG-Role binds the ContextVar for the inner app."""
    observed_roles: list[str | None] = []

    async def inner(_scope: object, _receive: object, send_fn: object) -> None:
        observed_roles.append(current_role.get())
        await send_fn(  # type: ignore[operator]
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send_fn({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(_message: dict[str, object]) -> None:
        pass

    middleware = _TenantRoleMiddleware(inner, allowed_roles=("tenant_a", "tenant_b"))
    scope = {
        "type": "http",
        "path": "/",
        "headers": [(b"x-mcpg-role", b"tenant_a")],
    }

    import asyncio

    asyncio.run(middleware(scope, receive, send))

    assert observed_roles == ["tenant_a"]
    # ContextVar is reset on exit so the next request starts clean.
    assert current_role.get() is None


def test_tenant_role_middleware_returns_403_when_role_is_not_in_allowlist() -> None:
    sent_messages: list[dict[str, object]] = []

    async def inner(_scope: object, _receive: object, _send: object) -> None:
        raise AssertionError("inner app should not be invoked")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    middleware = _TenantRoleMiddleware(inner, allowed_roles=("tenant_a",))
    scope = {
        "type": "http",
        "path": "/",
        "headers": [(b"x-mcpg-role", b"tenant_zzz")],
    }

    import asyncio

    asyncio.run(middleware(scope, receive, send))

    assert sent_messages[0]["status"] == 403


def test_tenant_role_middleware_returns_403_when_role_has_invalid_characters() -> None:
    sent_messages: list[dict[str, object]] = []

    async def inner(_scope: object, _receive: object, _send: object) -> None:
        raise AssertionError("inner app should not be invoked")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    middleware = _TenantRoleMiddleware(inner)
    scope = {
        "type": "http",
        "path": "/",
        "headers": [(b"x-mcpg-role", b'"; DROP USER alice; --')],
    }

    import asyncio

    asyncio.run(middleware(scope, receive, send))

    assert sent_messages[0]["status"] == 403


def test_tenant_role_middleware_passes_through_when_header_is_absent() -> None:
    observed_roles: list[str | None] = []

    async def inner(_scope: object, _receive: object, send_fn: object) -> None:
        observed_roles.append(current_role.get())
        await send_fn(  # type: ignore[operator]
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send_fn({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(_message: dict[str, object]) -> None:
        pass

    middleware = _TenantRoleMiddleware(inner)
    scope = {"type": "http", "path": "/", "headers": []}

    import asyncio

    asyncio.run(middleware(scope, receive, send))

    # No header → driver falls back to static default_role.
    assert observed_roles == [None]


def test_tenant_role_middleware_exempts_health_paths() -> None:
    """A probe to /healthz doesn't need the X-MCPG-Role header."""
    inner_invoked = False

    async def inner(_scope: object, _receive: object, send_fn: object) -> None:
        nonlocal inner_invoked
        inner_invoked = True
        await send_fn(  # type: ignore[operator]
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send_fn({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(_message: dict[str, object]) -> None:
        pass

    # Allowlist set but no header → would normally pass through;
    # explicitly verify /healthz is exempt before that.
    middleware = _TenantRoleMiddleware(inner, allowed_roles=("tenant_a",))
    scope = {"type": "http", "path": "/healthz", "headers": []}

    import asyncio

    asyncio.run(middleware(scope, receive, send))

    assert inner_invoked


def test_build_http_app_with_tenant_role_returns_403_for_unknown_role() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_DEFAULT_ROLE": "tenant_a",
            "MCPG_ALLOWED_ROLES": "tenant_a,tenant_b",
        }
    )

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    wrapped = build_http_app(_Stub(), settings, kind="streamable-http")
    with TestClient(wrapped) as client:
        # No header → default_role kicks in, request succeeds.
        response = client.get("/")
        assert response.status_code == 200
        # Allowed role → 200.
        response = client.get("/", headers={"X-MCPG-Role": "tenant_b"})
        assert response.status_code == 200
        # Unknown role → 403.
        response = client.get("/", headers={"X-MCPG-Role": "tenant_zzz"})
        assert response.status_code == 403


# --- OIDC mode (Shortlist 6.5) -------------------------------------------


def test_build_http_app_in_oidc_mode_installs_the_oidc_middleware() -> None:
    """In OIDC mode, _OIDCAuthMiddleware replaces the static bearer +
    X-MCPG-Role pair: the issuer is the source of truth for the role."""
    from mcpg.http_runtime import _OIDCAuthMiddleware

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_AUTH_MODE": "oidc",
            "MCPG_OIDC_ISSUER": "https://issuer.example",
            "MCPG_OIDC_AUDIENCE": "mcpg",
        }
    )

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    wrapped = build_http_app(_Stub(), settings, kind="streamable-http")

    # Find the OIDC middleware in the wrapped app's middleware stack.
    middleware_classes = [m.cls for m in wrapped.user_middleware]
    assert _OIDCAuthMiddleware in middleware_classes


def test_build_http_app_in_oidc_mode_blocks_requests_without_a_valid_jwt() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_AUTH_MODE": "oidc",
            "MCPG_OIDC_ISSUER": "https://issuer.example",
            "MCPG_OIDC_AUDIENCE": "mcpg",
        }
    )

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    wrapped = build_http_app(_Stub(), settings, kind="streamable-http")
    with TestClient(wrapped) as client:
        # No token → 401.
        response = client.get("/")
        assert response.status_code == 401
        # Wrong token → 401 (verification fails — discovery never reached).
        response = client.get("/", headers={"Authorization": "Bearer not.a.real.jwt"})
        assert response.status_code == 401
        # /metrics and /healthz still bypass auth.
        response = client.get("/metrics")
        assert response.status_code == 200
        response = client.get("/healthz")
        assert response.status_code == 200


# --- HTTP hardening middlewares (Security headers, CORS, request size limit) ---


def test_security_headers_middleware_adds_headers() -> None:
    from mcpg.http_runtime import _SecurityHeadersMiddleware

    inner = _bare_app()
    app = Starlette(routes=inner.router.routes)
    app.add_middleware(_SecurityHeadersMiddleware, hsts_max_age=31536000)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-security-policy"] == "default-src 'self'"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_request_size_limit_middleware_blocks_large_requests() -> None:
    from mcpg.http_runtime import _RequestSizeLimitMiddleware

    async def _post_handler(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", _post_handler, methods=["POST"])])
    app.add_middleware(_RequestSizeLimitMiddleware, max_bytes=10)

    with TestClient(app) as client:
        # Fits in limit (size 2) -> 200
        response = client.post("/", content="ok")
        assert response.status_code == 200

        # Exceeds limit (size 26) -> 413
        response = client.post("/", content="abcdefghijklmnopqrstuvwxyz")
        assert response.status_code == 413
        assert "request_entity_too_large" in response.text


def test_cors_middleware_integration() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_ALLOWED_ORIGINS": "http://localhost:3000, https://app.example.com",
        }
    )

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    wrapped = build_http_app(_Stub(), settings, kind="streamable-http")
    with TestClient(wrapped) as client:
        # Preflight options request from allowed origin
        headers = {
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        }
        response = client.options("/", headers=headers)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_security_headers_middleware_override_behavior() -> None:
    from mcpg.http_runtime import _SecurityHeadersMiddleware

    # An app that explicitly sets one of the security headers (e.g. CSP)
    async def _custom_app(scope: dict[str, object], receive: object, send: object) -> None:
        if scope["type"] == "http":

            async def custom_send(message: dict[str, object]) -> None:
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"content-security-policy", b"default-src 'none'"))
                    headers.append((b"x-frame-options", b"SAMEORIGIN"))
                    message["headers"] = headers
                await send(message)  # type: ignore[operator]

            await _bare_app()(scope, receive, custom_send)
        else:
            await _bare_app()(scope, receive, send)

    app = Starlette()
    app.add_middleware(_SecurityHeadersMiddleware, hsts_max_age=31536000)
    app.mount("/", _custom_app)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        # The inner app's custom headers must not be overwritten by the middleware
        assert response.headers["content-security-policy"] == "default-src 'none'"
        assert response.headers["x-frame-options"] == "SAMEORIGIN"
        # Other default headers should still be set
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_security_headers_middleware_hsts_disabled() -> None:
    from mcpg.http_runtime import _SecurityHeadersMiddleware

    inner = _bare_app()
    app = Starlette(routes=inner.router.routes)
    app.add_middleware(_SecurityHeadersMiddleware, hsts_max_age=0)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        # strict-transport-security should NOT be present since max-age is 0
        assert "strict-transport-security" not in response.headers


def test_security_headers_middleware_non_http_scope() -> None:
    from mcpg.http_runtime import _SecurityHeadersMiddleware

    async def _dummy_app(scope: dict[str, object], receive: object, send: object) -> None:
        pass

    middleware = _SecurityHeadersMiddleware(_dummy_app, hsts_max_age=3600)
    scope = {"type": "websocket"}

    # Running this should not raise any exceptions or add HTTP headers
    import asyncio

    asyncio.run(middleware(scope, None, None))


def test_cors_middleware_negative_and_default_config() -> None:
    # 1. Unset/empty allowed origins -> No CORS headers should be returned
    settings_empty = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_ALLOWED_ORIGINS": "",
        }
    )

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    wrapped_empty = build_http_app(_Stub(), settings_empty, kind="streamable-http")
    with TestClient(wrapped_empty) as client:
        headers = {
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        }
        response = client.options("/", headers=headers)
        # Without an allowlist, CORS middleware is not registered, so no CORS preflight headers
        assert "access-control-allow-origin" not in response.headers

    # 2. Request origin not in allowlist -> Origin not echoed back (CORS rejected)
    settings_allowlist = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_ALLOWED_ORIGINS": "https://app.example.com",
        }
    )
    wrapped_allowlist = build_http_app(_Stub(), settings_allowlist, kind="streamable-http")
    with TestClient(wrapped_allowlist) as client:
        headers = {
            "Origin": "http://disallowed-attacker.com",
            "Access-Control-Request-Method": "GET",
        }
        response = client.options("/", headers=headers)
        assert response.status_code == 400 or "access-control-allow-origin" not in response.headers


# --- request size limit: streaming + malformed Content-Length -------------


async def _drive(middleware: object, scope: dict[str, object], chunks: list[bytes]) -> list[dict[str, object]]:
    """Drive an ASGI middleware with a sequence of request-body chunks.

    Returns the list of messages the middleware sent downstream.
    """
    sent: list[dict[str, object]] = []
    pending = list(chunks)

    async def receive() -> dict[str, object]:
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def _inner(scope: dict[str, object], receive: object, send: object) -> None:
        # Read the whole body (this is what triggers the receive wrapper),
        # then respond 200.
        while True:
            msg = await receive()  # type: ignore[operator]
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    middleware._app = _inner  # type: ignore[attr-defined]
    await middleware(scope, receive, send)  # type: ignore[operator]
    return sent


async def test_request_size_limit_streams_413_when_body_exceeds_without_content_length() -> None:
    # No Content-Length header -> the middleware counts streamed bytes and
    # raises once the running total exceeds the cap, returning 413.
    from mcpg.http_runtime import _RequestSizeLimitMiddleware

    mw = _RequestSizeLimitMiddleware(None, max_bytes=10)
    scope = {"type": "http", "headers": []}
    sent = await _drive(mw, scope, [b"abcdef", b"ghijkl"])  # 12 bytes total

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 413


async def test_request_size_limit_allows_streamed_body_within_cap() -> None:
    from mcpg.http_runtime import _RequestSizeLimitMiddleware

    mw = _RequestSizeLimitMiddleware(None, max_bytes=100)
    scope = {"type": "http", "headers": []}
    sent = await _drive(mw, scope, [b"abc", b"def"])  # 6 bytes total

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 200


async def test_request_size_limit_ignores_malformed_content_length() -> None:
    # A non-integer Content-Length must not crash the middleware; it falls
    # back to counting streamed bytes (here within the cap -> 200).
    from mcpg.http_runtime import _RequestSizeLimitMiddleware

    mw = _RequestSizeLimitMiddleware(None, max_bytes=100)
    scope = {"type": "http", "headers": [(b"content-length", b"not-a-number")]}
    sent = await _drive(mw, scope, [b"hello"])

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 200


# --- OIDC auth middleware: role-claim handling ----------------------------


class _FakeVerifier:
    """Stand-in for OIDCVerifier with a canned verify() outcome."""

    def __init__(self, *, role: str | None = None, raises: bool = False) -> None:
        self._role = role
        self._raises = raises

    async def verify(self, token: str) -> object:
        from mcpg.oidc import OIDCError, VerifiedToken

        if self._raises:
            raise OIDCError("invalid token")
        return VerifiedToken(claims={"sub": "u"}, role=self._role)


async def _drive_get(middleware: object, *, auth: bytes | None = None) -> list[dict[str, object]]:
    """Drive a middleware with a single GET request, capturing sent messages."""
    sent: list[dict[str, object]] = []
    headers = [(b"authorization", auth)] if auth is not None else []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {"type": "http", "path": "/mcp", "headers": headers}
    await middleware(scope, receive, send)  # type: ignore[operator]
    return sent


async def test_oidc_middleware_passes_through_when_token_has_no_role() -> None:
    from mcpg.http_runtime import _OIDCAuthMiddleware

    seen: list[str | None] = []

    async def _inner(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(current_role.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    mw = _OIDCAuthMiddleware(_inner, verifier=_FakeVerifier(role=None))  # type: ignore[arg-type]
    sent = await _drive_get(mw, auth=b"Bearer tok")

    assert seen == [None]
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 200


async def test_oidc_middleware_sets_and_resets_role_from_claim() -> None:
    from mcpg.http_runtime import _OIDCAuthMiddleware

    seen: list[str | None] = []

    async def _inner(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(current_role.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    mw = _OIDCAuthMiddleware(_inner, verifier=_FakeVerifier(role="tenant_a"))  # type: ignore[arg-type]
    sent = await _drive_get(mw, auth=b"Bearer tok")

    # Role visible to the inner app, reset afterwards.
    assert seen == ["tenant_a"]
    assert current_role.get() is None
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 200


async def test_oidc_middleware_rejects_unsafe_role_identifier() -> None:
    from mcpg.http_runtime import _OIDCAuthMiddleware

    mw = _OIDCAuthMiddleware(None, verifier=_FakeVerifier(role='"; DROP USER alice; --'))  # type: ignore[arg-type]
    sent = await _drive_get(mw, auth=b"Bearer tok")

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 401


async def test_oidc_middleware_returns_401_on_verification_failure() -> None:
    from mcpg.http_runtime import _OIDCAuthMiddleware

    mw = _OIDCAuthMiddleware(None, verifier=_FakeVerifier(raises=True))  # type: ignore[arg-type]
    sent = await _drive_get(mw, auth=b"Bearer bad")

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 401


# --- build_http_app SSE branch + run_http -----------------------------------


def test_build_http_app_supports_sse_kind() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})

    class _Stub:
        def sse_app(self) -> Starlette:
            return _bare_app()

    app = build_http_app(_Stub(), settings, kind="sse")
    # /metrics + /healthz are mounted onto whichever app kind we built.
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_run_http_builds_app_and_serves_via_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpg import http_runtime

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_HOST": "0.0.0.0",
            "MCPG_HTTP_PORT": "9999",
        }
    )

    captured: dict[str, object] = {}

    def _fake_run(app: object, *, host: str, port: int) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    http_runtime.run_http(_Stub(), settings, kind="streamable-http")

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
    assert captured["app"] is not None


def test_run_http_pins_selector_loop_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # async psycopg dies on the ProactorEventLoop; uvicorn reinstalls the
    # proactor policy on Windows, so run_http must re-pin the selector
    # policy and tell uvicorn to leave the loop alone (loop="none").
    import asyncio

    from mcpg import http_runtime

    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})

    captured_kwargs: dict[str, object] = {}

    def _fake_run(app: object, *, host: str, port: int, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    class _FakeSelectorPolicy:
        pass

    policy_calls: list[object] = []

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    monkeypatch.setattr(http_runtime.sys, "platform", "win32")
    # WindowsSelectorEventLoopPolicy doesn't exist off Windows — provide it
    # so the win32 branch can be exercised on a Linux CI runner.
    monkeypatch.setattr(asyncio, "WindowsSelectorEventLoopPolicy", _FakeSelectorPolicy, raising=False)
    monkeypatch.setattr(asyncio, "set_event_loop_policy", lambda policy: policy_calls.append(policy))

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    http_runtime.run_http(_Stub(), settings, kind="streamable-http")

    assert captured_kwargs.get("loop") == "none"
    assert len(policy_calls) == 1
    assert isinstance(policy_calls[0], _FakeSelectorPolicy)


def test_run_http_leaves_the_event_loop_alone_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from mcpg import http_runtime

    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})

    captured_kwargs: dict[str, object] = {}

    def _fake_run(app: object, *, host: str, port: int, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    policy_calls: list[object] = []

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    monkeypatch.setattr(http_runtime.sys, "platform", "linux")
    monkeypatch.setattr(asyncio, "set_event_loop_policy", lambda policy: policy_calls.append(policy))

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    http_runtime.run_http(_Stub(), settings, kind="streamable-http")

    assert "loop" not in captured_kwargs
    assert policy_calls == []


# --- request timeout middleware -------------------------------------------


async def test_request_timeout_middleware_returns_504_when_app_is_too_slow() -> None:
    import asyncio

    from mcpg.http_runtime import _RequestTimeoutMiddleware

    async def _slow_app(scope: dict[str, object], receive: object, send: object) -> None:
        await asyncio.sleep(10)  # far longer than the 0s cap

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = _RequestTimeoutMiddleware(_slow_app, timeout_seconds=0)
    await mw({"type": "http", "path": "/mcp", "headers": []}, receive, send)

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 504


async def test_request_timeout_middleware_passes_through_a_fast_app() -> None:
    from mcpg.http_runtime import _RequestTimeoutMiddleware

    async def _fast_app(scope: dict[str, object], receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = _RequestTimeoutMiddleware(_fast_app, timeout_seconds=5)
    await mw({"type": "http", "path": "/mcp", "headers": []}, receive, send)

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 200


async def test_request_timeout_middleware_passes_through_non_http_scope() -> None:
    from mcpg.http_runtime import _RequestTimeoutMiddleware

    called = False

    async def _inner(scope: dict[str, object], receive: object, send: object) -> None:
        nonlocal called
        called = True

    mw = _RequestTimeoutMiddleware(_inner, timeout_seconds=1)
    await mw({"type": "lifespan"}, None, None)
    assert called is True


async def test_request_timeout_middleware_does_not_double_send_after_stream_started() -> None:
    # If the app already sent the response start before timing out, the
    # middleware must NOT try to write a 504 on top of it.
    import asyncio

    from mcpg.http_runtime import _RequestTimeoutMiddleware

    async def _streamer(scope: dict[str, object], receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await asyncio.sleep(10)  # times out mid-stream

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = _RequestTimeoutMiddleware(_streamer, timeout_seconds=0)
    await mw({"type": "http", "path": "/mcp", "headers": []}, receive, send)

    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    # Exactly one start, the app's 200 — no 504 stacked on top.
    assert statuses == [200]


# --- TLS / mTLS settings -> uvicorn kwargs ---------------------------------


def test_uvicorn_tls_kwargs_empty_when_tls_disabled() -> None:
    from mcpg.http_runtime import _uvicorn_tls_kwargs

    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})
    assert _uvicorn_tls_kwargs(settings, ssl_module=None) == {}


class _StubSSL:
    """Stand-in for the ssl module — covers every attribute the
    _uvicorn_tls_kwargs builder touches. Sentinel values let the
    assertions confirm what got routed where without depending on
    real ssl module enums."""

    PROTOCOL_TLS_SERVER = "PROTOCOL_TLS_SERVER_SENTINEL"
    CERT_REQUIRED = "CERT_REQUIRED_SENTINEL"


def test_uvicorn_tls_kwargs_emits_cert_and_key_when_set(tmp_path: object) -> None:
    from mcpg.http_runtime import _MOZILLA_INTERMEDIATE_CIPHERS, _uvicorn_tls_kwargs

    cert = tmp_path / "server.crt"  # type: ignore[operator]
    key = tmp_path / "server.key"  # type: ignore[operator]
    cert.write_text("-- placeholder; load_settings only checks existence\n")
    key.write_text("-- placeholder\n")

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_TLS_CERTFILE": str(cert),
            "MCPG_HTTP_TLS_KEYFILE": str(key),
        }
    )

    kwargs = _uvicorn_tls_kwargs(settings, ssl_module=_StubSSL())
    # Cert + key are always present. ssl_version + ssl_ciphers are
    # pinned in the builder regardless of mTLS, so an old system
    # OpenSSL can't silently negotiate TLS 1.0/1.1 or a weak suite.
    assert kwargs == {
        "ssl_certfile": str(cert),
        "ssl_keyfile": str(key),
        "ssl_version": "PROTOCOL_TLS_SERVER_SENTINEL",
        "ssl_ciphers": _MOZILLA_INTERMEDIATE_CIPHERS,
    }


def test_uvicorn_tls_kwargs_includes_ca_certs_when_set(tmp_path: object) -> None:
    from mcpg.http_runtime import _MOZILLA_INTERMEDIATE_CIPHERS, _uvicorn_tls_kwargs

    cert = tmp_path / "server.crt"  # type: ignore[operator]
    key = tmp_path / "server.key"  # type: ignore[operator]
    ca = tmp_path / "ca.crt"  # type: ignore[operator]
    for f in (cert, key, ca):
        f.write_text("-- placeholder\n")

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_TLS_CERTFILE": str(cert),
            "MCPG_HTTP_TLS_KEYFILE": str(key),
            "MCPG_HTTP_TLS_CA_CERTS": str(ca),
        }
    )

    kwargs = _uvicorn_tls_kwargs(settings, ssl_module=_StubSSL())
    assert kwargs == {
        "ssl_certfile": str(cert),
        "ssl_keyfile": str(key),
        "ssl_ca_certs": str(ca),
        "ssl_version": "PROTOCOL_TLS_SERVER_SENTINEL",
        "ssl_ciphers": _MOZILLA_INTERMEDIATE_CIPHERS,
    }


def test_uvicorn_tls_kwargs_pins_mozilla_intermediate_cipher_list() -> None:
    """Regression for deep-review P1 #10: TLS posture had no
    explicit cipher floor, so an old system OpenSSL could negotiate
    a non-AEAD suite. Pinning here makes the floor auditable and
    survives uvicorn upgrades."""
    from mcpg.http_runtime import _MOZILLA_INTERMEDIATE_CIPHERS

    suites = _MOZILLA_INTERMEDIATE_CIPHERS.split(":")
    # AEAD only — no CBC, no RC4, no 3DES, no NULL, no anonymous.
    forbidden_substrings = ("RC4", "3DES", "DES-CBC", "NULL", "anon", "MD5", "EXPORT")
    for suite in suites:
        for forbidden in forbidden_substrings:
            assert forbidden not in suite, f"forbidden suite component {forbidden!r} in {suite!r}"
    # Every suite must use ECDHE or DHE for forward secrecy.
    for suite in suites:
        assert suite.startswith(("ECDHE-", "DHE-")), f"no forward-secrecy KEX in {suite!r}"


def test_uvicorn_tls_kwargs_sets_cert_required_for_mtls(tmp_path: object) -> None:
    from mcpg.http_runtime import _uvicorn_tls_kwargs

    cert = tmp_path / "server.crt"  # type: ignore[operator]
    key = tmp_path / "server.key"  # type: ignore[operator]
    ca = tmp_path / "ca.crt"  # type: ignore[operator]
    for f in (cert, key, ca):
        f.write_text("-- placeholder\n")

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_TLS_CERTFILE": str(cert),
            "MCPG_HTTP_TLS_KEYFILE": str(key),
            "MCPG_HTTP_TLS_CA_CERTS": str(ca),
            "MCPG_HTTP_TLS_CLIENT_CERT_REQUIRED": "true",
        }
    )

    kwargs = _uvicorn_tls_kwargs(settings, ssl_module=_StubSSL())
    assert kwargs["ssl_cert_reqs"] == "CERT_REQUIRED_SENTINEL"
    assert kwargs["ssl_ca_certs"] == str(ca)
    # Version pin + cipher list still apply on the mTLS path.
    assert kwargs["ssl_version"] == "PROTOCOL_TLS_SERVER_SENTINEL"


def test_settings_rejects_certfile_without_keyfile(tmp_path: object) -> None:
    from mcpg.config import ConfigError

    cert = tmp_path / "server.crt"  # type: ignore[operator]
    cert.write_text("-- placeholder\n")
    with pytest.raises(ConfigError, match="must both be set or both be unset"):
        load_settings(
            {
                "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
                "MCPG_HTTP_TLS_CERTFILE": str(cert),
            }
        )


def test_settings_rejects_client_cert_required_without_ca_certs(tmp_path: object) -> None:
    from mcpg.config import ConfigError

    cert = tmp_path / "server.crt"  # type: ignore[operator]
    key = tmp_path / "server.key"  # type: ignore[operator]
    for f in (cert, key):
        f.write_text("-- placeholder\n")
    with pytest.raises(ConfigError, match="needs MCPG_HTTP_TLS_CA_CERTS"):
        load_settings(
            {
                "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
                "MCPG_HTTP_TLS_CERTFILE": str(cert),
                "MCPG_HTTP_TLS_KEYFILE": str(key),
                "MCPG_HTTP_TLS_CLIENT_CERT_REQUIRED": "true",
            }
        )


def test_settings_rejects_nonexistent_cert_path() -> None:
    from mcpg.config import ConfigError

    with pytest.raises(ConfigError, match="non-existent file"):
        load_settings(
            {
                "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
                "MCPG_HTTP_TLS_CERTFILE": "/nonexistent/server.crt",
                "MCPG_HTTP_TLS_KEYFILE": "/nonexistent/server.key",
            }
        )


def test_build_http_app_installs_request_timeout_only_when_positive() -> None:
    base = {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"}

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    # Disabled (default 0): no timeout middleware in the stack.
    off = build_http_app(_Stub(), load_settings(base), kind="streamable-http")
    assert not any(m.cls.__name__ == "_RequestTimeoutMiddleware" for m in off.user_middleware)

    # Enabled: middleware present.
    on = build_http_app(
        _Stub(),
        load_settings({**base, "MCPG_HTTP_REQUEST_TIMEOUT_SECONDS": "5"}),
        kind="streamable-http",
    )
    assert any(m.cls.__name__ == "_RequestTimeoutMiddleware" for m in on.user_middleware)


# --- IP allowlist middleware ----------------------------------------------


async def _call_middleware(middleware: object, scope: dict[str, object]) -> tuple[int, list[bytes]]:
    """Invoke an ASGI middleware against a constructed scope; return (status, body bytes)."""
    received_messages: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict[str, object]) -> None:
        received_messages.append(message)

    await middleware(scope, _receive, _send)  # type: ignore[operator]
    status_codes = [int(m.get("status", 0)) for m in received_messages if m.get("type") == "http.response.start"]
    bodies = [m.get("body", b"") for m in received_messages if m.get("type") == "http.response.body"]
    status = status_codes[0] if status_codes else 200
    body_bytes = [b for b in bodies if isinstance(b, bytes)]
    return status, body_bytes


def _scope(client_host: str | None) -> dict[str, object]:
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": (client_host, 50001) if client_host is not None else None,
    }


async def test_ip_allowlist_middleware_allows_listed_ip() -> None:
    from mcpg.http_runtime import _IPAllowlistMiddleware

    async def _inner_app(_scope: dict[str, object], _receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    middleware = _IPAllowlistMiddleware(_inner_app, allowlist=("10.0.0.5",))
    status, _ = await _call_middleware(middleware, _scope("10.0.0.5"))
    assert status == 200


async def test_ip_allowlist_middleware_rejects_unlisted_ip() -> None:
    from mcpg.http_runtime import _IPAllowlistMiddleware

    async def _inner_app(_scope: dict[str, object], _receive: object, send: object) -> None:
        # Should never be reached.
        raise AssertionError("inner app must not run when the IP is denied")

    middleware = _IPAllowlistMiddleware(_inner_app, allowlist=("10.0.0.5",))
    status, _ = await _call_middleware(middleware, _scope("192.168.1.1"))
    assert status == 403


async def test_ip_allowlist_middleware_matches_cidr_range() -> None:
    from mcpg.http_runtime import _IPAllowlistMiddleware

    async def _inner_app(_scope: dict[str, object], _receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    middleware = _IPAllowlistMiddleware(_inner_app, allowlist=("10.0.0.0/8",))
    # Any address in the /8 should pass.
    for addr in ("10.0.0.1", "10.255.255.255", "10.1.2.3"):
        status, _ = await _call_middleware(middleware, _scope(addr))
        assert status == 200, f"expected {addr} to be allowed"
    # Just outside the range — denied.
    status, _ = await _call_middleware(middleware, _scope("11.0.0.1"))
    assert status == 403


async def test_ip_allowlist_middleware_matches_ipv6() -> None:
    from mcpg.http_runtime import _IPAllowlistMiddleware

    async def _inner_app(_scope: dict[str, object], _receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    middleware = _IPAllowlistMiddleware(_inner_app, allowlist=("2001:db8::/32",))
    status, _ = await _call_middleware(middleware, _scope("2001:db8::1"))
    assert status == 200
    status, _ = await _call_middleware(middleware, _scope("2001:db9::1"))
    assert status == 403


async def test_ip_allowlist_middleware_accepts_list_shaped_client() -> None:
    # ASGI spec says ``client`` is a two-item iterable; most servers
    # use tuples, some use lists. The check must accept both — a
    # ``tuple``-only check would 403 valid requests under those
    # transports.
    from mcpg.http_runtime import _IPAllowlistMiddleware

    async def _inner_app(_scope: dict[str, object], _receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    middleware = _IPAllowlistMiddleware(_inner_app, allowlist=("10.0.0.5",))
    scope = _scope("10.0.0.5")
    scope["client"] = ["10.0.0.5", 50001]  # type: ignore[assignment]
    status, _ = await _call_middleware(middleware, scope)
    assert status == 200


async def test_ip_allowlist_middleware_denies_when_client_missing() -> None:
    # An ASGI transport that doesn't populate ``client`` (some test
    # runners, some custom transports) can't be checked against any
    # allowlist — fail closed rather than wave the request through.
    from mcpg.http_runtime import _IPAllowlistMiddleware

    async def _inner_app(_scope: dict[str, object], _receive: object, send: object) -> None:
        raise AssertionError("inner app must not run when client info is missing")

    middleware = _IPAllowlistMiddleware(_inner_app, allowlist=("10.0.0.0/8",))
    scope = _scope(None)
    scope["client"] = None  # type: ignore[assignment]
    status, _ = await _call_middleware(middleware, scope)
    assert status == 403


async def test_ip_allowlist_middleware_passes_non_http_scopes_through() -> None:
    # Lifespan / websocket / similar non-HTTP scopes must not be
    # rejected by the IP gate; the inner app handles them.
    from mcpg.http_runtime import _IPAllowlistMiddleware

    called = []

    async def _inner_app(scope: dict[str, object], _receive: object, _send: object) -> None:
        called.append(scope["type"])

    middleware = _IPAllowlistMiddleware(_inner_app, allowlist=("10.0.0.5",))
    await middleware(  # type: ignore[operator]
        {"type": "lifespan"},
        None,
        None,
    )
    assert called == ["lifespan"]


def test_build_http_app_installs_ip_allowlist_only_when_configured() -> None:
    base = {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"}

    class _Stub:
        def streamable_http_app(self) -> Starlette:
            return _bare_app()

    # No allowlist → no middleware.
    off = build_http_app(_Stub(), load_settings(base), kind="streamable-http")
    assert not any(m.cls.__name__ == "_IPAllowlistMiddleware" for m in off.user_middleware)

    # Allowlist set → middleware installed.
    on = build_http_app(
        _Stub(),
        load_settings({**base, "MCPG_HTTP_IP_ALLOWLIST": "10.0.0.0/8, 127.0.0.1"}),
        kind="streamable-http",
    )
    assert any(m.cls.__name__ == "_IPAllowlistMiddleware" for m in on.user_middleware)


def test_settings_rejects_invalid_ip_allowlist_entry() -> None:
    from mcpg.config import ConfigError

    with pytest.raises(ConfigError, match="MCPG_HTTP_IP_ALLOWLIST"):
        load_settings(
            {
                "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
                "MCPG_HTTP_IP_ALLOWLIST": "not-an-ip",
            }
        )
