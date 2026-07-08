"""Tests for the WarehousePG MPP read introspection bundle (15.2-15.5).

Each function follows the same pattern:
  - probes `get_warehousepg_status` first
  - bails with `available=False` on vanilla PG (gate via 15.1)
  - returns a typed report on MPP servers
  - swallows driver errors and surfaces them in `detail`

The fixture `_mpp_status_routes` injects the routes that flip the
status probe to `available=True` so each per-function test only has
to add the routes for its own catalog query.
"""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg.warehousepg import (
    AppendOptimizedTableInfo,
    DistributionPolicy,
    ResourceGroup,
    SegmentHealth,
    check_segment_health,
    describe_ao_table,
    list_distribution_policies,
    list_resource_groups,
)

_WAREHOUSEPG_VERSION = "PostgreSQL 14.4 (WarehousePG 7.2.0 build commit:abc) on x86_64"
_VANILLA_PG_VERSION = "PostgreSQL 16.4 on x86_64-pc-linux-gnu"


def _mpp_status_routes(
    *,
    coordinator_role: str = "coordinator",
    primary_count: int = 4,
    mirror_count: int = 4,
) -> dict[str, list[dict[str, Any]]]:
    """Status-probe routes that flip `get_warehousepg_status` to available=True."""
    return {
        "SELECT version()": [{"version": _WAREHOUSEPG_VERSION}],
        "to_regclass": [{"oid": "pg_catalog.gp_segment_configuration"}],
        "FROM gp_segment_configuration WHERE content = -1": [
            {
                "coordinator_role": coordinator_role,
                "primary_count": primary_count,
                "mirror_count": mirror_count,
            }
        ],
    }


def _vanilla_status_routes() -> dict[str, list[dict[str, Any]]]:
    """Status routes for a vanilla PG cluster (the gate flips False)."""
    return {
        "SELECT version()": [{"version": _VANILLA_PG_VERSION}],
        "to_regclass": [{"oid": None}],
    }


# ---------------------------------------------------------------------------
# 15.2 — list_distribution_policies
# ---------------------------------------------------------------------------


async def test_list_distribution_policies_returns_unavailable_on_vanilla_pg() -> None:
    driver = FakeRoutingDriver(_vanilla_status_routes())
    result = await list_distribution_policies(driver, schema="public")
    assert result.available is False
    assert result.policies == []
    assert "MPP" in result.detail


async def test_list_distribution_policies_decodes_hash_random_replicated_policies() -> None:
    """Single-letter policytype codes ('p'/'r'/'n') decode to the
    human-readable HASH / REPLICATED / RANDOM forms."""
    routes = _mpp_status_routes()
    routes["FROM gp_distribution_policy"] = [
        {
            "schema": "public",
            "table_name": "fact_sales",
            "policy_type": "p",
            "num_segments": 8,
            "distribution_columns": ["order_id"],
        },
        {
            "schema": "public",
            "table_name": "dim_currency",
            "policy_type": "r",
            "num_segments": 0,
            "distribution_columns": [],
        },
        {
            "schema": "public",
            "table_name": "staging_events",
            "policy_type": "n",
            "num_segments": 0,
            "distribution_columns": [],
        },
    ]
    driver = FakeRoutingDriver(routes)
    result = await list_distribution_policies(driver, schema="public")
    assert result.available is True
    assert len(result.policies) == 3
    by_table = {p.table: p for p in result.policies}
    assert by_table["fact_sales"].method == "HASH"
    assert by_table["fact_sales"].distribution_columns == ["order_id"]
    assert by_table["dim_currency"].method == "REPLICATED"
    assert by_table["staging_events"].method == "RANDOM"


async def test_list_distribution_policies_passes_schema_as_a_parameter() -> None:
    """The schema name must be parameter-bound, not interpolated into SQL."""
    routes = _mpp_status_routes()
    routes["FROM gp_distribution_policy"] = []
    driver = FakeRoutingDriver(routes)
    await list_distribution_policies(driver, schema="my_app")
    # Find the gp_distribution_policy query and verify params.
    gp_calls = [c for c in driver.calls if "gp_distribution_policy" in c[0]]
    assert gp_calls, "expected at least one gp_distribution_policy query"
    assert gp_calls[0][1] == ["my_app"]


async def test_list_distribution_policies_handles_unknown_policy_code() -> None:
    """A code we don't recognise surfaces as itself uppercased — keeps
    the report informative even when a new policy type lands upstream."""
    routes = _mpp_status_routes()
    routes["FROM gp_distribution_policy"] = [
        {
            "schema": "public",
            "table_name": "weird",
            "policy_type": "z",
            "num_segments": 0,
            "distribution_columns": [],
        }
    ]
    driver = FakeRoutingDriver(routes)
    result = await list_distribution_policies(driver, schema="public")
    assert result.policies[0].method == "Z"


