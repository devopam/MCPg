"""Tests for the pg_prewarm coverage module."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg.pg_prewarm import (
    AutowarmJob,
    BulkPrewarmResult,
    PrewarmedRelation,
    PrewarmError,
    PrewarmExtensionStatus,
    PrewarmRecommendation,
    PrewarmResult,
    RecommendPrewarmTargetsResult,
    ScheduleAutowarmResult,
    UnscheduleAutowarmResult,
    get_prewarm_extension_status,
    list_autowarm_jobs,
    list_prewarmed_relations,
    prewarm_recommended,
    prewarm_relation,
    recommend_prewarm_targets,
    schedule_autowarm,
    unschedule_autowarm,
)


def _shared_buffers_route(blocks: int = 16384) -> dict[str, list[dict[str, Any]]]:
    """Stat helper for the shared_buffers / block_size probe."""
    return {"pg_size_bytes(current_setting('shared_buffers'))": [{"blocks": blocks}]}


# --- get_prewarm_extension_status ------------------------------------------


async def test_status_reports_missing_extensions() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_extension WHERE extname": [],
            "current_setting('shared_preload_libraries'": [{"spl": ""}],
        }
    )
    status = await get_prewarm_extension_status(driver)  # type: ignore[arg-type]
    assert status == PrewarmExtensionStatus(
        pg_prewarm_installed=False,
        pg_buffercache_installed=False,
        autoprewarm_libraries_present=False,
        shared_preload_libraries="",
    )


async def test_status_detects_autoprewarm_library_in_spl() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_extension WHERE extname": [{"present": 1}],
            "current_setting('shared_preload_libraries'": [{"spl": "pg_stat_statements, pg_prewarm , pg_cron"}],
        }
    )
    status = await get_prewarm_extension_status(driver)  # type: ignore[arg-type]
    assert status.pg_prewarm_installed is True
    assert status.pg_buffercache_installed is True
    assert status.autoprewarm_libraries_present is True


# --- list_prewarmed_relations ----------------------------------------------


async def test_list_prewarmed_returns_empty_when_buffercache_absent() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": []})
    assert await list_prewarmed_relations(driver) == []  # type: ignore[arg-type]


async def test_list_prewarmed_maps_rows_and_computes_pct() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_extension WHERE extname": [{"present": 1}],
            "FROM pg_class c ": [
                {
                    "schema": "public",
                    "table_name": "orders",
                    "blocks_cached": 200,
                    "dirty_blocks": 5,
                    "total_blocks": 1000,
                },
                {"schema": "public", "table_name": "users", "blocks_cached": 50, "dirty_blocks": 0, "total_blocks": 0},
            ],
        }
    )
    rels = await list_prewarmed_relations(driver)  # type: ignore[arg-type]
    assert rels == [
        PrewarmedRelation("public", "orders", 200, 1000, 20.0, 5),
        PrewarmedRelation("public", "users", 50, 0, 0.0, 0),
    ]


async def test_list_prewarmed_rejects_bad_limit() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(PrewarmError, match="limit must be positive"):
        await list_prewarmed_relations(driver, limit=0)  # type: ignore[arg-type]


# --- recommend_prewarm_targets ---------------------------------------------


async def test_recommend_filters_and_classifies() -> None:
    routes = {
        "FROM pg_stat_user_tables s ": [
            # seq_scan dominant + high miss → seq_scan_dominant.
            {
                "schema": "public",
                "relation": "events",
                "heap_blks_read": 50_000,
                "heap_blks_hit": 5_000,
                "seq_scans": 100,
                "idx_scans": 10,
                "est_blocks": 100,
            },
            # high miss but idx_scan dominant → high_cold_miss_rate.
            {
                "schema": "public",
                "relation": "logs",
                "heap_blks_read": 30_000,
                "heap_blks_hit": 5_000,
                "seq_scans": 5,
                "idx_scans": 100,
                "est_blocks": 200,
            },
            # small hot relation uncached.
            {
                "schema": "public",
                "relation": "lookup",
                "heap_blks_read": 5_000,
                "heap_blks_hit": 50_000,
                "seq_scans": 100,
                "idx_scans": 100,
                "est_blocks": 10,
            },
            # filtered: low absolute reads.
            {
                "schema": "public",
                "relation": "cold",
                "heap_blks_read": 50,
                "heap_blks_hit": 5,
                "seq_scans": 5,
                "idx_scans": 0,
                "est_blocks": 100,
            },
        ],
    }
    routes.update(_shared_buffers_route(16384))
    driver = FakeRoutingDriver(routes)
    result = await recommend_prewarm_targets(driver)  # type: ignore[arg-type]
    names = [c.relation for c in result.candidates]
    assert "events" in names and "logs" in names and "lookup" in names
    assert "cold" not in names
    reasons = {c.relation: c.reason for c in result.candidates}
    assert reasons["events"] == "seq_scan_dominant"
    assert reasons["logs"] == "high_cold_miss_rate"
    assert reasons["lookup"] == "small_hot_relation_uncached"
    # Ready-to-run SQL is the canonical pg_prewarm signature.
    for c in result.candidates:
        assert c.ready_to_run_sql.startswith("SELECT pg_prewarm('")
        assert c.prewarm_mode == "buffer"


async def test_recommend_respects_shared_buffers_budget() -> None:
    routes = {
        "FROM pg_stat_user_tables s ": [
            # Each row is 600 blocks; budget 1000 → only first two fit.
            {
                "schema": "public",
                "relation": "a",
                "heap_blks_read": 50_000,
                "heap_blks_hit": 0,
                "seq_scans": 0,
                "idx_scans": 100,
                "est_blocks": 600,
            },
            {
                "schema": "public",
                "relation": "b",
                "heap_blks_read": 40_000,
                "heap_blks_hit": 0,
                "seq_scans": 0,
                "idx_scans": 100,
                "est_blocks": 400,
            },
            {
                "schema": "public",
                "relation": "c",
                "heap_blks_read": 30_000,
                "heap_blks_hit": 0,
                "seq_scans": 0,
                "idx_scans": 100,
                "est_blocks": 600,
            },
        ],
    }
    # shared_buffers = 1666 blocks * 60% = 999 → first two fit (600 + 400 = 1000 exceeds, so b alone keeps room).
    # Set shared_buffers to 1667 blocks: 60% = 1000, so a(600)+b(400)=1000 fits, c(600) doesn't.
    routes.update(_shared_buffers_route(1667))
    driver = FakeRoutingDriver(routes)
    result = await recommend_prewarm_targets(driver, shared_buffers_budget_pct=60.0)  # type: ignore[arg-type]
    names = [c.relation for c in result.candidates]
    assert names == ["a", "b"]
    assert result.total_cost_blocks == 1000
    assert result.budget_blocks == 1000
    assert result.shared_buffers_blocks == 1667


async def test_recommend_rejects_bad_inputs() -> None:
    driver = FakeRoutingDriver({"FROM pg_stat_user_tables s ": []})
    with pytest.raises(PrewarmError, match="prewarm_mode"):
        await recommend_prewarm_targets(driver, prewarm_mode="invalid")  # type: ignore[arg-type]
    with pytest.raises(PrewarmError, match="shared_buffers_budget_pct"):
        await recommend_prewarm_targets(driver, shared_buffers_budget_pct=0)  # type: ignore[arg-type]
    with pytest.raises(PrewarmError, match="limit"):
        await recommend_prewarm_targets(driver, limit=0)  # type: ignore[arg-type]


# --- prewarm_relation -------------------------------------------------------


async def test_prewarm_relation_emits_select_with_regclass_cast() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_extension WHERE extname": [{"present": 1}],
            "SELECT pg_prewarm(": [{"blocks": 1024}],
        }
    )
    result = await prewarm_relation(driver, schema="public", relation="orders")  # type: ignore[arg-type]
    assert result == PrewarmResult("public", "orders", "buffer", 1024)
    queries = " | ".join(call[0] for call in driver.calls)
    assert "pg_prewarm('public.orders'::regclass, 'buffer')" in queries


async def test_prewarm_relation_rejects_bad_mode() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(PrewarmError, match="mode must be"):
        await prewarm_relation(driver, schema="public", relation="orders", mode="bogus")  # type: ignore[arg-type]


async def test_prewarm_relation_rejects_quotes_in_relation() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(PrewarmError, match="characters"):
        await prewarm_relation(driver, schema="public", relation="orders'; DROP --")  # type: ignore[arg-type]


async def test_prewarm_relation_requires_extension() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": []})
    with pytest.raises(PrewarmError, match="pg_prewarm extension"):
        await prewarm_relation(driver, schema="public", relation="orders")  # type: ignore[arg-type]


# --- prewarm_recommended ----------------------------------------------------


async def test_bulk_dry_run_does_not_call_pg_prewarm() -> None:
    routes = {
        "FROM pg_extension WHERE extname": [{"present": 1}],
        "FROM pg_stat_user_tables s ": [
            {
                "schema": "public",
                "relation": "a",
                "heap_blks_read": 50_000,
                "heap_blks_hit": 0,
                "seq_scans": 0,
                "idx_scans": 100,
                "est_blocks": 100,
            },
        ],
    }
    routes.update(_shared_buffers_route(16384))
    driver = FakeRoutingDriver(routes)
    result = await prewarm_recommended(driver, dry_run=True)  # type: ignore[arg-type]
    assert isinstance(result, BulkPrewarmResult)
    assert result.dry_run is True
    assert result.total_blocks == 0
    assert len(result.outcomes) == 1
    queries = " | ".join(call[0] for call in driver.calls)
    # The dry-run path never executes the actual pg_prewarm call.
    assert "SELECT pg_prewarm('public.a'" not in queries


async def test_bulk_apply_invokes_pg_prewarm_per_candidate() -> None:
    routes = {
        "FROM pg_extension WHERE extname": [{"present": 1}],
        "FROM pg_stat_user_tables s ": [
            {
                "schema": "public",
                "relation": "a",
                "heap_blks_read": 50_000,
                "heap_blks_hit": 0,
                "seq_scans": 0,
                "idx_scans": 100,
                "est_blocks": 100,
            },
            {
                "schema": "public",
                "relation": "b",
                "heap_blks_read": 30_000,
                "heap_blks_hit": 0,
                "seq_scans": 0,
                "idx_scans": 100,
                "est_blocks": 100,
            },
        ],
        "SELECT pg_prewarm(": [{"blocks": 500}],
    }
    routes.update(_shared_buffers_route(16384))
    driver = FakeRoutingDriver(routes)
    result = await prewarm_recommended(driver)  # type: ignore[arg-type]
    assert result.dry_run is False
    # Both outcomes succeed with 500 blocks each.
    assert all(o.error is None for o in result.outcomes)
    assert result.total_blocks == 1000


# --- autowarm scheduling ----------------------------------------------------


async def test_schedule_autowarm_calls_cron_schedule() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_extension WHERE extname": [{"present": 1}],
            "SELECT cron.schedule(": [{"jobid": 42}],
        }
    )
    result = await schedule_autowarm(driver)  # type: ignore[arg-type]
    assert result == ScheduleAutowarmResult(jobid=42, name="mcpg_autowarm", schedule="@reboot")
    schedule_calls = [c for c in driver.calls if "cron.schedule" in c[0]]
    assert len(schedule_calls) == 1
    sql, params, _ = schedule_calls[0]
    assert "cron.schedule(%s, %s, %s)" in sql
    assert params is not None
    assert params[0] == "mcpg_autowarm"
    assert params[1] == "@reboot"
    assert "prewarm_recommended_cron" in params[2]


async def test_schedule_autowarm_rejects_pg_cron_absent() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": []})
    with pytest.raises(PrewarmError, match="pg_cron extension"):
        await schedule_autowarm(driver)  # type: ignore[arg-type]


async def test_schedule_autowarm_rejects_bad_schedule() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(PrewarmError, match="schedule contains"):
        await schedule_autowarm(driver, schedule="'; DROP TABLE cron.job; --")  # type: ignore[arg-type]


async def test_schedule_autowarm_rejects_bad_mode() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(PrewarmError, match="prewarm_mode"):
        await schedule_autowarm(driver, prewarm_mode="bogus")  # type: ignore[arg-type]


async def test_unschedule_autowarm_returns_false_when_pg_cron_absent() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": []})
    result = await unschedule_autowarm(driver)  # type: ignore[arg-type]
    assert result == UnscheduleAutowarmResult(name="mcpg_autowarm", removed=False)


async def test_unschedule_autowarm_calls_cron_unschedule() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_extension WHERE extname": [{"present": 1}],
            "SELECT cron.unschedule(": [{"removed": True}],
        }
    )
    result = await unschedule_autowarm(driver)  # type: ignore[arg-type]
    assert result.removed is True


async def test_list_autowarm_jobs_filters_to_mcpg_prefix() -> None:
    driver = FakeRoutingDriver(
        {
            "FROM pg_extension WHERE extname": [{"present": 1}],
            "FROM cron.job WHERE jobname LIKE": [
                {"jobid": 1, "jobname": "mcpg_autowarm", "schedule": "@reboot", "command": "SELECT 1"},
                {"jobid": 2, "jobname": "mcpg_autowarm_nightly", "schedule": "0 3 * * *", "command": "SELECT 2"},
            ],
        }
    )
    jobs = await list_autowarm_jobs(driver)  # type: ignore[arg-type]
    assert jobs == [
        AutowarmJob(1, "mcpg_autowarm", "@reboot", "SELECT 1"),
        AutowarmJob(2, "mcpg_autowarm_nightly", "0 3 * * *", "SELECT 2"),
    ]


async def test_list_autowarm_jobs_empty_when_cron_absent() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": []})
    assert await list_autowarm_jobs(driver) == []  # type: ignore[arg-type]


def test_recommendation_dataclass_shape() -> None:
    rec = PrewarmRecommendation(
        schema="public",
        relation="orders",
        reason="high_cold_miss_rate",
        prewarm_mode="buffer",
        estimated_buffer_cost=1000,
        heap_blks_read=50_000,
        heap_blks_hit=1_000,
        cache_miss_ratio=0.98,
        ready_to_run_sql="SELECT pg_prewarm('public.orders'::regclass, 'buffer');",
    )
    assert rec.reason == "high_cold_miss_rate"


def test_recommend_result_shape() -> None:
    result = RecommendPrewarmTargetsResult(
        shared_buffers_blocks=16384, budget_blocks=9830, total_cost_blocks=500, candidates=[]
    )
    assert result.shared_buffers_blocks == 16384
