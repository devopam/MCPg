"""Unit tests for the `mcpg.tool_introspection` content builders.

The builders take primitive inputs (tool name, schemas, registered-name
list) and return plain dicts — exercise them directly without an MCP
runtime so we cover the "found" / "missing" / "missing-but-close-typo"
paths without spinning a maximal-flag server every time.
"""

from __future__ import annotations

from mcpg.tool_introspection import (
    build_missing_tool_descriptor,
    build_tool_descriptor,
)


def test_build_tool_descriptor_returns_registered_true_with_all_fields() -> None:
    input_schema = {"type": "object", "properties": {"sql": {"type": "string"}}}
    output_schema = {"type": "object", "properties": {"rows": {"type": "array"}}}
    descriptor = build_tool_descriptor(
        name="run_select",
        description="Run a SELECT statement.",
        input_schema=input_schema,
        output_schema=output_schema,
    )
    assert descriptor["name"] == "run_select"
    assert descriptor["registered"] is True
    assert descriptor["description"] == "Run a SELECT statement."
    assert descriptor["input_schema"] is input_schema
    assert descriptor["output_schema"] is output_schema


def test_build_tool_descriptor_resolves_classify_tool_to_bucket_metadata() -> None:
    """The descriptor includes the bucket id + name + summary so agents
    don't have to do a second `describe_self` lookup."""
    descriptor = build_tool_descriptor(
        name="run_select",
        description="x",
        input_schema={},
        output_schema=None,
    )
    bucket = descriptor["bucket"]
    assert bucket is not None
    assert bucket["id"] == "query_execution"
    assert "name" in bucket
    assert "summary" in bucket


def test_build_tool_descriptor_preserves_none_output_schema_for_unswept_tools() -> None:
    """Tools that haven't been swept onto the typed-return pattern yet
    (the 8.6 long tail) carry `outputSchema=None` — the descriptor
    must pass that through rather than substituting `{}`."""
    descriptor = build_tool_descriptor(
        name="run_select",
        description=None,
        input_schema={},
        output_schema=None,
    )
    assert descriptor["output_schema"] is None
    assert descriptor["description"] is None


def test_build_tool_descriptor_bucket_is_none_when_classify_returns_none() -> None:
    """A genuinely-unclassified tool comes back with `bucket=None` —
    callers (and clients) shouldn't see a synthesised stub bucket."""
    descriptor = build_tool_descriptor(
        name="not_a_real_tool_name_at_all_xyzzy",
        description="x",
        input_schema={},
        output_schema=None,
    )
    assert descriptor["bucket"] is None


def test_build_missing_tool_descriptor_returns_registered_false() -> None:
    descriptor = build_missing_tool_descriptor(
        "no_such_tool",
        registered_names=["run_select", "list_tables", "describe_table"],
    )
    assert descriptor["name"] == "no_such_tool"
    assert descriptor["registered"] is False
    assert "did_you_mean" in descriptor


def test_build_missing_tool_descriptor_surfaces_close_match_typo_suggestions() -> None:
    """A single-letter typo should land in `did_you_mean`."""
    descriptor = build_missing_tool_descriptor(
        "run_sleect",
        registered_names=["run_select", "run_insert", "list_tables"],
    )
    assert "run_select" in descriptor["did_you_mean"]


def test_build_missing_tool_descriptor_returns_empty_when_no_close_match_exists() -> None:
    """Distinguishes 'typo with obvious fix' from 'wrong server / wrong
    universe' — the empty list IS the diagnostic."""
    descriptor = build_missing_tool_descriptor(
        "absolutely_not_a_postgres_tool",
        registered_names=["run_select", "list_tables"],
    )
    assert descriptor["did_you_mean"] == []


def test_build_missing_tool_descriptor_caps_suggestions_at_three() -> None:
    """An agent that needs to pick between 4+ near-matches is no better
    off than one calling `list_tools` — keep the response actionable."""
    descriptor = build_missing_tool_descriptor(
        "run_sel",
        registered_names=[
            "run_select",
            "run_sela",
            "run_sele",
            "run_selb",
            "run_selc",
        ],
    )
    assert len(descriptor["did_you_mean"]) <= 3
