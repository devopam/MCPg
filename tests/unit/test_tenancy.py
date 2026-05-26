"""Tests for per-request PG role multi-tenancy (Phase 1.4)."""

from __future__ import annotations

import pytest

from mcpg.tenancy import (
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
    token = current_role.set("tenant_42")
    try:
        assert resolve_role(default="static_default") == "tenant_42"
    finally:
        current_role.reset(token)


def test_tenant_sql_driver_default_role_is_stored_on_instance() -> None:
    # The driver subclasses SqlDriver; we only verify the new attribute
    # without trying to actually issue queries (which would need a real
    # pool). Connection-level behaviour is covered by the integration
    # tests once the driver wires through. Pass an opaque sentinel as
    # the conn — SqlDriver requires either conn or engine_url.
    driver = TenantSqlDriver(conn=object(), default_role="tenant_a")  # type: ignore[arg-type]
    assert driver._default_role == "tenant_a"
