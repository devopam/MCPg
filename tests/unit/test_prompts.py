"""Unit tests for the `mcpg.prompts` content builders.

The builders return plain strings (so they're trivially testable
without an MCP runtime) — these tests assert on the rendered body
to catch drift in the tool names a prompt references. If the
investigation steps drift away from the canonical tool names,
agents following the prompt will start calling tools that no longer
exist.
"""

from __future__ import annotations

import pytest

from mcpg.prompts import (
    _build_bisect_slow_migration,
    _build_diagnose_slow_query,
    _build_review_rls_policy,
)


def test_diagnose_slow_query_embeds_the_caller_supplied_sql_verbatim() -> None:
    body = _build_diagnose_slow_query("SELECT 1 FROM widgets WHERE id = 42")
    assert "SELECT 1 FROM widgets WHERE id = 42" in body


def test_diagnose_slow_query_references_the_canonical_diagnostic_tool_chain() -> None:
    """Drift here would send agents to tool names that don't exist."""
    body = _build_diagnose_slow_query("SELECT 1")
    for tool_name in ("explain_query", "analyze_query_plan", "recommend_indexes", "analyze_workload"):
        assert tool_name in body, f"`diagnose_slow_query` should mention `{tool_name}`"


def test_bisect_slow_migration_embeds_all_three_caller_supplied_identifiers() -> None:
    body = _build_bisect_slow_migration(
        migration_id="20260623_add_users_index",
        baseline_schema="public_v42",
        current_schema="public",
    )
    assert "20260623_add_users_index" in body
    assert "public_v42" in body
    assert "public" in body


def test_bisect_slow_migration_references_the_canonical_migration_tool_chain() -> None:
    body = _build_bisect_slow_migration("m1", "baseline", "current")
    for tool_name in (
        "list_applied_migrations",
        "list_unapplied_migration_scripts",
        "compare_schemas",
        "analyze_workload",
        "analyze_query_plan",
        "recommend_indexes",
    ):
        assert tool_name in body, f"`bisect_slow_migration` should mention `{tool_name}`"


def test_review_rls_policy_embeds_caller_supplied_schema_and_table() -> None:
    body = _build_review_rls_policy(schema="tenants", table="invoices")
    assert "tenants.invoices" in body or ("tenants" in body and "invoices" in body)


def test_review_rls_policy_references_the_canonical_security_tool_chain() -> None:
    body = _build_review_rls_policy("public", "widgets")
    for tool_name in ("describe_table", "list_policies", "audit_database"):
        assert tool_name in body, f"`review_rls_policy` should mention `{tool_name}`"


def test_review_rls_policy_stops_short_of_applying_writes() -> None:
    """RLS changes are too consequential to auto-apply — prompt must say so."""
    body = _build_review_rls_policy("public", "widgets")
    # Either explicit "do not apply" wording or "human approval" gating
    # is acceptable; both communicate the same constraint.
    assert any(
        marker in body.lower() for marker in ("stop short of applying", "human approval", "not apply", "do not apply")
    )


@pytest.mark.parametrize(
    "builder, args",
    [
        (_build_diagnose_slow_query, ("SELECT 1",)),
        (_build_bisect_slow_migration, ("m", "a", "b")),
        (_build_review_rls_policy, ("public", "t")),
    ],
)
def test_every_prompt_body_is_a_non_empty_string(builder, args) -> None:
    body = builder(*args)
    assert isinstance(body, str)
    assert len(body) > 200, "prompt bodies should be substantial — these are investigation playbooks"
