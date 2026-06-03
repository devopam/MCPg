"""Tests for the pg_buffercache integration."""

from __future__ import annotations

import json

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.io_stats import (
    BufferCacheRelationsReport,
    BufferCacheSummaryReport,
    read_pg_buffercache_relations,
    read_pg_buffercache_summary,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_read_pg_buffercache_summary_extension_not_installed() -> None:
    # extension_installed checks pg_extension for the name. We return empty list.
    driver = FakeRoutingDriver({"pg_extension": []})

    report = await read_pg_buffercache_summary(driver)  # type: ignore[arg-type]

    assert isinstance(report, BufferCacheSummaryReport)
    assert report.available is False
    assert report.total_buffers is None


async def test_read_pg_buffercache_summary_installed_but_empty() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "total_buffers": [],
        }
    )

    report = await read_pg_buffercache_summary(driver)  # type: ignore[arg-type]

    assert isinstance(report, BufferCacheSummaryReport)
    assert report.available is True
    assert report.total_buffers is None


async def test_read_pg_buffercache_summary_installed_with_data() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "total_buffers": [
                {
                    "total_buffers": 16384,
                    "free_buffers": 4096,
                    "used_buffers": 12288,
                    "dirty_buffers": 256,
                    "average_usage_count": 3.14,
                }
            ],
        }
    )

    report = await read_pg_buffercache_summary(driver)  # type: ignore[arg-type]

    assert isinstance(report, BufferCacheSummaryReport)
    assert report.available is True
    assert report.total_buffers == 16384
    assert report.free_buffers == 4096
    assert report.used_buffers == 12288
    assert report.dirty_buffers == 256
    assert report.average_usage_count == 3.14


async def test_read_pg_buffercache_relations_extension_not_installed() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    report = await read_pg_buffercache_relations(driver)  # type: ignore[arg-type]

    assert isinstance(report, BufferCacheRelationsReport)
    assert report.available is False
    assert report.relations == []


async def test_read_pg_buffercache_relations_installed_with_data() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "schema_name": [
                {
                    "schema_name": "public",
                    "relation_name": "users",
                    "relation_kind": "r",
                    "buffered_blocks": 100,
                    "buffered_bytes": 819200,
                    "buffer_percent": 0.61,
                    "percent_of_relation_buffered": 100.0,
                    "average_usage_count": 4.5,
                    "dirty_pages": 10,
                },
                {
                    "schema_name": "public",
                    "relation_name": "users_pkey",
                    "relation_kind": "i",
                    "buffered_blocks": 50,
                    "buffered_bytes": 409600,
                    "buffer_percent": 0.31,
                    "percent_of_relation_buffered": 80.0,
                    "average_usage_count": 3.0,
                    "dirty_pages": 0,
                },
                {
                    "schema_name": "public",
                    "relation_name": "unknown_rel",
                    "relation_kind": "x",
                    "buffered_blocks": 10,
                    "buffered_bytes": 81920,
                    "buffer_percent": 0.06,
                    "percent_of_relation_buffered": None,
                    "average_usage_count": None,
                    "dirty_pages": 1,
                },
            ],
        }
    )

    report = await read_pg_buffercache_relations(driver)  # type: ignore[arg-type]

    assert isinstance(report, BufferCacheRelationsReport)
    assert report.available is True
    assert len(report.relations) == 3

    r1 = report.relations[0]
    assert r1.schema_name == "public"
    assert r1.relation_name == "users"
    assert r1.relation_kind == "table"
    assert r1.buffered_blocks == 100
    assert r1.buffered_bytes == 819200
    assert r1.buffer_percent == 0.61
    assert r1.percent_of_relation_buffered == 100.0
    assert r1.average_usage_count == 4.5
    assert r1.dirty_pages == 10

    r2 = report.relations[1]
    assert r2.relation_kind == "index"

    r3 = report.relations[2]
    assert r3.relation_kind == "unknown (x)"
    assert r3.percent_of_relation_buffered is None
    assert r3.average_usage_count is None


async def test_read_pg_buffercache_relations_filters_by_schema() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "schema_name": [],
        }
    )

    await read_pg_buffercache_relations(driver, schema="app", limit=10)  # type: ignore[arg-type]

    # Verify query structure and params
    calls = driver.calls
    assert len(calls) > 1  # first is extension check, second is relations query
    rel_query, rel_params, _ = calls[1]
    assert "WHERE n.nspname = %s" in rel_query
    assert "LIMIT %s" in rel_query
    assert rel_params == ["app", 10]


# --- MCP tool registration tests ---


async def test_buffercache_tools_are_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools
        names = {tool.name for tool in tools}

    assert "read_pg_buffercache_summary" in names
    assert "read_pg_buffercache_relations" in names


async def test_buffercache_tools_execution() -> None:
    fake_driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "total_buffers": [
                {
                    "total_buffers": 100,
                    "free_buffers": 10,
                    "used_buffers": 90,
                    "dirty_buffers": 5,
                    "average_usage_count": 2.5,
                }
            ],
            "schema_name": [
                {
                    "schema_name": "public",
                    "relation_name": "users",
                    "relation_kind": "r",
                    "buffered_blocks": 100,
                    "buffered_bytes": 819200,
                    "buffer_percent": 0.61,
                    "percent_of_relation_buffered": 100.0,
                    "average_usage_count": 4.5,
                    "dirty_pages": 10,
                }
            ],
        }
    )
    server = create_server(_SETTINGS, database=FakeDatabase(fake_driver))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        # Call read_pg_buffercache_summary tool
        result_summary = await client.call_tool("read_pg_buffercache_summary", {})
        assert result_summary.isError is False
        assert result_summary.content[0].text is not None
        summary_data = json.loads(result_summary.content[0].text)
        assert summary_data["available"] is True
        assert summary_data["total_buffers"] == 100

        # Call read_pg_buffercache_relations tool
        result_relations = await client.call_tool("read_pg_buffercache_relations", {"schema": "public", "limit": 5})
        assert result_relations.isError is False
        assert result_relations.content[0].text is not None
        relations_data = json.loads(result_relations.content[0].text)
        assert relations_data["available"] is True
        assert len(relations_data["relations"]) == 1
        assert relations_data["relations"][0]["relation_name"] == "users"
