"""Tests for the PG 19 partition-reorganisation module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver

from mcpg.pg19_partitions import (
    MergePartitionsResult,
    Pg19PartitionsError,
    Pg19PartitionsStatus,
    SplitPartitionResult,
    SplitPartitionSpec,
    get_pg19_partitions_status,
    merge_partitions,
    split_partition,
)


def _version_route(num: int, ver: str) -> dict[str, list[dict[str, object]]]:
    return {"current_setting('server_version_num')": [{"ver_num": num, "ver": ver}]}


# --- get_pg19_partitions_status -------------------------------------------


async def test_status_available_on_pg19() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    status = await get_pg19_partitions_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, Pg19PartitionsStatus)
    assert status.available is True
    assert "MERGE PARTITIONS" in status.detail


async def test_status_unavailable_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    status = await get_pg19_partitions_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "detach" in status.detail.lower()


async def test_status_never_raises_on_driver_failure() -> None:
    driver = FakeDriver(fail=True)
    status = await get_pg19_partitions_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "version probe failed" in status.detail


# --- merge_partitions -----------------------------------------------------


def _pg19_database() -> FakeDatabase:
    driver = FakeDriver()
    driver._rows = [{"ver_num": 190001, "ver": "19beta1"}]  # type: ignore[attr-defined]
    return FakeDatabase(driver)


def _pg18_database() -> FakeDatabase:
    driver = FakeDriver()
    driver._rows = [{"ver_num": 180003, "ver": "18.3"}]  # type: ignore[attr-defined]
    return FakeDatabase(driver)


async def test_merge_partitions_emits_alter_table() -> None:
    db = _pg19_database()
    result = await merge_partitions(
        db,  # type: ignore[arg-type]
        parent_schema="public",
        parent_table="measurement",
        source_partitions=["measurement_y2024_q1", "measurement_y2024_q2"],
        target_partition_name="measurement_y2024_h1",
    )
    assert isinstance(result, MergePartitionsResult)
    assert result.target_partition == "measurement_y2024_h1"
    assert result.source_partitions == ("measurement_y2024_q1", "measurement_y2024_q2")
    assert db.unmanaged == [
        'ALTER TABLE "public"."measurement" MERGE PARTITIONS '
        '("measurement_y2024_q1", "measurement_y2024_q2") INTO "measurement_y2024_h1"'
    ]


async def test_merge_partitions_requires_at_least_two_sources() -> None:
    db = _pg19_database()
    with pytest.raises(Pg19PartitionsError, match="at least two"):
        await merge_partitions(
            db,  # type: ignore[arg-type]
            parent_schema="public",
            parent_table="measurement",
            source_partitions=["only_one"],
            target_partition_name="target",
        )
    assert db.unmanaged == []


async def test_merge_partitions_raises_on_pg18() -> None:
    db = _pg18_database()
    with pytest.raises(Pg19PartitionsError, match="PostgreSQL 19"):
        await merge_partitions(
            db,  # type: ignore[arg-type]
            parent_schema="public",
            parent_table="measurement",
            source_partitions=["a", "b"],
            target_partition_name="merged",
        )
    assert db.unmanaged == []


async def test_merge_partitions_wraps_unmanaged_failure() -> None:
    db = _pg19_database()
    db.unmanaged_fail = True
    with pytest.raises(Pg19PartitionsError, match="MERGE PARTITIONS failed"):
        await merge_partitions(
            db,  # type: ignore[arg-type]
            parent_schema="public",
            parent_table="measurement",
            source_partitions=["a", "b"],
            target_partition_name="merged",
        )


async def test_merge_partitions_quotes_identifiers_against_injection() -> None:
    db = _pg19_database()
    result = await merge_partitions(
        db,  # type: ignore[arg-type]
        parent_schema='evil"sch',
        parent_table='evil"tbl',
        source_partitions=['p"1', 'p"2'],
        target_partition_name='t"arget',
    )
    assert db.unmanaged == ['ALTER TABLE "evil""sch"."evil""tbl" MERGE PARTITIONS ("p""1", "p""2") INTO "t""arget"']
    assert result.target_partition == 't"arget'


# --- split_partition ------------------------------------------------------


async def test_split_partition_range_form() -> None:
    db = _pg19_database()
    specs = [
        SplitPartitionSpec(name="measurement_y2024_h1", for_values_clause="FROM ('2024-01-01') TO ('2024-07-01')"),
        SplitPartitionSpec(name="measurement_y2024_h2", for_values_clause="FROM ('2024-07-01') TO ('2025-01-01')"),
    ]
    result = await split_partition(
        db,  # type: ignore[arg-type]
        parent_schema="public",
        parent_table="measurement",
        source_partition="measurement_y2024",
        new_partitions=specs,
    )
    assert isinstance(result, SplitPartitionResult)
    assert result.new_partitions == ("measurement_y2024_h1", "measurement_y2024_h2")
    assert db.unmanaged == [
        'ALTER TABLE "public"."measurement" SPLIT PARTITION "measurement_y2024" INTO ('
        """PARTITION "measurement_y2024_h1" FOR VALUES FROM ('2024-01-01') TO ('2024-07-01'), """
        """PARTITION "measurement_y2024_h2" FOR VALUES FROM ('2024-07-01') TO ('2025-01-01'))"""
    ]


async def test_split_partition_list_form() -> None:
    db = _pg19_database()
    specs = [
        SplitPartitionSpec(name="region_north", for_values_clause="IN ('north', 'north_east')"),
        SplitPartitionSpec(name="region_south", for_values_clause="IN ('south', 'south_west')"),
    ]
    await split_partition(
        db,  # type: ignore[arg-type]
        parent_schema="public",
        parent_table="orders",
        source_partition="orders_by_region",
        new_partitions=specs,
    )
    sql = db.unmanaged[0]
    assert "FOR VALUES IN ('north', 'north_east')" in sql
    assert "FOR VALUES IN ('south', 'south_west')" in sql


async def test_split_partition_requires_at_least_two_new() -> None:
    db = _pg19_database()
    with pytest.raises(Pg19PartitionsError, match="at least two"):
        await split_partition(
            db,  # type: ignore[arg-type]
            parent_schema="public",
            parent_table="measurement",
            source_partition="src",
            new_partitions=[SplitPartitionSpec(name="only", for_values_clause="FROM (1) TO (2)")],
        )
    assert db.unmanaged == []


async def test_split_partition_raises_on_pg18() -> None:
    db = _pg18_database()
    specs = [
        SplitPartitionSpec(name="a", for_values_clause="FROM (1) TO (2)"),
        SplitPartitionSpec(name="b", for_values_clause="FROM (2) TO (3)"),
    ]
    with pytest.raises(Pg19PartitionsError, match="PostgreSQL 19"):
        await split_partition(
            db,  # type: ignore[arg-type]
            parent_schema="public",
            parent_table="measurement",
            source_partition="src",
            new_partitions=specs,
        )
    assert db.unmanaged == []


async def test_split_partition_wraps_unmanaged_failure() -> None:
    db = _pg19_database()
    db.unmanaged_fail = True
    specs = [
        SplitPartitionSpec(name="a", for_values_clause="FROM (1) TO (2)"),
        SplitPartitionSpec(name="b", for_values_clause="FROM (2) TO (3)"),
    ]
    with pytest.raises(Pg19PartitionsError, match="SPLIT PARTITION failed"):
        await split_partition(
            db,  # type: ignore[arg-type]
            parent_schema="public",
            parent_table="measurement",
            source_partition="src",
            new_partitions=specs,
        )


async def test_split_partition_quotes_new_partition_names() -> None:
    db = _pg19_database()
    specs = [
        SplitPartitionSpec(name='a"evil', for_values_clause="FROM (1) TO (2)"),
        SplitPartitionSpec(name='b"evil', for_values_clause="FROM (2) TO (3)"),
    ]
    await split_partition(
        db,  # type: ignore[arg-type]
        parent_schema="public",
        parent_table="measurement",
        source_partition='s"rc',
        new_partitions=specs,
    )
    sql = db.unmanaged[0]
    assert 'SPLIT PARTITION "s""rc"' in sql
    assert 'PARTITION "a""evil"' in sql
    assert 'PARTITION "b""evil"' in sql


# --- Dataclass shapes -----------------------------------------------------


def test_dataclass_shapes() -> None:
    status = Pg19PartitionsStatus(available=True, server_version_num=190001, server_version="19beta1", detail="ok")
    assert status.available is True
    merge = MergePartitionsResult(
        parent_schema="public",
        parent_table="t",
        source_partitions=("a", "b"),
        target_partition="m",
        merge_sql="ALTER TABLE ...",
    )
    assert merge.target_partition == "m"
    spec = SplitPartitionSpec(name="n", for_values_clause="FROM (1) TO (2)")
    assert spec.name == "n"
    split = SplitPartitionResult(
        parent_schema="public",
        parent_table="t",
        source_partition="src",
        new_partitions=("a", "b"),
        split_sql="ALTER TABLE ...",
    )
    assert split.new_partitions == ("a", "b")
