"""Unit tests for the Tier-B pure core (roadmap 19.5).

Covers the deterministic parts — the task graders and the trial aggregation.
The model loop, the Anthropic client, and the runner are costed / need a live
model + DB, so they are not unit-tested (and never run in CI).
"""

from __future__ import annotations

import pytest

from benchmarks.tokens.tier_b.agent import _hidden_tokens
from benchmarks.tokens.tier_b.schema import ARM_BASELINE, ARM_MCPG, TrialResult, aggregate
from benchmarks.tokens.tier_b.tasks import default_tasks


def _task(task_id: str):
    return next(t for t in default_tasks() if t.id == task_id)


def test_missing_index_grader() -> None:
    g = _task("missing_index").grade
    assert g("You should add an index on orders.customer_id.")
    assert g('The `orders` table needs an index on the "customer_id" column.')
    assert not g("Add an index on order_items.product_id.")  # wrong table/column
    assert not g("The customers table is fine.")


def test_pii_grader_requires_both_columns() -> None:
    g = _task("pii_columns").grade
    assert g("PII columns: customers.email and customers.phone")
    assert not g("Only customers.email holds PII.")  # missing phone
    assert not g("No PII found.")


def test_naming_grader_matches_camelcase_column() -> None:
    g = _task("naming_violation").grade
    assert g('The offending column is reviews."reviewSource".')
    assert g("reviewSource breaks snake_case.")
    assert not g("Everything follows snake_case.")


def _trial(
    arm: str, task_id: str, tin: int, tout: int, tools: int, turns: int, *, passed: bool, error=None
) -> TrialResult:
    return TrialResult(
        task_id=task_id,
        arm=arm,
        trial=0,
        tokens_in=tin,
        tokens_out=tout,
        turns=turns,
        tool_calls=tools,
        passed=passed,
        final_answer="",
        error=error,
    )


def test_aggregate_token_ratio_and_correctness() -> None:
    trials = [
        # baseline: 10k tokens, 1/2 correct
        _trial(ARM_BASELINE, "a", 8000, 2000, 5, 5, passed=True),
        _trial(ARM_BASELINE, "b", 8000, 2000, 5, 5, passed=False),
        # mcpg: 2.5k tokens, 2/2 correct
        _trial(ARM_MCPG, "a", 2000, 500, 1, 2, passed=True),
        _trial(ARM_MCPG, "b", 2000, 500, 1, 2, passed=True),
    ]
    agg = aggregate(trials)
    assert agg["baseline"]["mean_total_tokens"] == pytest.approx(10000)
    assert agg["mcpg"]["mean_total_tokens"] == pytest.approx(2500)
    assert agg["token_ratio"] == pytest.approx(4.0)  # baseline spends 4x
    assert agg["baseline"]["correctness"] == pytest.approx(0.5)
    assert agg["mcpg"]["correctness"] == pytest.approx(1.0)
    assert agg["errored"] == 0
    assert {e["task_id"] for e in agg["per_task"]} == {"a", "b"}


def test_aggregate_excludes_errored_trials_and_counts_them() -> None:
    trials = [
        _trial(ARM_MCPG, "a", 2000, 500, 1, 2, passed=True),
        _trial(ARM_MCPG, "a", 999999, 999999, 9, 9, passed=False, error="boom"),  # must not pollute means
    ]
    agg = aggregate(trials)
    assert agg["mcpg"]["trials"] == 1
    assert agg["mcpg"]["mean_total_tokens"] == pytest.approx(2500)
    assert agg["errored"] == 1


def test_aggregate_empty_is_safe() -> None:
    agg = aggregate([])
    assert agg["token_ratio"] == 0.0
    assert agg["mcpg"]["trials"] == 0


def test_total_tokens_includes_hidden_tokens() -> None:
    """translate_nl_to_sql's internal LLM call is invisible to the outer
    agent loop's own tokens_in/tokens_out — total_tokens must still count it,
    or the study would silently underweight MCPg's true cost."""
    trial = TrialResult(
        task_id="a",
        arm=ARM_MCPG,
        trial=0,
        tokens_in=1000,
        tokens_out=200,
        turns=2,
        tool_calls=1,
        passed=True,
        final_answer="",
        hidden_tokens_in=300,
        hidden_tokens_out=50,
    )
    assert trial.total_tokens == 1550


def test_total_tokens_defaults_hidden_to_zero_when_unset() -> None:
    """A trial that never calls translate_nl_to_sql (e.g. every baseline
    trial, which doesn't have the tool at all) must not be penalized or
    inflated — hidden tokens default to 0."""
    trial = TrialResult(
        task_id="a",
        arm=ARM_BASELINE,
        trial=0,
        tokens_in=1000,
        tokens_out=200,
        turns=2,
        tool_calls=1,
        passed=True,
        final_answer="",
    )
    assert trial.hidden_tokens_in == 0
    assert trial.hidden_tokens_out == 0
    assert trial.total_tokens == 1200


class _FakeToolResult:
    def __init__(self, structured: dict[str, object] | None) -> None:
        self.structuredContent = structured


def test_hidden_tokens_reads_translate_nl_to_sql_structured_content() -> None:
    result = _FakeToolResult({"tokens_in": 42, "tokens_out": 17, "sql": "SELECT 1"})
    assert _hidden_tokens("translate_nl_to_sql", result) == (42, 17)


def test_hidden_tokens_ignores_other_tools() -> None:
    """A tool that isn't known to make its own internal LLM call must never
    contribute hidden tokens, even if its result happens to have similarly
    named fields — only the documented allowlist counts."""
    result = _FakeToolResult({"tokens_in": 999, "tokens_out": 999})
    assert _hidden_tokens("run_select", result) == (0, 0)


def test_hidden_tokens_defaults_to_zero_on_missing_structured_content() -> None:
    assert _hidden_tokens("translate_nl_to_sql", _FakeToolResult(None)) == (0, 0)
