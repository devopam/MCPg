"""Tests for slow-call logging in MCPg server."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from mcpg.config import load_settings
from mcpg.server import create_server


def test_slow_call_config_parsing() -> None:
    # 1. Test default value
    settings = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})
    assert settings.slow_call_threshold_ms == 1000

    # 2. Test custom positive value
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SLOW_CALL_THRESHOLD_MS": "500",
        }
    )
    assert settings.slow_call_threshold_ms == 500

    # 3. Test invalid value raises ConfigError
    from mcpg.config import ConfigError

    with pytest.raises(ConfigError) as exc_info:
        load_settings(
            {
                "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
                "MCPG_SLOW_CALL_THRESHOLD_MS": "not-an-int",
            }
        )
    assert "MCPG_SLOW_CALL_THRESHOLD_MS must be an integer" in str(exc_info.value)


@pytest.mark.anyio
async def test_slow_call_warning_emitted_when_exceeding_threshold(caplog) -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SLOW_CALL_THRESHOLD_MS": "100",  # 0.1 seconds
        }
    )

    server = create_server(settings)

    # We patch super().call_tool to simulate a successful tool run
    # and time.monotonic to simulate 0.15 seconds duration (exceeds threshold)
    with (
        patch("mcp.server.fastmcp.FastMCP.call_tool", return_value="ok_result"),
        patch("time.monotonic", side_effect=[0.0, 0.15]),
        caplog.at_level(logging.WARNING, logger="mcpg.server"),
    ):
        # Propagate must be enabled for caplog to capture sub-logger logs
        mcpg_logger = logging.getLogger("mcpg")
        old_propagate = mcpg_logger.propagate
        mcpg_logger.propagate = True
        try:
            result = await server.call_tool("my_tool", {})
            assert result == "ok_result"

            # Check warning was logged
            warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == "mcpg.server"]
            assert len(warnings) == 1
            assert "Slow tool call: my_tool took 0.150s" in warnings[0].message
        finally:
            mcpg_logger.propagate = old_propagate


@pytest.mark.anyio
async def test_slow_call_warning_not_emitted_when_under_threshold(caplog) -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SLOW_CALL_THRESHOLD_MS": "100",  # 0.1 seconds
        }
    )

    server = create_server(settings)

    with (
        patch("mcp.server.fastmcp.FastMCP.call_tool", return_value="ok_result"),
        patch("time.monotonic", side_effect=[0.0, 0.05]),
        caplog.at_level(logging.WARNING, logger="mcpg.server"),
    ):
        mcpg_logger = logging.getLogger("mcpg")
        old_propagate = mcpg_logger.propagate
        mcpg_logger.propagate = True
        try:
            await server.call_tool("my_tool", {})

            warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == "mcpg.server"]
            assert len(warnings) == 0
        finally:
            mcpg_logger.propagate = old_propagate


@pytest.mark.anyio
async def test_slow_call_warning_not_emitted_when_disabled(caplog) -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SLOW_CALL_THRESHOLD_MS": "0",  # Disabled
        }
    )

    server = create_server(settings)

    with (
        patch("mcp.server.fastmcp.FastMCP.call_tool", return_value="ok_result"),
        patch("time.monotonic", side_effect=[0.0, 5.0]),
        caplog.at_level(logging.WARNING, logger="mcpg.server"),
    ):
        mcpg_logger = logging.getLogger("mcpg")
        old_propagate = mcpg_logger.propagate
        mcpg_logger.propagate = True
        try:
            await server.call_tool("my_tool", {})

            warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == "mcpg.server"]
            assert len(warnings) == 0
        finally:
            mcpg_logger.propagate = old_propagate


@pytest.mark.anyio
async def test_slow_call_warning_emitted_on_error_path(caplog) -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_SLOW_CALL_THRESHOLD_MS": "100",  # 0.1 seconds
        }
    )

    server = create_server(settings)

    with (
        patch("mcp.server.fastmcp.FastMCP.call_tool", side_effect=ValueError("fail")),
        patch("time.monotonic", side_effect=[0.0, 0.15]),
        caplog.at_level(logging.WARNING, logger="mcpg.server"),
    ):
        mcpg_logger = logging.getLogger("mcpg")
        old_propagate = mcpg_logger.propagate
        mcpg_logger.propagate = True
        try:
            with pytest.raises(ValueError, match="fail"):
                await server.call_tool("my_tool", {})

            warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == "mcpg.server"]
            assert len(warnings) == 1
            assert "Slow tool call: my_tool took 0.150s" in warnings[0].message
        finally:
            mcpg_logger.propagate = old_propagate