# ---------------------------------------------------------------------------
# 15.3 — check_segment_health
# ---------------------------------------------------------------------------


def _seg(
    *,
    dbid: int = 2,
    content: int = 0,
    role: str = "p",
    preferred_role: str = "p",
    mode: str = "s",
    status: str = "u",
    hostname: str = "seg1",
    port: int = 40000,
) -> dict[str, Any]:
    return {
        "dbid": dbid,
        "content": content,
        "role": role,
        "preferred_role": preferred_role,
        "mode": mode,
        "status": status,
        "hostname": hostname,
        "port": port,
    }


async def test_check_segment_health_returns_unavailable_on_vanilla_pg() -> None:
    driver = FakeRoutingDriver(_vanilla_status_routes())
    result = await check_segment_health(driver)
    assert result.available is False
    assert result.segments == []
    assert "MPP" in result.detail


async def test_check_segment_health_rolls_up_healthy_unhealthy_and_oos_counts() -> None:
    """One up-and-in-sync, one down, one post-failover, one not-in-sync.
    Counts should be 1 healthy / 3 unhealthy / 1 out-of-sync."""
    routes = _mpp_status_routes()
    routes["ORDER BY content, role"] = [
        _seg(dbid=1, content=-1, role="c", preferred_role="c"),  # coordinator OK
        _seg(dbid=2, content=0, role="p", preferred_role="p"),  # primary OK
        _seg(dbid=3, content=0, role="m", preferred_role="m", status="d"),  # mirror down
        _seg(dbid=4, content=1, role="m", preferred_role="p"),  # failed over
        _seg(dbid=5, content=1, role="p", preferred_role="m", mode="n"),  # OOS post-failover
    ]
    driver = FakeRoutingDriver(routes)
    result = await check_segment_health(driver)
    assert result.available is True
    assert result.total_segments == 5
    assert result.healthy_count == 2  # coordinator + first primary
    assert result.unhealthy_count == 3
    assert result.out_of_sync_count == 1


async def test_check_segment_health_clean_cluster_detail_is_positive() -> None:
    routes = _mpp_status_routes()
    routes["ORDER BY content, role"] = [
        _seg(dbid=1, content=-1, role="c", preferred_role="c"),
        _seg(dbid=2, content=0, role="p", preferred_role="p"),
        _seg(dbid=3, content=0, role="m", preferred_role="m"),
    ]
    driver = FakeRoutingDriver(routes)
    result = await check_segment_health(driver)
    assert "healthy" in result.detail.lower()


# ---------------------------------------------------------------------------
# 15.4 — describe_ao_table
# ---------------------------------------------------------------------------


async def test_describe_ao_table_returns_unavailable_on_vanilla_pg() -> None:
    driver = FakeRoutingDriver(_vanilla_status_routes())
    result = await describe_ao_table(driver, "public", "events_ao")
    assert result.is_ao is False
    assert result.compression_type is None
    assert "MPP" in result.detail


async def test_describe_ao_table_decodes_columnar_with_compression() -> None:
    """AO/CO column-oriented + zstd level 5 + 32 KiB blocks."""
    routes = _mpp_status_routes()
    routes["FROM pg_appendonly ao"] = [
        {
            "columnstore": True,
            "compresstype": "zstd",
            "compresslevel": 5,
            "blocksize": 32768,
            "checksum": True,
        }
    ]
    driver = FakeRoutingDriver(routes)
    result = await describe_ao_table(driver, "public", "events_ao")
    assert result.is_ao is True
    assert result.columnar is True
    assert result.compression_type == "zstd"
    assert result.compression_level == 5
    assert result.block_size == 32768
    assert result.checksum is True


async def test_describe_ao_table_normalises_compresstype_none_to_python_none() -> None:
    """``compresstype = 'none'`` in the catalog should surface as
    ``compression_type=None`` on the Python side — keeps callers from
    having to special-case the magic-string check."""
    routes = _mpp_status_routes()
    routes["FROM pg_appendonly ao"] = [
        {
            "columnstore": False,
            "compresstype": "none",
            "compresslevel": 0,
            "blocksize": 32768,
            "checksum": True,
        }
    ]
    driver = FakeRoutingDriver(routes)
    result = await describe_ao_table(driver, "public", "events_ao")
    assert result.compression_type is None


