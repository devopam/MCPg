"""Tests for the pg_walinspect integration."""

from __future__ import annotations

import json

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.walinspect import (
    WalRecordsReport,
    WalStatsReport,
    read_pg_wal_records,
    read_pg_wal_stats,
)

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_read_pg_wal_records_extension_not_installed() -> None:
    # extension_installed checks pg_extension for the name. We return empty list.
    driver = FakeRoutingDriver({"pg_extension": []})

    report = await read_pg_wal_records(driver, "0/E419E28")  # type: ignore[arg-type]

    assert isinstance(report, WalRecordsReport)
    assert report.available is False
    assert report.records == []


async def test_read_pg_wal_records_installed_with_data() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_get_wal_records_info": [
                {
                    "start_lsn": "0/E419E28",
                    "end_lsn": "0/E419E70",
                    "prev_lsn": "0/E419DF0",
                    "xid": 1234,
                    "resource_manager": "Heap",
                    "record_type": "INSERT",
                    "record_length": 72,
                    "main_data_length": 30,
                    "fpi_length": 0,
                    "description": "insert: rel 1663/1244314123/16384 tid 0/1",
                    "block_ref": "blk 0: rel 1663/1244314123/16384 fork main",
                }
            ],
        }
    )

    report = await read_pg_wal_records(driver, "0/E419E28")  # type: ignore[arg-type]

    assert isinstance(report, WalRecordsReport)
    assert report.available is True
    assert len(report.records) == 1

    r = report.records[0]
    assert r.start_lsn == "0/E419E28"
    assert r.end_lsn == "0/E419E70"
    assert r.prev_lsn == "0/E419DF0"
    assert r.xid == 1234
    assert r.resource_manager == "Heap"
    assert r.record_type == "INSERT"
    assert r.record_length == 72
    assert r.main_data_length == 30
    assert r.fpi_length == 0
    assert r.description == "insert: rel 1663/1244314123/16384 tid 0/1"
    assert r.block_ref == "blk 0: rel 1663/1244314123/16384 fork main"


async def test_read_pg_wal_stats_extension_not_installed() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    report = await read_pg_wal_stats(driver, "0/E419E28")  # type: ignore[arg-type]

    assert isinstance(report, WalStatsReport)
    assert report.available is False
    assert report.stats == []


async def test_read_pg_wal_stats_by_resource_manager() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_get_wal_stats": [
                {
                    "resource_manager": "Heap",
                    "count": 10,
                    "count_percentage": 50.0,
                    "record_size": 720,
                    "record_size_percentage": 45.0,
                    "fpi_size": 0,
                    "fpi_size_percentage": 0.0,
                    "combined_size": 720,
                    "combined_size_percentage": 45.0,
                },
                {
                    "resource_manager": "Btree",
                    "count": 10,
                    "count_percentage": 50.0,
                    "record_size": 880,
                    "record_size_percentage": 55.0,
                    "fpi_size": 0,
                    "fpi_size_percentage": 0.0,
                    "combined_size": 880,
                    "combined_size_percentage": 55.0,
                },
            ],
        }
    )

    report = await read_pg_wal_stats(driver, "0/E419E28", per_record=False)  # type: ignore[arg-type]

    assert isinstance(report, WalStatsReport)
    assert report.available is True
    assert len(report.stats) == 2

    s1 = report.stats[0]
    assert s1.resource_manager_or_record_type == "Heap"
    assert s1.count == 10
    assert s1.count_percentage == 50.0
    assert s1.record_size == 720
    assert s1.record_size_percentage == 45.0

    s2 = report.stats[1]
    assert s2.resource_manager_or_record_type == "Btree"


async def test_read_pg_wal_stats_per_record() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_get_wal_stats": [
                {
                    "record_type": "INSERT",
                    "count": 5,
                    "count_percentage": 100.0,
                    "record_size": 360,
                    "record_size_percentage": 100.0,
                    "fpi_size": 0,
                    "fpi_size_percentage": 0.0,
                    "combined_size": 360,
                    "combined_size_percentage": 100.0,
                }
            ],
        }
    )

    report = await read_pg_wal_stats(driver, "0/E419E28", per_record=True)  # type: ignore[arg-type]

    assert isinstance(report, WalStatsReport)
    assert report.available is True
    assert len(report.stats) == 1
    assert report.stats[0].resource_manager_or_record_type == "INSERT"


