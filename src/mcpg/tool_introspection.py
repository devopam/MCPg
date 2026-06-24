"""Per-tool introspection — payload for the ``describe_tool`` MCP tool.

Companion to :mod:`mcpg.about` (whole-server capability summary):
that builds a high-level menu, this builds the deep-dive for one
tool. Designed for the agent self-recovery loop — an agent hits a
schema-validation error, asks ``describe_tool('run_select')`` to
re-read the input shape, then retries with the right arguments
without paying the full ``describe_self`` cost again.

Returns a plain ``dict[str, Any]`` so the MCP wire serialises it
cleanly without a custom outputSchema dataclass.

Surface inventory:

* :func:`build_tool_descriptor` — for a tool that exists on the server,
  returns its description, input schema, output schema, and bucket
  metadata.
* :func:`build_missing_tool_descriptor` — for an unknown name, returns
  ``registered=false`` plus a ``did_you_mean`` suggestion list scored
  via :mod:`difflib`.
"""

from __future__ import annotations

import difflib
from typing import Any

from mcpg.about import CAPABILITIES, classify_tool

# How many close-match suggestions to surface when a lookup misses.
# 3 is the standard difflib default; more than that and the agent has
# to triage the list itself, which defeats the point.
_MAX_DID_YOU_MEAN = 3

# Lookup-side `difflib.get_close_matches` cutoff. The default (0.6) is
# permissive enough to catch single-letter typos (`run_sleect` →
# `run_select`) without surfacing distantly-related tools.
_DID_YOU_MEAN_CUTOFF = 0.6


def _bucket_metadata(bucket_id: str | None) -> dict[str, str] | None:
    """Resolve a bucket id to its :class:`Capability` metadata.

    Returns ``None`` when the id doesn't match any registered
    capability — same shape an unclassified tool ends up with — so
    callers don't have to special-case both "no bucket" and "unknown
    bucket".
    """
    if bucket_id is None:
        return None
    for capability in CAPABILITIES:
        if capability.id == bucket_id:
            return {
                "id": capability.id,
                "name": capability.name,
                "summary": capability.summary,
            }
    return None


def build_tool_descriptor(
    name: str,
    *,
    description: str | None,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the JSON descriptor for one registered tool.

    Args:
        name: The tool's registered name.
        description: The agent-facing description (may be ``None`` if
            the registration didn't supply one — rare but possible).
        input_schema: The MCP ``inputSchema`` dict.
        output_schema: The MCP ``outputSchema`` dict, or ``None`` for
            tools that haven't been swept onto the typed-return
            pattern yet (the long-tail tracked under roadmap 8.6).
    """
    return {
        "name": name,
        "registered": True,
        "description": description,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "bucket": _bucket_metadata(classify_tool(name)),
    }


def build_missing_tool_descriptor(
    name: str,
    *,
    registered_names: list[str],
) -> dict[str, Any]:
    """Assemble the response for a name that isn't registered.

    Returns ``registered=false`` plus a ``did_you_mean`` list so the
    agent can self-correct without a separate ``list_tools`` call.
    ``did_you_mean`` is empty when no close match clears the cutoff —
    distinguishes "typo with an obvious fix" from "the agent picked
    a name out of training data that this server doesn't carry".
    """
    suggestions = difflib.get_close_matches(
        name,
        registered_names,
        n=_MAX_DID_YOU_MEAN,
        cutoff=_DID_YOU_MEAN_CUTOFF,
    )
    return {
        "name": name,
        "registered": False,
        "did_you_mean": suggestions,
    }


__all__ = [
    "build_missing_tool_descriptor",
    "build_tool_descriptor",
]
