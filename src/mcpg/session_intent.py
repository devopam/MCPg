"""Session-intent handshake — narrow the tool surface to a stated goal.

Realises roadmap row **8.8**. Lets the operator declare the agent's
high-level intent at server-start time (via the ``MCPG_SESSION_INTENT``
env var); MCPg filters its tool surface to the capability buckets
relevant to that intent before any tool is advertised on the wire.

Big prompt-injection resilience win: a session declared ``intent=lookup``
literally cannot call ``drop_database`` because ``run_ddl`` was never
registered with FastMCP. The defence is structural, not policy — the
adversary can't talk the agent into a tool that isn't on the wire.

Why launch-time, not call-time
==============================

The MCP transport advertises the tool list on connect. A call-time
"is this tool allowed?" check would still expose every tool name in
``tools/list`` — useful for a soft policy gate, but it leaks the
attack surface. Removing the tools from the FastMCP registry before
the first ``tools/list`` request is the only way to make them
invisible.

Presets
=======

``MCPG_SESSION_INTENT`` accepts a comma-separated list. Each entry is
either a **preset name** (resolved via :data:`INTENT_PRESETS` below)
or a **raw bucket id** from :mod:`mcpg.about`. Bucket ids let callers
opt into combinations the presets don't cover; presets give the
common shapes one-word names so the env var stays readable.

The escape hatch — ``describe_self`` and ``describe_tool`` — is
**always** kept regardless of intent. Without them the filtered agent
has no way to discover what *is* on the wire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcpg.about import classify_tool

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


# Tools that are NEVER removed by the intent filter — without them an
# agent connecting to a narrowed surface has no way to learn what's on
# the wire. Both are read-only, no DB access.
_ALWAYS_KEEP: frozenset[str] = frozenset({"describe_self", "describe_tool"})


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


def filter_server_tools(
    server: FastMCP,
    allowed_buckets: frozenset[str],
    *,
    always_keep: frozenset[str] = _ALWAYS_KEEP,
) -> list[str]:
    """Remove every registered tool whose bucket isn't in ``allowed_buckets``.

    Tools in ``always_keep`` survive regardless — without them the
    filtered agent can't introspect the surviving surface.

    Returns the list of removed tool names, sorted, so the caller can
    log / surface the diff. Idempotent — running twice with the same
    arguments removes nothing the second time.
    """
    # FastMCP's ``list_tools`` is async; we use the internal
    # ``_tool_manager.list_tools()`` instead because the intent filter
    # runs in synchronous startup code. ``server.remove_tool(name)``
    # is the public removal API.
    removed: list[str] = []
    for tool in list(server._tool_manager.list_tools()):
        name = tool.name
        if name in always_keep:
            continue
        bucket = classify_tool(name)
        if bucket in allowed_buckets:
            continue
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
    "INTENT_PRESETS",
    "filter_server_tools",
    "parse_intent_setting",
    "resolve_intent_to_buckets",
]
