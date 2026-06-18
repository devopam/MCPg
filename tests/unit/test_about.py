"""Tests for ``mcpg.about`` — the self-description capability map.

The contract this module enforces:

1. Every tool registered on the maximal-flag server falls into exactly
   one capability bucket (no unclassified tools, no double-counting).
   When a new tool is added to ``register_tools``, this test fails
   until ``about.py``'s override / pattern list catches it. That's
   intentional — it forces a deliberate decision per tool.

2. Every capability bucket has at least one tool. An empty bucket
   would mean either a tool was removed or a bucket is dead code.

3. The ``build_capability_summary`` response shape matches the
   contract the ``describe_self`` MCP tool advertises in its
   description (so the LLM agent can rely on the keys).
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from mcpg.about import (
    BUCKET_IDS,
    CAPABILITIES,
    build_capability_summary,
    classify_tool,
)
from mcpg.config import load_settings
from mcpg.tools import register_tools

_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"


async def _registered_tool_names() -> list[str]:
    """Same maximal-server pattern the contract test uses, so the
    classification check is run against the *full* surface."""
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
        }
    )
    server: FastMCP = FastMCP("mcpg-test")
    register_tools(server, settings)
    tools = await server.list_tools()
    return [t.name for t in tools]


def test_capability_ids_are_unique() -> None:
    """No two capabilities share an ``id``; ``BUCKET_IDS`` matches the
    tuple."""
    ids = [c.id for c in CAPABILITIES]
    assert len(ids) == len(set(ids)), f"duplicate bucket ids: {ids}"
    assert BUCKET_IDS == frozenset(ids)


def test_capability_summaries_are_short_enough_for_an_llm() -> None:
    """Summaries should fit on one LLM-token-budget line (≤180 chars).

    Longer summaries blow the per-bucket budget when an agent is
    deciding which bucket to expand on. ``detail`` is unbounded.
    """
    for cap in CAPABILITIES:
        assert len(cap.summary) <= 180, (
            f"capability {cap.id!r} summary is {len(cap.summary)} chars; "
            "trim to <=180 so an LLM can scan all buckets in one breath."
        )


def test_headline_tools_are_not_empty() -> None:
    """Every bucket promises 3-6 headline tools; an empty list means the
    bucket is dead."""
    for cap in CAPABILITIES:
        assert cap.headline_tools, f"capability {cap.id!r} has no headline_tools"
        assert len(cap.headline_tools) <= 6, (
            f"capability {cap.id!r} has {len(cap.headline_tools)} headline "
            "tools; trim to <=6 so the agent doesn't drown."
        )


async def test_every_registered_tool_classifies_into_a_bucket() -> None:
    """The contract: no tool on the maximal-flag server is unclassified.

    If this fails after adding a new tool, add an entry to
    ``_TOOL_TO_BUCKET_OVERRIDES`` or extend
    ``_TOOL_TO_BUCKET_PATTERNS`` in ``mcpg.about``.
    """
    names = await _registered_tool_names()
    unclassified = [name for name in names if classify_tool(name) is None]
    assert not unclassified, (
        f"{len(unclassified)} tool(s) are not assigned to a capability "
        f"bucket: {unclassified}. Add overrides / patterns to mcpg.about."
    )


async def test_every_bucket_has_at_least_one_tool() -> None:
    """An empty bucket signals either a removed tool or a dead bucket."""
    names = await _registered_tool_names()
    summary = build_capability_summary(names)
    empty = [cap["id"] for cap in summary["capabilities"] if cap["tool_count"] == 0]
    assert not empty, (
        f"capability bucket(s) {empty} have zero tools — either a tool was "
        "removed without re-bucketing, or the bucket is dead code."
    )


async def test_summary_shape_matches_documented_contract() -> None:
    """The ``describe_self`` tool's description promises these keys; the
    LLM agent relies on the shape staying stable."""
    names = await _registered_tool_names()
    summary = build_capability_summary(names)

    # Top-level keys.
    expected_top = {
        "headline",
        "version",
        "tool_count",
        "capability_count",
        "capabilities",
        "unclassified_tools",
        "next_step_hint",
    }
    assert set(summary.keys()) == expected_top
    assert summary["tool_count"] == len(names)
    assert summary["capability_count"] == len(CAPABILITIES)
    assert summary["unclassified_tools"] == []

    # Per-capability keys.
    expected_cap = {
        "id",
        "name",
        "summary",
        "detail",
        "headline_tools",
        "tool_count",
        "all_tools",
    }
    for cap in summary["capabilities"]:
        assert set(cap.keys()) == expected_cap, f"capability {cap['id']!r} has unexpected keys: {set(cap.keys())}"
        # all_tools must be sorted and a list (deterministic for snapshots).
        assert cap["all_tools"] == sorted(cap["all_tools"])
        # headline_tools must be a subset of all_tools (or filtered out
        # when the registered server doesn't expose them).
        for headline in cap["headline_tools"]:
            assert headline in cap["all_tools"], (
                f"headline tool {headline!r} in bucket {cap['id']!r} is not "
                "in that bucket's all_tools list — bucket mapping mismatch."
            )


async def test_describe_self_tool_is_registered_and_classified() -> None:
    """The new tool itself appears in the catalogue and lands in
    ``observability``."""
    names = await _registered_tool_names()
    assert "describe_self" in names
    assert classify_tool("describe_self") == "observability"


def test_headline_tools_belong_to_their_own_bucket() -> None:
    """A tool can only appear in one bucket's ``headline_tools`` list."""
    seen: dict[str, str] = {}
    for cap in CAPABILITIES:
        for tool in cap.headline_tools:
            if tool in seen and seen[tool] != cap.id:
                pytest.fail(f"headline tool {tool!r} appears in both {seen[tool]!r} and {cap.id!r}")
            seen[tool] = cap.id


def test_capability_summary_handles_empty_tool_list() -> None:
    """Defensive: an empty server (no tools registered) still produces
    a well-formed summary — every bucket reports tool_count=0 but the
    shape stays valid."""
    summary = build_capability_summary([])
    assert summary["tool_count"] == 0
    assert summary["unclassified_tools"] == []
    for cap in summary["capabilities"]:
        assert cap["tool_count"] == 0
        assert cap["all_tools"] == []
        assert cap["headline_tools"] == []
