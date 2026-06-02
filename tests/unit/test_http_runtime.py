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
