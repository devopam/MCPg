"""Tests for per-request PG role multi-tenancy (Phase 1.4)."""

from __future__ import annotations

import asyncio

import pytest

from mcpg.tenancy import (
    _ROLE_SCOPE_KEY,
    TenancyError,
    TenantRoleContextMiddleware,
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
    "role",
    [
        # Roles needing delimited quoting are legal in PostgreSQL and are now
        # accepted — validate_role returns them unchanged; the SET LOCAL ROLE
        # splice quotes them (embedded quotes doubled) so they can't inject.
        "role-with-dash",
        "role with space",
        "1starts_with_digit",
        "weird$char",
        "role; DROP USER alice",
        '"; DROP USER alice',
    ],
)
def test_validate_role_accepts_delimited_role_names(role: str) -> None:
    assert validate_role(role) == role


@pytest.mark.parametrize("bad", ["", "\x00", "role\x00x", "a" * 64])
def test_validate_role_rejects_non_addressable_names(bad: str) -> None:
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


# --- TenantRoleContextMiddleware (mcp 2.0: ServerMiddleware replaces the ----
# --- old request_ctx-threading hack; see tenancy.py's module docstring) ----


class _FakeRequest:
    def __init__(self, scope: dict[str, object]) -> None:
        self.scope = scope


class _FakeCtx:
    def __init__(self, request: object | None) -> None:
        self.request = request


@pytest.mark.asyncio
async def test_middleware_sets_current_role_from_request_scope() -> None:
    middleware = TenantRoleContextMiddleware()
    ctx = _FakeCtx(_FakeRequest({_ROLE_SCOPE_KEY: "analytics_ro"}))

    seen_role_inside_call_next = {}

    async def call_next(_ctx: object) -> None:
        seen_role_inside_call_next["role"] = resolve_role(None)
        return None

    await middleware(ctx, call_next)  # type: ignore[arg-type]

    assert seen_role_inside_call_next["role"] == "analytics_ro"
    # Reset after call_next returns — no leakage into the next request's task.
    assert current_role.get() is None


@pytest.mark.asyncio
async def test_middleware_clears_role_when_request_has_no_role_scope_key() -> None:
    """Not a no-op: the request object is present (HTTP/SSE), so the
    middleware is authoritative and explicitly sets current_role to None
    (falling through to the static default) rather than skipping the set."""
    middleware = TenantRoleContextMiddleware()
    ctx = _FakeCtx(_FakeRequest({}))

    async def call_next(_ctx: object) -> str | None:
        return resolve_role("static_default")

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]

    assert result == "static_default"


@pytest.mark.asyncio
async def test_middleware_clears_a_stale_inherited_role_when_request_carries_none() -> None:
    """Security-critical regression guard.

    asyncio tasks copy their context from whichever task *spawned* them, not
    from the ASGI task that stashed the role on the scope — so in principle a
    per-request dispatch task could inherit a non-None current_role from a
    long-lived parent task's context (exactly the "session-frozen" hazard the
    old ``_role_from_request`` mechanism existed to work around). A request
    whose own scope carries no role must never observe such a stale value:
    the middleware must clear it, not merely leave it alone, whenever a
    request object is present at all.
    """
    middleware = TenantRoleContextMiddleware()
    stale = current_role.set("tenant_FIRST")  # simulates an inherited stale value
    try:
        ctx = _FakeCtx(_FakeRequest({}))  # this request carries no role header

        async def call_next(_ctx: object) -> str | None:
            return resolve_role("static_default")

        result = await middleware(ctx, call_next)  # type: ignore[arg-type]

        assert result == "static_default"  # NOT "tenant_FIRST"
    finally:
        current_role.reset(stale)


@pytest.mark.asyncio
async def test_middleware_is_noop_on_stdio_where_request_is_none() -> None:
    middleware = TenantRoleContextMiddleware()
    ctx = _FakeCtx(None)

    async def call_next(_ctx: object) -> str | None:
        return resolve_role("static_default")

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]

    assert result == "static_default"


