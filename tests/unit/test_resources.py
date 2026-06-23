"""Tests for the MCP resources content-builder module."""

from __future__ import annotations

import json

import pytest

from mcpg.resources import (
    MCPgResourceError,
    _build_about_index_payload,
    _build_capabilities_index_payload,
    _build_capability_detail_payload,
)


def test_about_index_payload_has_canonical_shape() -> None:
    payload = json.loads(_build_about_index_payload(["list_tables", "describe_self"]))
    assert "version" in payload
    assert "tool_count" in payload
    assert "capability_count" in payload
    assert isinstance(payload["capabilities"], list)
    assert payload["capabilities"], "must list at least one bucket"
    sample = payload["capabilities"][0]
    for key in ("id", "name", "summary", "detail", "headline_tools", "tool_count", "all_tools"):
        assert key in sample, f"{key} missing from per-bucket payload"


def test_about_index_payload_tool_count_matches_input() -> None:
    """The summary's `tool_count` reflects the tool list we hand in,
    not the static module catalogue."""
    payload = json.loads(_build_about_index_payload(["list_tables", "describe_self", "run_select"]))
    assert payload["tool_count"] == 3


def test_capabilities_index_is_compact() -> None:
    """Compact = id + name + summary only; no per-bucket tool lists."""
    payload = json.loads(_build_capabilities_index_payload())
    assert "capabilities" in payload
    for cap in payload["capabilities"]:
        assert set(cap.keys()) == {"id", "name", "summary"}, (
            f"capabilities index must stay compact; got extra keys: {set(cap.keys()) - {'id', 'name', 'summary'}}"
        )


def test_capability_detail_returns_one_bucket() -> None:
    payload = json.loads(_build_capability_detail_payload("schema_introspection", ["list_tables", "describe_table"]))
    assert payload["id"] == "schema_introspection"
    assert "headline_tools" in payload
    assert "all_tools" in payload


def test_capability_detail_raises_on_unknown_bucket() -> None:
    with pytest.raises(MCPgResourceError, match="unknown capability bucket"):
        _build_capability_detail_payload("does-not-exist", ["list_tables"])


def test_capability_detail_error_lists_valid_bucket_ids() -> None:
    """The error message should help the agent recover by enumerating the valid IDs."""
    with pytest.raises(MCPgResourceError) as exc:
        _build_capability_detail_payload("typo", ["list_tables"])
    msg = str(exc.value)
    # At least one known canonical bucket should appear in the error so the
    # agent can self-correct.
    assert "schema_introspection" in msg or "operations_and_health" in msg
