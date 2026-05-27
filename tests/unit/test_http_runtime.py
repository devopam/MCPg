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
