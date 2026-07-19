"""Unit tests for the Tier-A token-accounting pure core (roadmap 19.4).

Covers the deterministic helpers — savings/ratio derivation and the break-even
math. The DB-touching comparisons + runner are operator tools (live PostgreSQL),
not unit-tested. ``count_tokens`` needs the optional ``tiktoken`` (bench group),
so its test is skipped when that isn't installed — CI runs the dev group only.
"""

from __future__ import annotations

import pytest

from benchmarks.tokens.tier_a.schema import TokenReport, break_even, derive


def test_derive_savings_and_ratio() -> None:
    c = derive("x", "schema", mcpg_tokens=574, raw_tokens=2375)
    assert c.savings_pct == pytest.approx(100 * (2375 - 574) / 2375)  # ~75.8%
    assert c.ratio == pytest.approx(2375 / 574)  # ~4.1x
    assert c.category == "schema"


def test_derive_degenerate_zero_raw() -> None:
    c = derive("x", "schema", mcpg_tokens=10, raw_tokens=0)
    assert c.savings_pct == 0.0
    assert c.ratio == 0.0


def test_break_even_ceils_tasks() -> None:
    comps = [
        derive("schema", "schema", 574, 2375),  # saves 1801
        derive("plan", "query-plan", 146, 3847),  # saves 3701
        derive("tools", "tool-context", 48576, 193),  # upfront extra 48383
    ]
    be = break_even(comps)
    assert be["upfront_extra_tokens"] == 48383
    assert be["mean_per_call_saving_tokens"] == pytest.approx((1801 + 3701) / 2)  # 2751
    # ceil(48383 / 2751) == 18
    assert be["break_even_tasks"] == 18


def test_break_even_none_without_upfront_or_savings() -> None:
    # No tool-context row -> no break-even to compute.
    assert break_even([derive("schema", "schema", 574, 2375)])["break_even_tasks"] is None
    # Upfront cost but no per-call savings -> None (nothing to amortize it).
    only_cost = [derive("tools", "tool-context", 48576, 193)]
    assert break_even(only_cost)["break_even_tasks"] is None


def test_report_serializes_to_json() -> None:
    comps = [derive("schema", "schema", 574, 2375), derive("tools", "tool-context", 48576, 193)]
    report = TokenReport(metadata={"encoding": "o200k_base", "break_even": break_even(comps)}, comparisons=comps)
    d = report.to_dict()
    assert d["kind"] == "tokens_tier_a"
    assert d["schema_version"] == 1
    assert d["comparisons"][0]["savings_pct"] > 0
    # One per-call saving (1801) amortizes the 48383 upfront -> ceil = 27 tasks.
    assert d["metadata"]["break_even"]["break_even_tasks"] == 27


def test_count_tokens_if_available() -> None:
    pytest.importorskip("tiktoken")
    from benchmarks.tokens.tokenize import count_tokens

    assert count_tokens("") == 0
    assert count_tokens("hello world") > 0
    # Longer text encodes to more tokens.
    assert count_tokens("SELECT * FROM t") < count_tokens("SELECT * FROM t WHERE a = 1 AND b = 2 ORDER BY c")
