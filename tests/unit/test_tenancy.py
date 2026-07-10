"""Tests for per-request PG role multi-tenancy (Phase 1.4)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.lowlevel.server import request_ctx

from mcpg.tenancy import (
    _ROLE_SCOPE_KEY,
    TenancyError,
    TenantSqlDriver,
    current_role,
    resolve_role,
    validate_role,
)


def test_validate_role_accepts_safe_identifiers() -> None:
    assert validate_role("app_reader") == "app_reader"
    assert validate_role("_internal") == "_internal"
    assert validate_role("Tenant42") == "Tenant42"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        '"; DROP USER alice',
        "role-with-dash",
        "role with space",
        "1starts_with_digit",
        "weird$char",
        "role; DROP USER alice",
    ],
)
def test_validate_role_rejects_unsafe_identifiers(bad: str) -> None:
    with pytest.raises(TenancyError):
        validate_role(bad)


def test_resolve_role_returns_default_when_contextvar_is_unset() -> None:
    # ContextVar defaults to None; resolution falls back to the
    # static default.
    assert resolve_role(default="readonly_role") == "readonly_role"
    assert resolve_role(default=None) is None


def test_resolve_role_prefers_contextvar_over_default() -> None:
    # No HTTP request context (the stdio path) → the ContextVar wins.
    token = current_role.set("tenant_42")
    try:
        assert resolve_role(default="static_default") == "tenant_42"
    finally:
        current_role.reset(token)


# --- HTTP/SSE per-message request path (roadmap: 0.6.11 tenancy fix) --------


def _http_request_ctx(scope: dict[str, object]) -> object:
    """A stand-in for the SDK's per-message RequestContext carrying a request."""
    return SimpleNamespace(request=SimpleNamespace(scope=scope))


def test_resolve_role_uses_per_message_request_role_on_http() -> None:
    token = request_ctx.set(_http_request_ctx({_ROLE_SCOPE_KEY: "tenant_b"}))
    try:
        assert resolve_role(default="static_default") == "tenant_b"
    finally:
        request_ctx.reset(token)


def test_http_request_role_is_authoritative_over_frozen_contextvar() -> None:
    """The bug this fix closes: on HTTP/SSE the dispatch task's ``current_role``
    is frozen to the session's FIRST request. A later request that carries no
    role header must resolve to the static default — NOT the frozen value."""
    frozen = current_role.set("tenant_FIRST")  # the session-frozen role
    req = request_ctx.set(_http_request_ctx({}))  # this request: no role header
    try:
        assert resolve_role(default="static_default") == "static_default"
    finally:
        request_ctx.reset(req)
        current_role.reset(frozen)


def test_http_request_role_wins_over_contextvar() -> None:
    # A per-message role overrides whatever the frozen ContextVar holds.
    frozen = current_role.set("tenant_FIRST")
    req = request_ctx.set(_http_request_ctx({_ROLE_SCOPE_KEY: "tenant_CURRENT"}))
    try:
        assert resolve_role(default="static_default") == "tenant_CURRENT"
    finally:
        request_ctx.reset(req)
        current_role.reset(frozen)


def test_tenant_sql_driver_default_role_is_stored_on_instance() -> None:
    # The driver subclasses SqlDriver; we only verify the new attribute
    # without trying to actually issue queries (which would need a real
    # pool). Connection-level behaviour is covered by the integration
    # tests once the driver wires through. Pass an opaque sentinel as
    # the conn — SqlDriver requires either conn or engine_url.
    driver = TenantSqlDriver(conn=object(), default_role="tenant_a")  # type: ignore[arg-type]
    assert driver._default_role == "tenant_a"
