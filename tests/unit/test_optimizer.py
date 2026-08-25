"""Tests for the Query Syntax Optimizer (optimize_query) tool."""

import json
import logging

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from _mcp_test_helpers import create_connected_server_and_client_session

from mcpg import advisors
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


class _FlakyPlan:
    """A plan double whose ``sequential_scans`` raises on its third access.

    ``optimize_query`` reads ``plan.sequential_scans`` twice while building
    the EXPLAIN summary (truthy check, then ``", ".join(...)``) inside a
    ``try``/``except QueryError`` block, and once more later while
    composing the rationale, inside a separate ``try``/``except
    Exception`` block. Raising only on the third access exercises that
    second, best-effort block without tripping the first.
    """

    total_cost = 12.0
    estimated_rows = 5
    node_types = ("Seq Scan",)

    def __init__(self) -> None:
        self._accesses = 0

    @property
    def sequential_scans(self) -> list[str]:
        self._accesses += 1
        if self._accesses <= 2:
            return ["large_table"]
        raise RuntimeError("plan inspection blew up")


async def test_optimize_query_logs_debug_when_sequential_scan_advisory_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure while composing the seq-scan advisory line logs at debug, not silently."""

    async def _flaky_analyze_query_plan(driver: object, sql: str) -> _FlakyPlan:
        return _FlakyPlan()

    monkeypatch.setattr(advisors, "analyze_query_plan", _flaky_analyze_query_plan)

    # setup_logging() (invoked by create_server in other tests in this
    # session) disables propagation on the "mcpg" logger to avoid
    # double-logging in production; restore it here so caplog (which
    # attaches to the root logger) can see records from "mcpg.advisors".
    root_logger = logging.getLogger("mcpg")
    old_propagate = root_logger.propagate
    root_logger.propagate = True
    try:
        caplog.set_level(logging.DEBUG, logger="mcpg.advisors")

        driver = FakeRoutingDriver({})
        res = await optimize_query(driver, "SELECT * FROM large_table;")  # type: ignore[arg-type]

        # The function completes normally — the failure is swallowed, not raised.
        assert res.original_sql == "SELECT * FROM large_table;"
        assert any(
            "sequential-scan advisory" in record.message and record.levelno == logging.DEBUG
            for record in caplog.records
        )
    finally:
        root_logger.propagate = old_propagate


async def test_optimize_query_tool_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "optimize_query" in listed

        # Run optimizer through the tool with a failing query plan (graceful degradation)
        result = await client.call_tool("optimize_query", {"sql": "SELECT 1;"})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["original_sql"] == "SELECT 1;"