@pytest.mark.asyncio
async def test_middleware_resets_role_even_when_call_next_raises() -> None:
    """Security-critical: a failing request must not leave ``current_role``
    set for whatever task runs next. The ``finally`` block must fire even
    when ``call_next`` (i.e. the actual tool dispatch) raises."""
    middleware = TenantRoleContextMiddleware()
    ctx = _FakeCtx(_FakeRequest({_ROLE_SCOPE_KEY: "tenant_that_blows_up"}))

    async def call_next(_ctx: object) -> None:
        assert current_role.get() == "tenant_that_blows_up"
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await middleware(ctx, call_next)  # type: ignore[arg-type]

    # The exception unwound through the middleware's finally: block, so the
    # role must be reset — a later request sharing this task (there isn't
    # one, per-request tasks are fresh, but we verify the mechanism anyway)
    # would otherwise inherit the failed request's role.
    assert current_role.get() is None


@pytest.mark.asyncio
async def test_middleware_does_not_leak_role_across_sequential_requests() -> None:
    """Two "requests" handled one after another via the same middleware
    instance must not see each other's role: each call sets, then resets,
    its own token — there is no shared mutable state on the middleware."""
    middleware = TenantRoleContextMiddleware()

    async def call_next_capture(_ctx: object) -> str | None:
        return resolve_role(None)

    first_ctx = _FakeCtx(_FakeRequest({_ROLE_SCOPE_KEY: "tenant_one"}))
    first_result = await middleware(first_ctx, call_next_capture)  # type: ignore[arg-type]
    assert first_result == "tenant_one"
    assert current_role.get() is None  # reset after the first "request"

    second_ctx = _FakeCtx(_FakeRequest({}))  # no role header this time
    second_result = await middleware(second_ctx, call_next_capture)  # type: ignore[arg-type]
    assert second_result is None  # NOT "tenant_one" leaking from the first call
    assert current_role.get() is None


@pytest.mark.asyncio
async def test_middleware_does_not_leak_role_across_concurrent_request_tasks() -> None:
    """The property the mcp 2.0 SDK guarantees (and this middleware relies
    on): each inbound request is dispatched to its own asyncio task, so a
    plain ``ContextVar.set()`` in one task is invisible to a sibling task
    running concurrently. Drive two "requests" as actual concurrent tasks
    (mirroring ``Dispatcher.run``) with different roles and confirm neither
    ever observes the other's role, even mid-flight."""
    middleware = TenantRoleContextMiddleware()
    barrier = asyncio.Event()
    observed: dict[str, str | None] = {}

    async def make_request(tenant: str, role: str) -> None:
        ctx = _FakeCtx(_FakeRequest({_ROLE_SCOPE_KEY: role}))

        async def call_next(_ctx: object) -> None:
            # Yield so the other task's request-scoped `.set()` (if it were
            # ever visible across tasks) would have a chance to interfere.
            await barrier.wait()
            observed[tenant] = resolve_role(None)
            return None

        await middleware(ctx, call_next)  # type: ignore[arg-type]

    async def release_after_both_started() -> None:
        await asyncio.sleep(0)
        barrier.set()

    await asyncio.gather(
        make_request("tenant_a", "role_a"),
        make_request("tenant_b", "role_b"),
        release_after_both_started(),
    )

    assert observed == {"tenant_a": "role_a", "tenant_b": "role_b"}
    assert current_role.get() is None


def test_tenant_sql_driver_default_role_is_stored_on_instance() -> None:
    # The driver subclasses SqlDriver; we only verify the new attribute
    # without trying to actually issue queries (which would need a real
    # pool). Connection-level behaviour is covered by the integration
    # tests once the driver wires through. Pass an opaque sentinel as
    # the conn — SqlDriver requires either conn or engine_url.
    driver = TenantSqlDriver(conn=object(), default_role="tenant_a")  # type: ignore[arg-type]
    assert driver._default_role == "tenant_a"
