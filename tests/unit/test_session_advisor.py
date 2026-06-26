"""Tests for the session-scope cost advisor (roadmap 8.7)."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.session_advisor import (
    REASON_HOT_REPEATED_CALL,
    REASON_IDLE_SESSION,
    REASON_REDUNDANT_LISTING,
    CostFinding,
    SessionAdvisorError,
    SessionCostAnalysis,
    analyze_session_cost,
)


def _audit_present(present: bool) -> dict[str, list[dict[str, object]]]:
    return {"to_regclass('mcpg_audit.events')": [{"present": present}]}


def _events_route(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    return {"FROM mcpg_audit.events": rows}


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


async def test_rejects_negative_lookback() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(SessionAdvisorError, match="lookback_minutes"):
        await analyze_session_cost(driver, lookback_minutes=0)  # type: ignore[arg-type]


async def test_rejects_lookback_over_24h() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(SessionAdvisorError, match="1440"):
        await analyze_session_cost(driver, lookback_minutes=10_000)  # type: ignore[arg-type]


async def test_rejects_zero_threshold() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(SessionAdvisorError, match="hot_threshold"):
        await analyze_session_cost(driver, hot_threshold=0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Audit-table presence
# ---------------------------------------------------------------------------


async def test_returns_diagnostic_when_audit_table_missing() -> None:
    driver = FakeRoutingDriver(_audit_present(False))
    result = await analyze_session_cost(driver)  # type: ignore[arg-type]
    assert isinstance(result, SessionCostAnalysis)
    assert result.audit_table_present is False
    assert result.events_examined == 0
    assert result.findings == []
    assert "MCPG_AUDIT" in result.detail


async def test_returns_idle_finding_when_no_events_in_window() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([]))
    driver = FakeRoutingDriver(routes)
    result = await analyze_session_cost(driver, lookback_minutes=15)  # type: ignore[arg-type]
    assert result.audit_table_present is True
    assert result.events_examined == 0
    assert len(result.findings) == 1
    assert result.findings[0].reason == REASON_IDLE_SESSION
    assert "15 minute" in result.findings[0].suggestion


# ---------------------------------------------------------------------------
# Finding classification
# ---------------------------------------------------------------------------


async def test_redundant_listing_classified_for_catalogue_tool() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([{"tool": "list_tables", "call_count": 47}]))
    driver = FakeRoutingDriver(routes)
    result = await analyze_session_cost(driver, hot_threshold=10)  # type: ignore[arg-type]
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.reason == REASON_REDUNDANT_LISTING
    assert finding.tool == "list_tables"
    assert finding.call_count == 47
    assert "get_compact_schema" in finding.suggestion


async def test_hot_repeated_call_classified_for_non_catalogue_tool() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([{"tool": "explain_query", "call_count": 25}]))
    driver = FakeRoutingDriver(routes)
    result = await analyze_session_cost(driver, hot_threshold=10)  # type: ignore[arg-type]
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.reason == REASON_HOT_REPEATED_CALL
    assert finding.tool == "explain_query"
    assert "Cache" in finding.suggestion


async def test_under_threshold_emits_no_finding() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([{"tool": "list_tables", "call_count": 3}, {"tool": "explain_query", "call_count": 2}]))
    driver = FakeRoutingDriver(routes)
    result = await analyze_session_cost(driver, hot_threshold=10)  # type: ignore[arg-type]
    assert result.events_examined == 5
    assert result.findings == []
    assert "no tool exceeded" in result.detail


async def test_threshold_inclusive_lower_bound() -> None:
    """Equal-to-threshold doesn't flag — only strictly above does."""
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([{"tool": "list_tables", "call_count": 10}]))
    driver = FakeRoutingDriver(routes)
    result = await analyze_session_cost(driver, hot_threshold=10)  # type: ignore[arg-type]
    assert result.findings == []


# ---------------------------------------------------------------------------
# Cumulative event count + sort
# ---------------------------------------------------------------------------


async def test_examines_all_events_even_when_only_some_flag() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(
        _events_route(
            [
                {"tool": "list_tables", "call_count": 47},
                {"tool": "describe_table", "call_count": 3},
                {"tool": "list_indexes", "call_count": 22},
            ]
        )
    )
    driver = FakeRoutingDriver(routes)
    result = await analyze_session_cost(driver, hot_threshold=10)  # type: ignore[arg-type]
    assert result.events_examined == 47 + 3 + 22
    # Both list_* tools should land as redundant_listing findings.
    flagged_tools = {f.tool for f in result.findings}
    assert flagged_tools == {"list_tables", "list_indexes"}
    assert all(f.reason == REASON_REDUNDANT_LISTING for f in result.findings)


# ---------------------------------------------------------------------------
# Parameter binding — lookback_minutes lands in params, not f-string
# ---------------------------------------------------------------------------


async def test_lookback_lands_as_bound_parameter() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([]))
    driver = FakeRoutingDriver(routes)
    await analyze_session_cost(driver, lookback_minutes=42)  # type: ignore[arg-type]
    # Find the events query call and confirm 42 is in its params.
    events_call = next(call for call in driver.calls if "FROM mcpg_audit.events" in call[0])
    assert events_call[1] == [42]


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


async def test_returned_dataclasses_are_frozen() -> None:
    f = CostFinding(reason="x", tool="t", call_count=1, suggestion="s")
    with pytest.raises((AttributeError, Exception)):
        f.tool = "y"  # type: ignore[misc]
    r = SessionCostAnalysis(audit_table_present=True, events_examined=0, lookback_minutes=60)
    with pytest.raises((AttributeError, Exception)):
        r.events_examined = 1  # type: ignore[misc]
