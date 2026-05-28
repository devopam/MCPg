"""Tests for the Query Syntax Optimizer (optimize_query) tool."""

import json

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.advisors import optimize_query
from mcpg.config import load_settings
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_optimize_query_detects_all_anti_patterns() -> None:
    # Setup EXPLAIN plan double
    plan_data = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "large_table",
                "Total Cost": 123.45,
                "Plan Rows": 1000,
                "Plans": [],
            }
        }
    ]
    driver = FakeRoutingDriver({"EXPLAIN (FORMAT JSON)": [{"explain": json.dumps(plan_data)}]})

    # Query containing SELECT *, missing LIMIT, IN (SELECT ...), and leading wildcard LIKE '%abc%'
    sql = "SELECT * FROM large_table WHERE name LIKE '%abc%' AND id IN (SELECT id FROM other);"
    res = await optimize_query(driver, sql)  # type: ignore[arg-type]

    assert res.original_sql == sql
    # Replaced SELECT * and appended LIMIT 100
    assert "SELECT id, [explicit_columns]" in res.optimized_sql
    assert "LIMIT 100;" in res.optimized_sql

    # Verify findings are populated
    findings = set(res.findings)
    assert any("SELECT *" in f for f in findings)
    assert any("LIMIT" in f for f in findings)
    assert any("IN (SELECT ...)" in f for f in findings)
    assert any("wildcard" in f for f in findings)
    assert any("Sequential scan" in f for f in findings)

    # Rationale should suggest fixes
    assert "SELECT *" in res.rationale
    assert "LIMIT 100" in res.rationale
    assert "pg_trgm" in res.rationale
    assert "Seq Scan" in res.rationale


async def test_optimize_query_tool_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "optimize_query" in listed

        # Run optimizer through the tool with a failing query plan (graceful degradation)
        result = await client.call_tool("optimize_query", {"sql": "SELECT 1;"})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["original_sql"] == "SELECT 1;"
