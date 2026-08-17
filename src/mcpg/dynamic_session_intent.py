"""Dynamic session-intent — grow a session's visible tool surface at runtime.

Realises roadmap row 22, layered on top of ``mcpg.session_intent``
(roadmap 8.8) rather than duplicating its preset vocabulary. Where
``session_intent`` narrows the tool surface once, at launch, for every
session (via ``MCPG_SESSION_INTENT``, by physically removing tools
from the SDK's registry), this module lets one *individual* session
grow its own view of the surface at runtime, without a restart and
without affecting any other concurrent session.

Response filtering, not registry mutation
==========================================

``MCPServer``'s tool registry is process-wide, not per-session — two
concurrent ``streamable-http`` sessions share one ``MCPServer``
instance. Mutating the registry per session is therefore not an
option (session A's growth would leak into session B's view). Instead
``DynamicSessionIntentMiddleware`` (below) lets every tool register
normally (or survive whatever ``session_intent``'s static filter
left) and narrows only the *response* to each session's own
``tools/list`` call, keyed by the transport's own ``Mcp-Session-Id``
header.

Not an authorization boundary
==============================

This is visibility only. A client that already knows a filtered-out
tool's name and schema can still call it directly — ``tools/call`` is
never filtered by anything in this module. The real authorization
boundary is ``MCPG_ACCESS_MODE`` / capability gating in
``mcpg.policy``, untouched by this feature. Contrast with
``session_intent``'s static filter, which *does* achieve true
invisibility (registry removal) — that's why it's launch-time only,
per its own module docstring. This module makes no such claim.

Opt-in
======

Enabled only via ``MCPG_DYNAMIC_SESSION_INTENT``. Off by default:
zero behavior change for existing deployments, with or without
``MCPG_SESSION_INTENT`` also configured.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from mcp.server.context import CallNext, ServerRequestContext
from mcp_types import ListToolsResult

from mcpg.about import BUCKET_IDS
from mcpg.session_intent import _TOOL_NAME_PRESETS, INTENT_PRESETS, resolve_intent, resolved_tool_names


class DynamicIntentError(ValueError):
    """Raised when :func:`enable_intent` is given an unrecognized name."""


# stdio is inherently single-session-per-process (no Mcp-Session-Id
# header exists there) — every stdio call shares this one sentinel key,
# consistent with how mcpg.tenancy.current_role also no-ops distinctly
# on stdio.
STDIO_SESSION_KEY = "__stdio__"

# Per-session enabled-intent-name state. A session_key with no entry
# yet behaves identically to one with an empty set (see
# enabled_intents) — there's no separate "initialize" step to get
# wrong. Guarded by _lock since enable_intent can race across
# concurrent requests on the same session.
_session_intents: dict[str, set[str]] = {}
_lock = asyncio.Lock()


def session_key_from_headers(headers: Mapping[str, str] | None) -> str:
    """Resolve the per-session state key from a request's headers.

    ``headers`` is ``None`` on stdio (no HTTP request at all) or when
    a transport's request object carries no headers. A present-but-
    empty header set (the header wasn't sent) also falls back to the
    stdio sentinel rather than raising — an MCP client is expected to
    always send ``Mcp-Session-Id`` after the initial handshake, but a
    missing header degrading to "shared state" rather than crashing is
    the safer failure mode.
    """
    if not headers:
        return STDIO_SESSION_KEY
    session_id = headers.get("mcp-session-id")
    return session_id if session_id else STDIO_SESSION_KEY


def enabled_intents(session_key: str, *, default_intent: tuple[str, ...]) -> frozenset[str]:
    """The intent names currently enabled for ``session_key``.

    A session that hasn't called ``enable_intent`` yet resolves to
    ``default_intent`` — whatever ``MCPG_SESSION_INTENT`` was
    configured with, or ``("core",)`` when that's unset (the caller
    decides which; see ``DynamicSessionIntentMiddleware``).
    """
    enabled = _session_intents.get(session_key)
    return frozenset(enabled) if enabled else frozenset(default_intent)


def visible_tool_names(
    session_key: str,
    *,
    default_intent: tuple[str, ...],
    registered: frozenset[str],
) -> frozenset[str]:
    """The tools ``session_key`` should see, intersected with ``registered``.

    ``registered`` is whatever the SDK's ``MCPServer`` actually still
    has — i.e. whatever ``session_intent``'s static filter left, or
    everything if that wasn't configured. This intersection is what
    makes the static-filter/dynamic-layer ceiling relationship real in
    code: enabling an intent whose tools were never registered reveals
    nothing, rather than erroring or silently exceeding the ceiling.
    """
    names = enabled_intents(session_key, default_intent=default_intent)
    resolution = resolve_intent(tuple(names))
    if resolution is None:
        # "admin" was enabled (or the resolved default was), which is
        # the explicit no-filter sentinel — reveal everything Layer 1
        # left registered.
        return registered
    return resolved_tool_names(resolution, registered)


async def enable_intent(session_key: str, name: str) -> None:
    """Add ``name`` (a preset name or a raw bucket id) to ``session_key``'s
    enabled set. Idempotent. Raises :class:`DynamicIntentError` on an
    unrecognized name.
    """
    normalized = name.strip().lower()
    if not normalized:
        raise DynamicIntentError("intent name must not be blank")
    known = normalized in INTENT_PRESETS or normalized in _TOOL_NAME_PRESETS or normalized in BUCKET_IDS
    if not known:
        raise DynamicIntentError(
            f"{name!r} is not a known session-intent preset or capability-bucket id. "
            "Call list_session_intents() to see the available names."
        )
    async with _lock:
        _session_intents.setdefault(session_key, set()).add(normalized)


async def enable_intent_and_notify(
    session_key: str,
    name: str,
    *,
    notify: Callable[[], Awaitable[None]],
) -> None:
    """``enable_intent``, then invoke ``notify`` (the caller's
    ``tools/list_changed`` notification). Split out from the
    ``@server.tool``-decorated closure that calls this (see
    ``mcpg.tools._register_dynamic_session_intent``) so the
    notify-on-success / no-notify-on-error behavior is unit-testable
    with a fake ``notify`` callback, without needing a live MCP
    session — mirrors this codebase's existing ``build_server_info()``
    / ``get_server_info`` split.
    """
    await enable_intent(session_key, name)
    await notify()


class DynamicSessionIntentMiddleware:
    """Filters ``tools/list`` responses to each session's visible surface.

    Registered only when ``MCPG_DYNAMIC_SESSION_INTENT`` is enabled
    (see ``mcpg.server``). All other request kinds pass through
    untouched — this only ever narrows what a ``tools/list`` call
    returns, never what a ``tools/call`` can invoke (see the module
    docstring's "Not an authorization boundary" section).
    """

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> Any:
        result = await call_next(ctx)
        if ctx.method != "tools/list" or not isinstance(result, ListToolsResult):
            return result

        settings = ctx.lifespan_context.settings
        default_intent = settings.session_intent or ("core",)
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        session_key = session_key_from_headers(headers)

        registered = frozenset(tool.name for tool in result.tools)
        visible = visible_tool_names(session_key, default_intent=default_intent, registered=registered)
        return result.model_copy(update={"tools": [tool for tool in result.tools if tool.name in visible]})


# Static structural-conformance check: DynamicSessionIntentMiddleware must
# match the ServerMiddleware Protocol's call signature. Never evaluated at
# runtime -- mypy checks the assignment, nothing constructs this. Mirrors the
# exact pattern mcpg.tenancy uses for TenantRoleContextMiddleware.
if TYPE_CHECKING:
    from mcp.server.context import ServerMiddleware

    _dynamic_intent_middleware_matches_protocol: ServerMiddleware[Any] = DynamicSessionIntentMiddleware()


__all__ = [
    "STDIO_SESSION_KEY",
    "DynamicIntentError",
    "DynamicSessionIntentMiddleware",
    "enable_intent",
    "enable_intent_and_notify",
    "enabled_intents",
    "session_key_from_headers",
    "visible_tool_names",
]