# --- MCP tool registration tests ---


async def test_walinspect_tools_are_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools
        names = {tool.name for tool in tools}

    assert "read_pg_wal_records" in names
    assert "read_pg_wal_stats" in names


async def test_walinspect_tools_execution() -> None:
    fake_driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_is_in_recovery": [{"lsn": "0/E419E70"}],
            "pg_get_wal_records_info": [
                {
                    "start_lsn": "0/E419E28",
                    "end_lsn": "0/E419E70",
                    "prev_lsn": "0/E419DF0",
                    "xid": 1234,
                    "resource_manager": "Heap",
                    "record_type": "INSERT",
                    "record_length": 72,
                    "main_data_length": 30,
                    "fpi_length": 0,
                    "description": "insert: rel 1663/1244314123/16384 tid 0/1",
                    "block_ref": "blk 0: rel 1663/1244314123/16384 fork main",
                }
            ],
            "pg_get_wal_stats": [
                {
                    "resource_manager": "Heap",
                    "count": 10,
                    "count_percentage": 100.0,
                    "record_size": 720,
                    "record_size_percentage": 100.0,
                    "fpi_size": 0,
                    "fpi_size_percentage": 0.0,
                    "combined_size": 720,
                    "combined_size_percentage": 100.0,
                }
            ],
        }
    )
    server = create_server(_SETTINGS, database=FakeDatabase(fake_driver))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        # Call read_pg_wal_records tool
        res_records = await client.call_tool("read_pg_wal_records", {"start_lsn": "0/E419E28"})
        assert res_records.isError is False
        assert res_records.content[0].text is not None
        records_data = json.loads(res_records.content[0].text)
        assert records_data["available"] is True
        assert len(records_data["records"]) == 1
        assert records_data["records"][0]["xid"] == 1234

        # Call read_pg_wal_stats tool
        res_stats = await client.call_tool("read_pg_wal_stats", {"start_lsn": "0/E419E28"})
        assert res_stats.isError is False
        assert res_stats.content[0].text is not None
        stats_data = json.loads(res_stats.content[0].text)
        assert stats_data["available"] is True
        assert len(stats_data["stats"]) == 1
        assert stats_data["stats"][0]["resource_manager_or_record_type"] == "Heap"


async def test_read_pg_wal_records_resolves_lsn_happy_path() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_is_in_recovery": [{"lsn": "0/E419E70"}],
            "pg_get_wal_records_info": [],
        }
    )

    await read_pg_wal_records(driver, "0/E419E28", end_lsn=None)  # type: ignore[arg-type]

    assert len(driver.calls) == 3
    assert "pg_extension" in driver.calls[0][0]
    assert "pg_is_in_recovery" in driver.calls[1][0]
    assert "pg_get_wal_records_info" in driver.calls[2][0]
    assert driver.calls[2][1] == ["0/E419E28", "0/E419E70", 100]


async def test_read_pg_wal_records_resolves_lsn_fallback() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_get_wal_records_info": [],
        }
    )

    await read_pg_wal_records(driver, "0/E419E28", end_lsn=None)  # type: ignore[arg-type]

    assert len(driver.calls) == 3
    assert "pg_is_in_recovery" in driver.calls[1][0]
    # fell back to start_lsn
    assert driver.calls[2][1] == ["0/E419E28", "0/E419E28", 100]


async def test_read_pg_wal_records_resolves_lsn_ffffffff() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_is_in_recovery": [{"lsn": "0/E419E70"}],
            "pg_get_wal_records_info": [],
        }
    )

    await read_pg_wal_records(driver, "0/E419E28", end_lsn="FFFFFFFF/FFFFFFFF")  # type: ignore[arg-type]

    assert len(driver.calls) == 3
    assert "pg_is_in_recovery" in driver.calls[1][0]
    assert driver.calls[2][1] == ["0/E419E28", "0/E419E70", 100]


async def test_read_pg_wal_stats_resolves_lsn_happy_path() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pg_is_in_recovery": [{"lsn": "0/E419E70"}],
            "pg_get_wal_stats": [],
        }
    )

    await read_pg_wal_stats(driver, "0/E419E28", end_lsn=None)  # type: ignore[arg-type]

    assert len(driver.calls) == 3
    assert "pg_is_in_recovery" in driver.calls[1][0]
    assert driver.calls[2][1] == ["0/E419E28", "0/E419E70", False]
