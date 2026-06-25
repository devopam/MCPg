"""Tests for the WarehousePG MPP advisors bundle (15.6 + 15.7)."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg.warehousepg import (
    MppMotionNode,
    RedistributeCandidate,
    analyze_mpp_query_plan,
    recommend_redistribute,
)

_WAREHOUSEPG_VERSION = "PostgreSQL 14.4 (WarehousePG 7.2.0 build) on x86_64"
_VANILLA_PG_VERSION = "PostgreSQL 16.4 on x86_64-pc-linux-gnu"


def _mpp_status_routes(*, primary_count: int = 4) -> dict[str, list[dict[str, Any]]]:
    return {
        "SELECT version()": [{"version": _WAREHOUSEPG_VERSION}],
        "to_regclass": [{"oid": "pg_catalog.gp_segment_configuration"}],
        "FROM gp_segment_configuration WHERE content = -1": [
            {
                "coordinator_role": "coordinator",
                "primary_count": primary_count,
                "mirror_count": primary_count,
            }
        ],
    }


def _vanilla_status_routes() -> dict[str, list[dict[str, Any]]]:
    return {
        "SELECT version()": [{"version": _VANILLA_PG_VERSION}],
        "to_regclass": [{"oid": None}],
    }


# ---------------------------------------------------------------------------
# 15.6 — analyze_mpp_query_plan
# ---------------------------------------------------------------------------


async def test_analyze_mpp_query_plan_returns_unavailable_on_vanilla_pg() -> None:
    driver = FakeRoutingDriver(_vanilla_status_routes())
    result = await analyze_mpp_query_plan(driver, "SELECT 1")
    assert result.available is False
    assert result.motions == []
    assert "MPP" in result.detail


async def test_analyze_mpp_query_plan_classifies_redistribute_motion() -> None:
    """One Redistribute Motion + one Gather Motion = bad join co-location."""
    plan = [
        {
            "Plan": {
                "Node Type": "Gather Motion",
                "Slice": 0,
                "Senders": 4,
                "Receivers": 1,
                "Plan Rows": 1000,
                "Plans": [
                    {
                        "Node Type": "Hash Join",
                        "Slice": 1,
                        "Plans": [
                            {"Node Type": "Seq Scan", "Slice": 1},
                            {
                                "Node Type": "Redistribute Motion",
                                "Slice": 1,
                                "Senders": 4,
                                "Receivers": 4,
                                "Plan Rows": 500,
                                "Plans": [{"Node Type": "Seq Scan", "Slice": 2}],
                            },
                        ],
                    }
                ],
            }
        }
    ]
    routes = _mpp_status_routes()
    # explain_query uses EXPLAIN (FORMAT JSON) for the safety pre-flight,
    # then EXPLAIN (ANALYZE, BUFFERS, TIMING, FORMAT JSON) for io=True.
    routes["EXPLAIN (ANALYZE"] = [{"QUERY PLAN": plan}]
    routes["EXPLAIN (FORMAT JSON)"] = [{"QUERY PLAN": [{"Plan": {"Node Type": "Result"}}]}]
    driver = FakeRoutingDriver(routes)
    result = await analyze_mpp_query_plan(driver, "SELECT * FROM big")
    assert result.available is True
    assert result.motion_count == 2
    assert result.redistribute_count == 1
    assert result.gather_count == 1
    assert result.broadcast_count == 0
    assert result.slice_count == 3  # slices 0, 1, 2
    assert "redistribute" in result.detail.lower()


async def test_analyze_mpp_query_plan_no_motions_means_co_located() -> None:
    """A plan with no motion nodes — every step runs on each segment
    locally; ideal MPP layout."""
    plan = [
        {
            "Plan": {
                "Node Type": "Aggregate",
                "Slice": 0,
                "Plans": [
                    {"Node Type": "Seq Scan", "Slice": 0, "Relation Name": "fact"},
                ],
            }
        }
    ]
    routes = _mpp_status_routes()
    routes["EXPLAIN (ANALYZE"] = [{"QUERY PLAN": plan}]
    routes["EXPLAIN (FORMAT JSON)"] = [{"QUERY PLAN": [{"Plan": {"Node Type": "Result"}}]}]
    driver = FakeRoutingDriver(routes)
    result = await analyze_mpp_query_plan(driver, "SELECT 1")
    assert result.motion_count == 0
    assert result.slice_count == 1
    assert "co-located" in result.detail


async def test_analyze_mpp_query_plan_detects_broadcast() -> None:
    plan = [
        {
            "Plan": {
                "Node Type": "Broadcast Motion",
                "Slice": 0,
                "Senders": 1,
                "Receivers": 4,
                "Plans": [{"Node Type": "Seq Scan", "Slice": 1}],
            }
        }
    ]
    routes = _mpp_status_routes()
    routes["EXPLAIN (ANALYZE"] = [{"QUERY PLAN": plan}]
    routes["EXPLAIN (FORMAT JSON)"] = [{"QUERY PLAN": [{"Plan": {"Node Type": "Result"}}]}]
    driver = FakeRoutingDriver(routes)
    result = await analyze_mpp_query_plan(driver, "SELECT 1")
    assert result.broadcast_count == 1
    assert "broadcast" in result.detail.lower()


async def test_analyze_mpp_query_plan_explain_failure_surfaces_as_available_false() -> None:
    """The 15.1 status probe passes; the EXPLAIN itself fails (e.g. the
    SQL doesn't parse or the table doesn't exist)."""

    class _ExplainFailingDriver:
        def __init__(self) -> None:
            self.call_index = 0

        async def execute_query(
            self, query: str, params: list[Any] | None = None, force_readonly: bool = False
        ) -> list[Any]:
            del params, force_readonly
            from mcpg._vendor.sql import SqlDriver

            self.call_index += 1
            if self.call_index == 1:
                return [SqlDriver.RowResult(cells={"version": _WAREHOUSEPG_VERSION})]
            if self.call_index == 2:
                return [SqlDriver.RowResult(cells={"oid": "pg_catalog.gp_segment_configuration"})]
            if self.call_index == 3:
                return [
                    SqlDriver.RowResult(
                        cells={
                            "coordinator_role": "coordinator",
                            "primary_count": 4,
                            "mirror_count": 4,
                        }
                    )
                ]
            raise RuntimeError("relation does not exist")

    result = await analyze_mpp_query_plan(_ExplainFailingDriver(), "SELECT 1")  # type: ignore[arg-type]
    assert result.available is False
    assert "failed" in result.detail.lower()


# ---------------------------------------------------------------------------
# 15.7 — recommend_redistribute
# ---------------------------------------------------------------------------


async def test_recommend_redistribute_returns_unavailable_on_vanilla_pg() -> None:
    driver = FakeRoutingDriver(_vanilla_status_routes())
    result = await recommend_redistribute(driver, "public", "fact")
    assert result.available is False
    assert "Greenplum" in result.detail


async def test_recommend_redistribute_skips_non_hash_distributions() -> None:
    """RANDOM and REPLICATED distributions don't have a key to optimize."""
    routes = _mpp_status_routes()
    routes["LEFT JOIN gp_distribution_policy"] = [
        {
            "reltuples": 1000,
            "policy_type": "n",  # RANDOM
            "num_segments": 4,
            "distribution_columns": [],
        }
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_redistribute(driver, "public", "rand_table")
    assert result.available is True
    assert result.current_method == "RANDOM"
    assert result.recommendation is None
    assert result.suggested_ddl is None


async def test_recommend_redistribute_flags_low_cardinality_current_key() -> None:
    """Current key has 4 distinct values across 4 segments — very skewed.
    Top candidate has 1M distinct values — clear recommendation."""
    routes = _mpp_status_routes(primary_count=4)
    routes["LEFT JOIN gp_distribution_policy"] = [
        {
            "reltuples": 1_000_000,
            "policy_type": "p",  # HASH
            "num_segments": 4,
            "distribution_columns": ["status"],
        }
    ]
    routes["FROM pg_stats"] = [
        {"attname": "status", "n_distinct": 4.0, "data_type": "text"},
        {"attname": "user_id", "n_distinct": 950000.0, "data_type": "bigint"},
        {"attname": "created_at", "n_distinct": 99999.0, "data_type": "timestamptz"},
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_redistribute(driver, "public", "events")
    assert result.available is True
    assert result.current_method == "HASH"
    assert result.current_columns == ["status"]
    assert result.recommendation is not None
    assert result.recommendation.column == "user_id"
    assert result.suggested_ddl is not None
    assert "REORGANIZE=TRUE" in result.suggested_ddl
    assert "user_id" in result.suggested_ddl


async def test_recommend_redistribute_skips_when_current_key_is_already_good() -> None:
    """Current key has high cardinality already — no rewrite needed."""
    routes = _mpp_status_routes(primary_count=4)
    routes["LEFT JOIN gp_distribution_policy"] = [
        {
            "reltuples": 1_000_000,
            "policy_type": "p",
            "num_segments": 4,
            "distribution_columns": ["user_id"],
        }
    ]
    routes["FROM pg_stats"] = [
        {"attname": "user_id", "n_distinct": 950000.0, "data_type": "bigint"},
        {"attname": "status", "n_distinct": 4.0, "data_type": "text"},
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_redistribute(driver, "public", "events")
    assert result.recommendation is None
    assert result.suggested_ddl is None
    assert "reasonable" in result.detail.lower()


async def test_recommend_redistribute_handles_negative_n_distinct_fraction() -> None:
    """pg_stats encodes 'distinct values are a fraction of rows' as
    negative numbers. The advisor must translate them to absolute
    counts using reltuples."""
    routes = _mpp_status_routes(primary_count=4)
    routes["LEFT JOIN gp_distribution_policy"] = [
        {
            "reltuples": 1_000_000,
            "policy_type": "p",
            "num_segments": 4,
            "distribution_columns": ["status"],
        }
    ]
    routes["FROM pg_stats"] = [
        # status: 4 absolute distinct values
        {"attname": "status", "n_distinct": 4.0, "data_type": "text"},
        # uuid: every row distinct (encoded as -1.0)
        {"attname": "uuid", "n_distinct": -1.0, "data_type": "uuid"},
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_redistribute(driver, "public", "events")
    assert result.recommendation is not None
    assert result.recommendation.column == "uuid"
    assert result.recommendation.approx_distinct == 1_000_000


async def test_recommend_redistribute_handles_missing_table() -> None:
    routes = _mpp_status_routes()
    routes["LEFT JOIN gp_distribution_policy"] = []
    driver = FakeRoutingDriver(routes)
    result = await recommend_redistribute(driver, "public", "nope")
    assert result.available is False
    assert "not found" in result.detail


# ---------------------------------------------------------------------------
# Common — immutability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "instance",
    [
        MppMotionNode(kind="Gather Motion", slice_index=0, senders=4, receivers=1, estimated_rows=100),
        RedistributeCandidate(column="x", data_type="int", n_distinct=10.0, approx_distinct=10),
    ],
)
def test_advisor_dataclasses_are_frozen(instance: Any) -> None:
    with pytest.raises(AttributeError):
        instance.kind = "modified"  # type: ignore[misc,attr-defined]
