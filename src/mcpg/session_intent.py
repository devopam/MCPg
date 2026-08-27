"""Session-intent handshake — narrow the tool surface to a stated goal.

Realises roadmap row **8.8**. Lets the operator declare the agent's
high-level intent at server-start time (via the ``MCPG_SESSION_INTENT``
env var); MCPg filters its tool surface to the capability buckets
relevant to that intent before any tool is advertised on the wire.

Big prompt-injection resilience win: a session declared ``intent=lookup``
literally cannot call ``drop_database`` because ``run_ddl`` was never
registered with MCPServer. The defence is structural, not policy — the
adversary can't talk the agent into a tool that isn't on the wire.

Why launch-time, not call-time
==============================

The MCP transport advertises the tool list on connect. A call-time
"is this tool allowed?" check would still expose every tool name in
``tools/list`` — useful for a soft policy gate, but it leaks the
attack surface. Removing the tools from the MCPServer registry before
the first ``tools/list`` request is the only way to make them
invisible.

Presets
=======

``MCPG_SESSION_INTENT`` accepts a comma-separated list. Each entry is
either a **preset name** (resolved via :data:`INTENT_PRESETS` below —
``lookup`` / ``migration`` / ``vector_rag`` / ``monitor`` / ``admin``,
plus the finer, headline-tools-based ``core`` preset in
:data:`_TOOL_NAME_PRESETS`) or a **raw bucket id** from
:mod:`mcpg.about`. Bucket ids let callers opt into combinations the
presets don't cover; presets give the common shapes one-word names so
the env var stays readable.

The escape hatch — :data:`ALWAYS_KEEP` (``describe_self``,
``describe_tool``, and, when ``MCPG_DYNAMIC_SESSION_INTENT`` is also
enabled, the two dynamic-session-intent meta-tools ``list_session_intents``
/ ``enable_session_intent``) — is **always** kept regardless of intent.
Without them the filtered agent has no way to discover what *is* on
the wire, or to grow its own view of it at runtime.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, NamedTuple

from mcpg.about import CAPABILITIES, classify_tool

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


# Tools that are NEVER removed by the intent filter — without them an
# agent connecting to a narrowed surface has no way to learn what's on
# the wire, or (for the two dynamic-session-intent meta-tools, roadmap
# 22) to discover/grow its own surface at runtime. All four are
# read-only, no DB access. Public (not `_`-prefixed): the dynamic
# layer in `mcpg.dynamic_session_intent` reuses this directly rather
# than defining its own always-visible set.
ALWAYS_KEEP: frozenset[str] = frozenset(
    {
        "describe_self",
        "describe_tool",
        "list_session_intents",
        "enable_session_intent",
    }
)


# ---------------------------------------------------------------------------
# Presets — readable shorthand for common bucket combinations.
# ---------------------------------------------------------------------------
#
# Adding a preset: keep the bucket list tight. The point of intent is to
# REMOVE surface; a preset that names half the buckets isn't worth its
# weight — the operator should just use ``admin`` (no filter) instead.

INTENT_PRESETS: dict[str, frozenset[str]] = {
    # "Look up a row." Read-only navigation of the catalogue + safe
    # SELECTs. No writes, no DDL, no shell, no migration tooling.
    "lookup": frozenset(
        {
            "schema_introspection",
            "query_execution",
            "observability",
        }
    ),
    # "Run a migration." Schema work + the validation / comparison
    # surface + the audit trail (so the change is logged).
    "migration": frozenset(
        {
            "schema_introspection",
            "query_execution",
            "migrations",
            "advisors",
            "operations_and_health",
            "audit_trail",
            "observability",
        }
    ),
    # "Vector / RAG retrieval work." Catalogue + query + vector / text
    # search + RAG telemetry.
    "vector_rag": frozenset(
        {
            "schema_introspection",
            "query_execution",
            "vector_search",
            "text_search",
            "rag_telemetry",
            "observability",
        }
    ),
    # "Operational dashboard." Health, advisors, live ops — no writes.
    "monitor": frozenset(
        {
            "operations_and_health",
            "advisors",
            "observability",
            "audit_trail",
        }
    ),
    # "Admin." Full access — no filter applied. Useful as the explicit
    # opt-out value so operators can document intent=admin in their
    # service manifests instead of leaving the env var unset.
    "admin": frozenset(),  # empty set is the sentinel for "no filter"
}


# Headline-tools-based preset — deliberately NOT a bucket set. About 4x
# tighter than the coarsest bucket preset (`lookup`, 3 buckets ~56
# tools): built from `about.py`'s own curated `headline_tools`, so
# there's one source of truth for "what matters most" shared across
# this preset, the dynamic session-intent feature (roadmap 22), and
# `describe_self`'s headline display. Kept in a separate dict from
# `INTENT_PRESETS` rather than widening that dict's value type —
# additive, not a breaking change to the existing five entries or
# their `dict[str, frozenset[str]]` public shape.
_TOOL_NAME_PRESETS: dict[str, frozenset[str]] = {
    "core": frozenset(
        name
        for cap in CAPABILITIES
        if cap.id in ("schema_introspection", "query_execution")
        for name in cap.headline_tools
    ),
}


class IntentResolution(NamedTuple):
    """The two independently-checked halves of a resolved intent.

    Kept separate rather than merged into one set: bucket ids and tool
    names are different namespaces (a bucket preset like ``lookup``
    names buckets; ``core`` names literal tools), and a merged
    predicate silently drops every tool-name preset's tools — a
    bucket-only keep-check never matches a literal tool name against a
    bucket id. Caught during design, not left as a latent bug.
    """

    buckets: frozenset[str]
    tool_names: frozenset[str]


def resolve_intent_to_buckets(intent_values: tuple[str, ...]) -> frozenset[str] | None:
    """Expand the configured intent values into the allowed bucket set.

    Returns:
        ``None`` when no filter should be applied — either because
        ``intent_values`` is empty, or one of the entries is the
        ``admin`` preset (whose preset set is empty as the sentinel).
        Otherwise the union of every named preset's buckets, plus any
        raw bucket id passed verbatim. Unknown names are silently
        ignored — :func:`filter_server_tools` validates the final set
        against the live tool surface so a typo just narrows the
        result further (no surprise expansion).
    """
    if not intent_values:
        return None
    allowed: set[str] = set()
    for raw in intent_values:
        name = raw.strip().lower()
        if not name:
            continue
        if name in INTENT_PRESETS:
            preset_buckets = INTENT_PRESETS[name]
            if not preset_buckets:
                # ``admin`` (or any future "no filter" preset) short-
                # circuits the whole filter; mixing it with other
                # entries doesn't make sense but we honour it.
                return None
            allowed |= preset_buckets
        else:
            # Treat unrecognised entries as raw bucket ids. We don't
            # validate against BUCKET_IDS here because the bucket list
            # could legitimately grow before this module is updated;
            # the filter step only KEEPS tools whose bucket is in the
            # set, so a bogus entry is harmless (the tools whose bucket
            # is that bogus name don't exist, so nothing extra is kept).
            allowed.add(name)
    return frozenset(allowed)


def resolve_intent(intent_values: tuple[str, ...]) -> IntentResolution | None:
    """Like :func:`resolve_intent_to_buckets`, but also resolves
    tool-name presets (currently just ``core``). Supersedes
    ``resolve_intent_to_buckets`` for callers that need tool-name
    presets; that function is unchanged and still bucket-only.

    Returns ``None`` under the same conditions as
    ``resolve_intent_to_buckets``: empty input, or the ``admin``
    sentinel present anywhere in the input.
    """
    if not intent_values:
        return None
    buckets: set[str] = set()
    tool_names: set[str] = set()
    for raw in intent_values:
        name = raw.strip().lower()
        if not name:
            continue
        if name in INTENT_PRESETS:
            preset_buckets = INTENT_PRESETS[name]
            if not preset_buckets:
                return None  # admin sentinel
            buckets |= preset_buckets
        elif name in _TOOL_NAME_PRESETS:
            tool_names |= _TOOL_NAME_PRESETS[name]
        else:
            buckets.add(name)
    return IntentResolution(buckets=frozenset(buckets), tool_names=frozenset(tool_names))


def resolved_tool_names(
    resolution: IntentResolution,
    candidate_names: Iterable[str],
    *,
    always_keep: frozenset[str] = ALWAYS_KEEP,
) -> frozenset[str]:
    """The subset of ``candidate_names`` that survive ``resolution``.

    The one keep-decision both :func:`filter_server_tools` (registry
    mutation, launch-time) and ``mcpg.dynamic_session_intent``'s
    response filter (per-session, runtime) apply — kept in exactly one
    place so the two layers can never silently diverge.
    """
    kept: set[str] = set()
    for name in candidate_names:
        if name in always_keep or name in resolution.tool_names or classify_tool(name) in resolution.buckets:
            kept.add(name)
    return frozenset(kept)


def filter_server_tools(
    server: MCPServer,
    allowed_buckets: frozenset[str],
    *,
    allowed_tool_names: frozenset[str] = frozenset(),
    always_keep: frozenset[str] = ALWAYS_KEEP,
) -> list[str]:
    """Remove every registered tool that doesn't survive the resolved intent.

    ``allowed_tool_names`` is new (additive) — existing callers that
    only pass ``allowed_buckets`` positionally are unaffected, since it
    defaults to empty and every existing preset is bucket-only.

    Returns the list of removed tool names, sorted. Idempotent.
    """
    resolution = IntentResolution(buckets=allowed_buckets, tool_names=allowed_tool_names)
    all_names = [tool.name for tool in server._tool_manager.list_tools()]
    keep = resolved_tool_names(resolution, all_names, always_keep=always_keep)
    removed: list[str] = []
    for name in all_names:
        if name not in keep:
            server.remove_tool(name)
            removed.append(name)
    return sorted(removed)


def parse_intent_setting(raw: str | None) -> tuple[str, ...]:
    """Parse the ``MCPG_SESSION_INTENT`` env value into a tuple.

    Splits on ``,``, strips whitespace, drops empty entries, lowercases
    every entry. ``None`` / empty string → empty tuple.
    """
    if not raw:
        return ()
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


__all__ = [
    "ALWAYS_KEEP",
    "INTENT_PRESETS",
    "IntentResolution",
    "filter_server_tools",
    "parse_intent_setting",
    "resolve_intent",
    "resolve_intent_to_buckets",
    "resolved_tool_names",
]
