"""Tests for the composite (multi-primitive) tools."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.composite import (
    CompositeError,
    SlowQuerySuggestion,
    _build_suggestions,
    _cache_hit_ratio,
    _check_identifier,
    _pk_columns,
    summarize_table,
)
from mcpg.config import load_settings
from mcpg.introspection import ConstraintInfo
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


def test_check_identifier_rejects_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(CompositeError, match="invalid table"):
        _check_identifier('w"; DROP', "table")
    with pytest.raises(CompositeError, match="invalid schema"):
        _check_identifier("with space", "schema")


def test_pk_columns_extracts_from_primary_key_definition() -> None:
    cons = [
        ConstraintInfo(name="widget_pkey", type="primary_key", definition="PRIMARY KEY (id)"),
        ConstraintInfo(name="other_check", type="check", definition="CHECK (qty >= 0)"),
    ]
    assert _pk_columns(cons) == ["id"]


def test_pk_columns_handles_composite_pk() -> None:
    cons = [
        ConstraintInfo(
            name="team_member_pkey",
            type="primary_key",
            definition='PRIMARY KEY ("team_id", user_id)',
        )
    ]
    assert _pk_columns(cons) == ["team_id", "user_id"]


def test_pk_columns_returns_empty_when_no_primary_key() -> None:
    assert _pk_columns([]) == []
    assert _pk_columns([ConstraintInfo(name="x", type="unique", definition="UNIQUE (email)")]) == []


# --- _build_suggestions -----------------------------------------------


def test_build_suggestions_flags_seq_scan_with_high_cost() -> None:
    suggestions = _build_suggestions(
        plan_summary={"sequential_scan_count": 2, "total_cost": 2500.0},
        active_queries=[],
        blocking_locks=[],
        cache_hit_ratio=0.99,
    )
    assert any(s.category == "plan" and "sequential scan" in s.hint for s in suggestions)


def test_build_suggestions_flags_high_total_cost_regardless_of_seq_scan() -> None:
    suggestions = _build_suggestions(
        plan_summary={"sequential_scan_count": 0, "total_cost": 200_000.0},
        active_queries=[],
        blocking_locks=[],
        cache_hit_ratio=0.99,
    )
    assert any(s.category == "plan" and "high" in s.hint.lower() for s in suggestions)


def test_build_suggestions_flags_blocking_locks() -> None:
    suggestions = _build_suggestions(
        plan_summary={"sequential_scan_count": 0, "total_cost": 1.0},
        active_queries=[],
        blocking_locks=[{"blocked_pid": 1, "blocking_pid": 2}],
        cache_hit_ratio=0.99,
    )
    assert any(s.category == "contention" and "lock" in s.hint for s in suggestions)


def test_build_suggestions_flags_low_cache_hit_ratio() -> None:
    suggestions = _build_suggestions(
        plan_summary={"sequential_scan_count": 0, "total_cost": 1.0},
        active_queries=[],
        blocking_locks=[],
        cache_hit_ratio=0.50,
    )
    assert any(s.category == "cache" and "cache" in s.hint for s in suggestions)


def test_build_suggestions_falls_back_to_explain_analyze_advice_when_nothing_else_fires() -> None:
    suggestions = _build_suggestions(
        plan_summary={"sequential_scan_count": 0, "total_cost": 1.0},
        active_queries=[],
        blocking_locks=[],
        cache_hit_ratio=0.99,
    )
    assert len(suggestions) == 1
    assert "EXPLAIN" in suggestions[0].hint


def test_slow_query_suggestion_dataclass_shape() -> None:
    sug = SlowQuerySuggestion(category="plan", hint="add an index")
    assert sug.category == "plan"
    assert sug.hint == "add an index"


# --- _cache_hit_ratio --------------------------------------------------


async def test_cache_hit_ratio_returns_none_when_no_io_yet() -> None:
    driver = FakeDriver([{"hits": 0, "reads": 0}])

    ratio = await _cache_hit_ratio(driver)  # type: ignore[arg-type]

    assert ratio is None


async def test_cache_hit_ratio_computes_hits_over_total() -> None:
    driver = FakeDriver([{"hits": 950, "reads": 50}])

    ratio = await _cache_hit_ratio(driver)  # type: ignore[arg-type]

    assert ratio == pytest.approx(0.95)


async def test_cache_hit_ratio_handles_null_aggregates() -> None:
    # pg_stat_database can return NULL for sum() on an empty cluster.
    driver = FakeDriver([{"hits": None, "reads": None}])

    ratio = await _cache_hit_ratio(driver)  # type: ignore[arg-type]

    assert ratio is None


# --- summarize_table --------------------------------------------------


async def test_summarize_table_rejects_unsafe_identifiers() -> None:
    driver = FakeRoutingDriver({})

    with pytest.raises(CompositeError, match="invalid"):
        await summarize_table(driver, 'app"; DROP', "widget")  # type: ignore[arg-type]


async def test_summarize_table_rejects_negative_sample_rows() -> None:
    driver = FakeRoutingDriver({})

    with pytest.raises(CompositeError, match="non-negative"):
        await summarize_table(driver, "app", "widget", sample_rows=-1)  # type: ignore[arg-type]


# --- tool registration -----------------------------------------------


async def test_summarize_table_tool_is_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "summarize_table" in listed


async def test_why_is_this_slow_tool_is_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "why_is_this_slow" in listed


async def test_why_is_this_slow_rejects_empty_sql() -> None:
    from mcpg.composite import why_is_this_slow

    with pytest.raises(CompositeError, match="empty"):
        await why_is_this_slow(FakeDriver(), "   ")  # type: ignore[arg-type]
