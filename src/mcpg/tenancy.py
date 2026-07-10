"""Per-request PostgreSQL role multi-tenancy.

One MCPg process can serve many tenants from a single connection
pool by issuing ``SET LOCAL ROLE "<role>"`` at the start of every
transaction. Because ``SET LOCAL`` resets at transaction end, no
state leaks into the next pool checkout — and because the role name
is validated against ``[A-Za-z_][A-Za-z0-9_]*`` (rejected at the
config / middleware boundary), it's safe to interpolate into SQL.

Two ways to set the role for a request:

* **Static**: ``MCPG_DEFAULT_ROLE`` — applies to every query when
  no per-request override is present. The HTTP bearer-token /
  stdio paths use this.
* **Per-request**: the streamable-http / sse transports parse
  ``X-MCPG-Role: <role>`` (or the OIDC role claim), validate it, and
  stash it on the request's ASGI ``scope``. FastMCP dispatches tool
  calls in a long-lived *per-session* task, so a plain ContextVar set
  in the request's ASGI task never reaches it — but the MCP SDK threads
  the *per-message* request into that task via its ``request_ctx``, so
  :func:`resolve_role` reads the authoritative role from the request's
  scope at query time. The :class:`TenantSqlDriver` then issues
  ``SET LOCAL ROLE`` and falls back to the static default when the
  request carried no role.

When neither is configured, the driver is identical to the vendored
:class:`SqlDriver` and zero overhead is added.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextvars import ContextVar
from typing import Any

from psycopg.rows import dict_row

from mcpg.sql import SqlDriver

logger = logging.getLogger(__name__)

# Mirrors the validator in mcpg.config — duplicated here so this
# module has no import-cycle on Settings.
_ROLE_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Per-request override for the stdio path (and a legacy fallback). ``None``
# means "no override, use the static default". IMPORTANT: on the HTTP/SSE
# transports this ContextVar is NOT authoritative — FastMCP dispatches tool
# calls in a long-lived per-session task whose context is copied once at
# session creation, so a role set in a *later* request's ASGI task never
# reaches it. The per-message request path (see :func:`_role_from_request`)
# is authoritative there.
current_role: ContextVar[str | None] = ContextVar("mcpg_current_role", default=None)

# Key under which the HTTP/SSE middlewares stash the validated per-request
# role on the ASGI ``scope``. The MCP SDK threads that same request (and its
# scope) into the tool-dispatch task via its ``request_ctx``, so the driver
# reads the authoritative per-message role at query time.
_ROLE_SCOPE_KEY = "mcpg.tenant_role"


class TenancyError(ValueError):
    """Raised when a role name fails validation."""


def validate_role(role: str) -> str:
    """Return ``role`` unchanged if safe; raise otherwise."""
    if not _ROLE_IDENTIFIER.match(role):
        raise TenancyError(f"role name {role!r} must match [A-Za-z_][A-Za-z0-9_]*")
    return role


def _role_from_request() -> tuple[bool, str | None]:
    """Read the per-message request's role from the MCP SDK's request context.

    Returns ``(has_http_request, role)``:

    * ``has_http_request`` is ``True`` when the current tool call is being
      dispatched for an HTTP/SSE message (the SDK set ``request_ctx`` with a
      Starlette request). In that case the request's scope is authoritative:
      ``role`` is the value the tenant middleware stashed (or ``None`` when the
      request carried no ``X-MCPG-Role`` / role claim) — and the caller must
      NOT consult :data:`current_role`, which on these transports is frozen to
      the session's first request.
    * ``(False, None)`` when there is no HTTP request (stdio, or any path
      without an SDK request context) — the caller uses the ContextVar path.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx
    except Exception:  # SDK internals moved — fail safe to the ContextVar path
        return (False, None)
    try:
        ctx = request_ctx.get()
    except LookupError:
        return (False, None)
    scope = getattr(getattr(ctx, "request", None), "scope", None)
    if not isinstance(scope, dict):
        return (False, None)
    role = scope.get(_ROLE_SCOPE_KEY)
    return (True, role if isinstance(role, str) else None)


def resolve_role(default: str | None) -> str | None:
    """Return the role for the current request.

    On HTTP/SSE the per-message request is authoritative: its stashed role, or
    the static ``default`` when the request carried no role — never the
    session-frozen :data:`current_role`. On stdio (no request context) the
    :data:`current_role` ContextVar wins, falling back to ``default``. ``None``
    means "do nothing — use the role the pool was opened with".
    """
    has_request, request_role = _role_from_request()
    if has_request:
        return request_role if request_role is not None else default
    override = current_role.get()
    if override is not None:
        return override
    return default


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
    ):
        role = resolve_role(self._default_role)
        if role is None:
            return await super()._execute_with_connection(connection, query, params, force_readonly)
        return await _execute_with_role(connection, query, params, force_readonly, role)


async def _execute_with_role(
    connection: Any,
    query: str,
    params: Any,
    force_readonly: bool,
    role: str,
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

            rows = await cursor.fetchall()
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
                logger.error("Error rolling back transaction during role-wrapped execute: %s", rollback_error)
        raise
