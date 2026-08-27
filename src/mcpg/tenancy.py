"""Per-request PostgreSQL role multi-tenancy.

One MCPg process can serve many tenants from a single connection
pool by issuing ``SET LOCAL ROLE "<role>"`` at the start of every
transaction. Because ``SET LOCAL`` resets at transaction end, no
state leaks into the next pool checkout — and because the role name
is validated against ``[A-Za-z_][A-Za-z0-9_]*`` (rejected at the
config / middleware boundary), it's safe to interpolate into SQL.

The role for a request is set once, by :class:`TenantRoleContextMiddleware`:

* **Static**: ``MCPG_DEFAULT_ROLE`` — applies to every query when
  no per-request override is present. The HTTP bearer-token /
  stdio paths use this.
* **Per-request**: the streamable-http / sse transports parse
  ``X-MCPG-Role: <role>`` (or the OIDC role claim), validate it, and
  stash it on the request's ASGI ``scope``. The MCP SDK dispatches
  every inbound request to its own asyncio task
  (``mcp.shared.dispatcher.Dispatcher.run``), and
  :class:`TenantRoleContextMiddleware` runs inside that task, at the
  top of the request's dispatch. It reads the role stashed on the
  request's scope and sets :data:`current_role` for the lifetime of
  the request via a ``ContextVar.set()`` / ``.reset()`` pair. Because
  a plain ``ContextVar`` set inside one task is invisible to sibling
  tasks, this is reliably visible to everything that request awaits —
  including several calls deep into the SQL driver — and can't leak
  into any other request's task. :func:`resolve_role` simply reads it
  back. The :class:`TenantSqlDriver` then issues ``SET LOCAL ROLE`` and
  falls back to the static default when the request carried no role.

When neither is configured, the driver is identical to the vendored
:class:`SqlDriver` and zero overhead is added.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from psycopg.rows import dict_row

from mcpg.errors import MCPgError
from mcpg.sql import SqlDriver

logger = logging.getLogger(__name__)

# Mirrors the validator in mcpg.config — duplicated here so this
# module has no import-cycle on Settings.
_ROLE_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Per-request override, set once per inbound request by
# TenantRoleContextMiddleware (stdio: never set, so it stays at its default
# of None and resolve_role falls back to the static default). ``None`` means
# "no override, use the static default". The MCP SDK dispatches every
# inbound request to its own asyncio task, so a ``.set()`` here is scoped to
# that one request and can't leak into any other request's task.
current_role: ContextVar[str | None] = ContextVar("mcpg_current_role", default=None)

# Key under which the HTTP/SSE middlewares stash the validated per-request
# role on the ASGI ``scope``. TenantRoleContextMiddleware reads it from the
# request object it's handed and sets current_role for that request's task.
_ROLE_SCOPE_KEY = "mcpg.tenant_role"


class TenancyError(MCPgError, ValueError):
    """Raised when a role name fails validation."""


def validate_role(role: str) -> str:
    """Return ``role`` unchanged if safe; raise otherwise."""
    if not _ROLE_IDENTIFIER.match(role):
        raise TenancyError(f"role name {role!r} must match [A-Za-z_][A-Za-z0-9_]*")
    return role


def resolve_role(default: str | None) -> str | None:
    """Return the role for the current request.

    :data:`current_role` is set once per inbound request by
    :class:`TenantRoleContextMiddleware`, which runs inside that request's own
    asyncio task (the MCP SDK dispatches every request to its own task — see
    the middleware's docstring) — so the ContextVar is reliably scoped to this
    request on every transport, not just stdio. ``None`` means "do nothing —
    use the role the pool was opened with".
    """
    override = current_role.get()
    if override is not None:
        return override
    return default


class TenantRoleContextMiddleware:
    """Sets :data:`current_role` from the request's stashed tenant role.

    HTTP/SSE middlewares (:class:`mcpg.http_runtime._TenantRoleMiddleware`,
    :class:`mcpg.http_runtime._OIDCAuthMiddleware`) validate ``X-MCPG-Role`` /
    the OIDC role claim and stash it on the ASGI request's ``scope`` under
    :data:`_ROLE_SCOPE_KEY`. This middleware runs inside the MCP SDK's
    per-request dispatch — each inbound request gets its own asyncio task
    (``mcp.shared.dispatcher.Dispatcher.run``), so setting a plain
    :class:`~contextvars.ContextVar` here is reliably visible to everything
    that request's tool call awaits, including the SQL driver several calls
    deep — unlike a naive set from the ASGI middleware layer, which would be
    invisible by the time a tool function runs in the SDK's own task.

    Whenever ``ctx.request`` is present (HTTP/SSE) this middleware is
    **authoritative** for the request: it always calls ``current_role.set(...)``
    with the scope's role or ``None``, never leaving the prior value in place.
    This matters because asyncio tasks copy their context from whichever task
    *spawned* them, not from the ASGI task that stashed the scope value — a
    per-request dispatch task can in principle inherit a non-``None``
    :data:`current_role` from a long-lived parent task's context. Always
    setting (instead of only setting when a role is present) means a request
    that carries no role can never observe a stale value some earlier
    request's task left behind; it deterministically resolves to the static
    default via :func:`resolve_role`, exactly like the old
    ``_role_from_request`` mechanism this middleware replaces.

    A true no-op only on stdio (``ctx.request`` is ``None`` there — there is
    no per-message scope to read, so :data:`current_role` is left untouched
    for stdio's own, non-HTTP path).
    """

    async def __call__(
        self,
        ctx: ServerRequestContext[object, object],
        call_next: CallNext,
    ) -> HandlerResult:
        request = getattr(ctx, "request", None)
        if request is None:
            return await call_next(ctx)
        scope = getattr(request, "scope", None)
        raw_role = scope.get(_ROLE_SCOPE_KEY) if isinstance(scope, dict) else None
        role = raw_role if isinstance(raw_role, str) else None
        token = current_role.set(role)
        try:
            return await call_next(ctx)
        finally:
            current_role.reset(token)


# Static structural-conformance check: TenantRoleContextMiddleware must match
# the ServerMiddleware Protocol's call signature (registered directly as
# AuditedMCPServer(..., middleware=[TenantRoleContextMiddleware()])). Never
# evaluated at runtime — mypy checks the assignment, nothing constructs this.
if TYPE_CHECKING:
    _tenant_role_middleware_matches_protocol: ServerMiddleware[Any] = TenantRoleContextMiddleware()


class TenantSqlDriver(SqlDriver):
    """``SqlDriver`` subclass that prepends ``SET LOCAL ROLE`` to every txn.

    The vendored driver opens a fresh transaction per query (explicit
    ``BEGIN TRANSACTION READ ONLY`` for read-only, implicit per-statement
    for writes). To make ``SET LOCAL ROLE`` valid for write paths too,
    we wrap every execution in an explicit transaction.

    When :func:`resolve_role` returns ``None``, the override path is
    skipped and the call falls back to the upstream method unchanged
    — keeping the cost at exactly one ContextVar lookup per query
    when tenancy isn't configured.
    """

    def __init__(self, *args: Any, default_role: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._default_role = default_role

    async def _execute_with_connection(  # type: ignore[no-untyped-def]
        self,
        connection,
        query,
        params,
        force_readonly,
        row_limit=None,
    ):
        role = resolve_role(self._default_role)
        if role is None:
            return await super()._execute_with_connection(
                connection, query, params, force_readonly, row_limit=row_limit
            )
        return await _execute_with_role(connection, query, params, force_readonly, role, row_limit=row_limit)


# C901 rationale: multi-tenant RLS execution path -- role validation,
# explicit transaction lifecycle so `SET LOCAL ROLE` is valid on write
# paths too, and the same transaction-commit/rollback state machine as
# sql/driver.py's `_execute_with_connection` (mirrored intentionally, per
# the docstring) -- restructuring risks a tenant-isolation regression (the
# wrong role active, or a transaction left open) for no benefit.
async def _execute_with_role(  # noqa: C901
    connection: Any,
    query: str,
    params: Any,
    force_readonly: bool,
    role: str,
    row_limit: int | None = None,
) -> Any:
    """Run ``query`` inside an explicit transaction with ``SET LOCAL ROLE``.

    Mirrors the upstream :meth:`SqlDriver._execute_with_connection`
    flow (begin → execute → fetch / commit / rollback) but always
    opens an explicit transaction so ``SET LOCAL`` is valid even on
    write paths, and resets the role on every exit branch.
    """
    # Defence-in-depth — role is already validated at config / middleware,
    # but a misconfigured caller could still pass an unvalidated string.
    validate_role(role)
    transaction_started = False
    try:
        async with connection.cursor(row_factory=dict_row) as cursor:
            if force_readonly:
                await cursor.execute("BEGIN TRANSACTION READ ONLY")
            else:
                await cursor.execute("BEGIN")
            transaction_started = True

            await cursor.execute(f'SET LOCAL ROLE "{role}"')

            if params:
                await cursor.execute(query, params)
            else:
                await cursor.execute(query)

            while cursor.nextset():
                pass

            if cursor.description is None:
                # No result set — DDL / DML without RETURNING.
                if force_readonly:
                    await cursor.execute("ROLLBACK")
                else:
                    await cursor.execute("COMMIT")
                transaction_started = False
                return None

            rows = await cursor.fetchmany(row_limit) if row_limit is not None else await cursor.fetchall()
            if force_readonly:
                await cursor.execute("ROLLBACK")
            else:
                await cursor.execute("COMMIT")
            transaction_started = False
            return [SqlDriver.RowResult(cells=dict(row)) for row in rows]
    except BaseException:
        if transaction_started:
            try:
                await connection.rollback()
            except asyncio.CancelledError:
                # Re-raise cancellation so the caller's cancel scope
                # actually unwinds; never swallow it inside a fallback.
                raise
            except Exception as rollback_error:
                logger.error(
                    "Error rolling back transaction during role-wrapped execute: %s",
                    rollback_error,
                    exc_info=True,
                )
        raise