async def test_describe_ao_table_returns_is_ao_false_for_heap_tables() -> None:
    """No row in pg_appendonly → table is a regular heap. Distinguish
    from "MPP unavailable" by inspecting `is_ao` not `detail`."""
    routes = _mpp_status_routes()
    routes["FROM pg_appendonly ao"] = []
    driver = FakeRoutingDriver(routes)
    result = await describe_ao_table(driver, "public", "regular_heap")
    assert result.is_ao is False
    assert "not append-optimized" in result.detail


async def test_describe_ao_table_passes_schema_and_table_as_parameters() -> None:
    routes = _mpp_status_routes()
    routes["FROM pg_appendonly ao"] = []
    driver = FakeRoutingDriver(routes)
    await describe_ao_table(driver, "warehouse", "fact_events")
    ao_calls = [c for c in driver.calls if "pg_appendonly" in c[0]]
    assert ao_calls
    assert ao_calls[0][1] == ["warehouse", "fact_events"]


# ---------------------------------------------------------------------------
# 15.5 — list_resource_groups
# ---------------------------------------------------------------------------


async def test_list_resource_groups_returns_unavailable_on_vanilla_pg() -> None:
    driver = FakeRoutingDriver(_vanilla_status_routes())
    result = await list_resource_groups(driver)
    assert result.available is False
    assert result.groups == []
    assert "MPP" in result.detail


async def test_list_resource_groups_decodes_config_rows() -> None:
    routes = _mpp_status_routes()
    routes["FROM gp_toolkit.gp_resgroup_status"] = [
        {
            "groupname": "default_group",
            "concurrency": 20,
            "cpu_max_percent": 100,
            "cpu_weight": 100,
            "memory_limit": 30,
            "memory_shared_quota": 80,
            "num_running": 3,
            "num_queueing": 0,
        },
        {
            "groupname": "etl_group",
            "concurrency": 4,
            "cpu_max_percent": 50,
            "cpu_weight": 200,
            "memory_limit": 40,
            "memory_shared_quota": 90,
            "num_running": 2,
            "num_queueing": 5,
        },
    ]
    driver = FakeRoutingDriver(routes)
    result = await list_resource_groups(driver)
    assert result.available is True
    assert len(result.groups) == 2
    by_name = {g.name: g for g in result.groups}
    assert by_name["etl_group"].concurrency == 4
    assert by_name["etl_group"].active_queries == 2
    assert by_name["etl_group"].queued_queries == 5
    assert by_name["default_group"].cpu_max_percent == 100


# ---------------------------------------------------------------------------
# Common — error paths + immutability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "func_call",
    [
        lambda d: list_distribution_policies(d, schema="public"),
        lambda d: check_segment_health(d),
        lambda d: describe_ao_table(d, "public", "t"),
        lambda d: list_resource_groups(d),
    ],
)
async def test_all_reads_handle_driver_failure_on_data_query_gracefully(func_call: Any) -> None:
    """First two probes (status probe) succeed, the data query fails —
    every function in the bundle should swallow it and surface
    `available=False` with the error in detail rather than raising."""

    class _DataFailDriver:
        def __init__(self) -> None:
            self.call_index = 0

        async def execute_query(
            self, query: str, params: list[Any] | None = None, force_readonly: bool = False
        ) -> list[Any]:
            del force_readonly
            self.call_index += 1
            from mcpg.sql import SqlDriver

            # Status probe — 3 calls (version + to_regclass + segments).
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
            raise RuntimeError("data query failed: permission denied")

    result = await func_call(_DataFailDriver())
    available = getattr(result, "available", None) if not isinstance(result, AppendOptimizedTableInfo) else result.is_ao
    assert available is False or (isinstance(result, AppendOptimizedTableInfo) and not result.is_ao)
    assert "failed" in result.detail.lower() or "denied" in result.detail.lower()


@pytest.mark.parametrize(
    "instance",
    [
        DistributionPolicy(schema="s", table="t", method="HASH", distribution_columns=["x"], num_segments=1),
        SegmentHealth(
            dbid=1,
            content=0,
            role="p",
            preferred_role="p",
            mode="s",
            status="u",
            hostname="h",
            port=40000,
        ),
        ResourceGroup(
            name="g",
            concurrency=10,
            cpu_max_percent=100,
            cpu_weight=100,
            memory_limit=30,
            memory_shared_quota=80,
            active_queries=0,
            queued_queries=0,
        ),
    ],
)
def test_report_rows_are_immutable(instance: Any) -> None:
    """Every dataclass in the bundle is `frozen=True` so the wire
    shape can't be mutated by an agent inadvertently."""
    with pytest.raises(AttributeError):
        instance.schema = "modified"  # type: ignore[misc]
