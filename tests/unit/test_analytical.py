"""Tests for the analytical-query path (roadmap: long-running reads).

Covers the deterministic parts — the timeout clamp, settings validation, and
that the tool is registered iff enabled. The query execution itself runs on a
live PostgreSQL (an isolated pool) and is not unit-tested, like the other
DB-touching paths.
"""

from __future__ import annotations

import pytest

from mcpg.analytical import AnalyticalRunner
from mcpg.config import ConfigError, load_settings
from mcpg.server import create_server

_DSN = "postgresql://u:p@localhost:5432/db"


def _settings(**env: str):
    return load_settings({"MCPG_DATABASE_URL": _DSN, **env})


def test_timeout_clamp_default_min_max() -> None:
    runner = AnalyticalRunner(
        _settings(
            MCPG_ANALYTICAL_TIMEOUT_MS="120000",
            MCPG_ANALYTICAL_MAX_TIMEOUT_MS="600000",
        )
    )
    assert runner._resolve_timeout_s(None) == 120.0  # default when omitted
    assert runner._resolve_timeout_s(30000) == 30.0  # per-call honored
    assert runner._resolve_timeout_s(999_999_999) == 600.0  # clamped to max
    assert runner._resolve_timeout_s(0) == 0.001  # floored to >= 1 ms
    assert runner._resolve_timeout_s(-5) == 0.001  # negative floored too


def test_config_defaults() -> None:
    s = _settings()
    assert s.enable_analytical_queries is True
    assert s.analytical_timeout_ms == 120000
    assert s.analytical_max_timeout_ms == 600000
    assert s.analytical_max_concurrency == 2


def test_config_rejects_default_over_max() -> None:
    with pytest.raises(ConfigError, match="must not exceed"):
        _settings(MCPG_ANALYTICAL_TIMEOUT_MS="700000")  # > default max 600000


@pytest.mark.parametrize("var", ["MCPG_ANALYTICAL_TIMEOUT_MS", "MCPG_ANALYTICAL_MAX_CONCURRENCY"])
def test_config_rejects_non_positive(var: str) -> None:
    with pytest.raises(ConfigError, match="positive integer"):
        _settings(**{var: "0"})


async def _tool_names(settings) -> set[str]:
    server = create_server(settings)
    return {t.name for t in await server.list_tools()}


async def test_tool_registered_when_enabled() -> None:
    names = await _tool_names(_settings(MCPG_ENABLE_ANALYTICAL_QUERIES="true"))
    assert "run_analytical_query" in names


async def test_tool_absent_when_disabled() -> None:
    names = await _tool_names(_settings(MCPG_ENABLE_ANALYTICAL_QUERIES="false"))
    assert "run_analytical_query" not in names
