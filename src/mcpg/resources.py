"""MCP resources — preload-on-connect surface (`mcpg://…`).

Closes the gap flagged in :mod:`mcpg.about`'s docstring: prior to this
module the only way an agent could learn what MCPg does was to call
``describe_self`` / ``list_tables`` / ``get_compact_schema`` as
*tools*, which costs tool-call budget on the wire and the per-call
context-window overhead. MCP's protocol-level **resources** primitive
(separate from tools and prompts) is the right home for that
"preload-on-connect" content: a client can read a resource by URI
once at session start, cache the payload locally, and skip the tool
call entirely.

Resource surface (four entries, all ``application/json``):

* ``mcpg://about/index`` — the full :func:`mcpg.about.build_capability_summary`
  payload. Same shape as the ``describe_self`` tool, no DB access.
* ``mcpg://capabilities/index`` — a *compact* list of buckets
  (``id`` / ``name`` / ``summary`` only, no per-bucket tool lists).
  Cheap enough that an agent can pull it on every session start
  without context bloat.
* ``mcpg://capabilities/{bucket_id}`` — full detail for one
  capability bucket (the ``capabilities`` entry from
  ``build_capability_summary``, filtered to one bucket). Lets an
  agent drill in after picking a bucket from ``index`` without
  re-pulling the full payload.
* ``mcpg://schema/{schema_name}`` — :func:`mcpg.introspection.get_compact_schema`
  output for one PostgreSQL schema, formatted as Markdown.
  Read-only DB access; the schema name is parameter-bound
  inside ``get_compact_schema`` so there's no injection surface.

Each resource is registered from :func:`register_resources` in
``mcpg.tools``, alongside the tool registrations, so the existing
capability-gate machinery applies: resources that touch the database
are gated on ``Capability.READ`` like the equivalent tools would be.

Why a separate module
---------------------
Resources are *not* tools — different MCP protocol primitive, different
client API (``read_resource`` vs ``call_tool``), different
discoverability surface (``list_resources`` vs ``list_tools``). Keeping
them in their own module makes the boundary explicit and matches the
shape we already use for ``mcpg.about`` / ``mcpg.introspection``
(modules holding the *content* logic; ``tools.py`` doing the
registration plumbing). Future MCP prompts (roadmap row 8.4) get
their own ``mcpg.prompts`` module under the same convention.

Why JSON bodies
---------------
Every resource emits a JSON document so a downstream client (LangChain
``MCPResourceLoader``, LangGraph state preload, a hand-rolled HTTP
adapter) can deserialize without sniffing. MCP allows arbitrary MIME
types per resource, but uniformity is more valuable than per-resource
optimisation at this scale.
"""

from __future__ import annotations

import json
from typing import Any

from mcpg.about import CAPABILITIES, build_capability_summary
from mcpg.introspection import get_compact_schema


class MCPgResourceError(Exception):
    """Raised when a resource lookup fails."""


def _build_about_index_payload(tool_names: list[str]) -> str:
    """Render the full describe_self-equivalent payload as a JSON string."""
    return json.dumps(build_capability_summary(tool_names), indent=2)


def _build_capabilities_index_payload() -> str:
    """Render the compact capability-bucket list as a JSON string.

    Carries only the agent-orientation fields (``id`` / ``name`` /
    ``summary``) so an agent can hold the whole catalogue in
    working memory without pulling per-bucket tool lists. Drill in
    with ``mcpg://capabilities/{bucket_id}`` for full detail.
    """
    compact = [{"id": cap.id, "name": cap.name, "summary": cap.summary} for cap in CAPABILITIES]
    return json.dumps({"capabilities": compact}, indent=2)


def _build_capability_detail_payload(bucket_id: str, tool_names: list[str]) -> str:
    """Render the per-bucket detail payload for one capability bucket.

    Reuses ``build_capability_summary`` to pick up the canonical
    per-bucket tool routing (so the resource and the tool's wire shape
    stay in lockstep), then filters down to the one bucket.
    """
    summary = build_capability_summary(tool_names)
    for cap in summary.get("capabilities", []):
        if cap.get("id") == bucket_id:
            return json.dumps(cap, indent=2)
    raise MCPgResourceError(
        f"unknown capability bucket {bucket_id!r}; expected one of {[cap.id for cap in CAPABILITIES]}"
    )


async def _build_schema_payload(driver: Any, schema_name: str) -> str:
    """Render the compact-schema payload for one PostgreSQL schema as JSON.

    Wraps ``get_compact_schema`` (which returns Markdown) into a JSON
    envelope ``{schema, format, body}`` so the resource's MIME type
    stays ``application/json`` uniformly. The Markdown body is in the
    ``body`` field; clients that want the raw rendering split it off.
    """
    body = await get_compact_schema(driver, schema_name)
    return json.dumps(
        {
            "schema": schema_name,
            "format": "markdown",
            "body": body,
        },
        indent=2,
    )


__all__ = [
    "MCPgResourceError",
    "_build_about_index_payload",
    "_build_capabilities_index_payload",
    "_build_capability_detail_payload",
    "_build_schema_payload",
]
