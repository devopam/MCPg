"""Tests for the dynamic ``headline_tools`` recommender (roadmap 14.4)."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.headline_curator import (
    BucketHeadlineRecommendation,
    HeadlineCuratorError,
    HeadlineRecommendationReport,
    recommend_headline_tools,
)


def _audit_present(present: bool) -> dict[str, list[dict[str, object]]]:
    return {"to_regclass('mcpg_audit.events')": [{"present": present}]}


def _events_route(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    return {"FROM mcpg_audit.events": rows}


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


async def test_rejects_zero_lookback() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(HeadlineCuratorError, match="lookback_days"):
        await recommend_headline_tools(driver, lookback_days=0)  # type: ignore[arg-type]


async def test_rejects_lookback_over_90() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(HeadlineCuratorError, match="90"):
        await recommend_headline_tools(driver, lookback_days=120)  # type: ignore[arg-type]


async def test_rejects_zero_top_n() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(HeadlineCuratorError, match="top_n"):
        await recommend_headline_tools(driver, top_n=0)  # type: ignore[arg-type]


async def test_rejects_top_n_over_50() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(HeadlineCuratorError, match="50"):
        await recommend_headline_tools(driver, top_n=99)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Audit-table presence
# ---------------------------------------------------------------------------


async def test_returns_diagnostic_when_audit_table_missing() -> None:
    driver = FakeRoutingDriver(_audit_present(False))
    report = await recommend_headline_tools(driver)  # type: ignore[arg-type]
    assert isinstance(report, HeadlineRecommendationReport)
    assert report.audit_table_present is False
    assert report.buckets == []
    assert "MCPG_AUDIT" in report.detail


async def test_idle_window_returns_empty_recommendations_with_table_present() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([]))
    driver = FakeRoutingDriver(routes)
    report = await recommend_headline_tools(driver)  # type: ignore[arg-type]
    assert report.audit_table_present is True
    assert report.events_examined == 0
    # Every bucket gets a row, but each recommended tuple is empty.
    assert all(b.recommended == () for b in report.buckets)
    assert "No successful tool calls" in report.detail


# ---------------------------------------------------------------------------
# Recommendation shape — uses real classify_tool routing.
# ---------------------------------------------------------------------------


async def test_top_n_per_bucket_respects_call_count_ranking() -> None:
    """Three schema_introspection tools at different call counts —
    they land in the schema_introspection bucket in DESC order."""
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(
        _events_route(
            [
                {"tool": "list_tables", "call_count": 100},
                {"tool": "list_indexes", "call_count": 50},
                {"tool": "describe_table", "call_count": 200},
            ]
        )
    )
    driver = FakeRoutingDriver(routes)
    report = await recommend_headline_tools(driver, top_n=3)  # type: ignore[arg-type]
    schema_bucket = next(b for b in report.buckets if b.bucket_id == "schema_introspection")
    assert schema_bucket.recommended == ("describe_table", "list_tables", "list_indexes")


async def test_top_n_caps_per_bucket() -> None:
    """top_n=2 → only the top two from each bucket reach `recommended`."""
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(
        _events_route(
            [
                {"tool": "list_tables", "call_count": 100},
                {"tool": "list_indexes", "call_count": 50},
                {"tool": "describe_table", "call_count": 200},
            ]
        )
    )
    driver = FakeRoutingDriver(routes)
    report = await recommend_headline_tools(driver, top_n=2)  # type: ignore[arg-type]
    schema_bucket = next(b for b in report.buckets if b.bucket_id == "schema_introspection")
    assert len(schema_bucket.recommended) == 2


async def test_call_counts_are_propagated_to_recommendation() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([{"tool": "list_tables", "call_count": 42}]))
    driver = FakeRoutingDriver(routes)
    report = await recommend_headline_tools(driver)  # type: ignore[arg-type]
    schema_bucket = next(b for b in report.buckets if b.bucket_id == "schema_introspection")
    assert schema_bucket.call_counts == {"list_tables": 42}


# ---------------------------------------------------------------------------
# Diff fields — newcomers and departures
# ---------------------------------------------------------------------------


async def test_newcomers_flag_recommended_not_in_current() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([{"tool": "list_tables", "call_count": 1}]))
    driver = FakeRoutingDriver(routes)
    # Current headline doesn't include list_tables — it should land as a newcomer.
    report = await recommend_headline_tools(  # type: ignore[arg-type]
        driver,
        current_headlines={"schema_introspection": ("describe_table",)},
    )
    schema_bucket = next(b for b in report.buckets if b.bucket_id == "schema_introspection")
    assert schema_bucket.newcomers == ("list_tables",)


async def test_departures_flag_current_not_in_recommended() -> None:
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([{"tool": "list_tables", "call_count": 1}]))
    driver = FakeRoutingDriver(routes)
    report = await recommend_headline_tools(  # type: ignore[arg-type]
        driver,
        current_headlines={"schema_introspection": ("retired_tool",)},
    )
    schema_bucket = next(b for b in report.buckets if b.bucket_id == "schema_introspection")
    assert schema_bucket.departures == ("retired_tool",)


# ---------------------------------------------------------------------------
# Deterministic bucket ordering
# ---------------------------------------------------------------------------


async def test_bucket_order_matches_curated_display_order() -> None:
    """Report's `buckets` list comes back in CAPABILITIES order, NOT
    BUCKET_IDS (frozenset hash) order. Gemini critical on #180."""
    from mcpg.about import CAPABILITIES

    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([]))
    driver = FakeRoutingDriver(routes)
    report = await recommend_headline_tools(driver)  # type: ignore[arg-type]
    expected_order = [cap.id for cap in CAPABILITIES]
    actual_order = [b.bucket_id for b in report.buckets]
    assert actual_order == expected_order


# ---------------------------------------------------------------------------
# Successful-only filter
# ---------------------------------------------------------------------------


async def test_query_filters_on_success_status() -> None:
    """The recommender only considers successful events — failures
    shouldn't push a flaky tool into a bucket's headline."""
    routes: dict[str, list[dict[str, object]]] = {}
    routes.update(_audit_present(True))
    routes.update(_events_route([{"tool": "list_tables", "call_count": 1}]))
    driver = FakeRoutingDriver(routes)
    await recommend_headline_tools(driver)  # type: ignore[arg-type]
    events_call = next(call for call in driver.calls if "FROM mcpg_audit.events" in call[0])
    assert "status = 'success'" in events_call[0]


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


async def test_returned_dataclasses_are_frozen() -> None:
    rec = BucketHeadlineRecommendation(bucket_id="x", current=(), recommended=(), newcomers=(), departures=())
    with pytest.raises((AttributeError, Exception)):
        rec.bucket_id = "y"  # type: ignore[misc]
    rep = HeadlineRecommendationReport(audit_table_present=True, lookback_days=7, top_n=6, events_examined=0)
    with pytest.raises((AttributeError, Exception)):
        rep.lookback_days = 1  # type: ignore[misc]
