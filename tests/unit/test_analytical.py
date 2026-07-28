"""Tests for the analytical-query path (roadmap: long-running reads).

Covers the deterministic parts — the timeout clamp, settings validation, and
that the tool is registered iff enabled. The query execution itself runs on a
live PostgreSQL (an isolated pool) and is not unit-tested, like the other
DB-touching paths.
"""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver

from mcpg.analytical import AnalyticalRunner
from mcpg.config import ConfigError, load_settings
from mcpg.query import QueryError, QueryTimeoutError, _is_timeout_exc
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


# --- structured timeout detection (replaces the old message string-match) ---


def test_query_timeout_error_is_query_error() -> None:
    # A QueryTimeoutError must still be caught by existing `except QueryError`
    # handlers (e.g. run_select_parallel) — it only *narrows* the signal.
    assert issubclass(QueryTimeoutError, QueryError)


def test_is_timeout_exc_asyncio_cap_direct() -> None:
    # run_select_tuned surfaces the asyncio.wait_for TimeoutError directly.
    assert _is_timeout_exc(TimeoutError("timed out")) is True


def test_is_timeout_exc_asyncio_cap_wrapped() -> None:
    # SafeSqlDriver re-wraps the asyncio TimeoutError in a ValueError but
    # chains the original as __cause__ — we detect it via the cause, not text.
    wrapped = ValueError("Query execution timed out after 30 seconds")
    wrapped.__cause__ = TimeoutError()
    assert _is_timeout_exc(wrapped) is True


def test_is_timeout_exc_pg_statement_timeout() -> None:
    # Server-side statement_timeout — psycopg raises SQLSTATE 57014, whose
    # message ("canceling statement due to statement timeout") never contains
    # "timed out", so the old string-match missed it.
    class _CanceledError(Exception):
        sqlstate = "57014"

    assert _is_timeout_exc(_CanceledError("canceling statement due to statement timeout")) is True


def test_is_timeout_exc_ignores_non_timeout() -> None:
    assert _is_timeout_exc(ValueError("syntax error at or near")) is False

    class _SyntaxStateError(Exception):
        sqlstate = "42601"

    assert _is_timeout_exc(_SyntaxStateError("boom")) is False


# --- registration aligns with runner presence (injected-database case) ---


async def test_tool_absent_with_injected_database_and_no_runner() -> None:
    # Enabled + an injected database but no runner: create_server builds no
    # real pool (it would hang on the fake DSN), so the tool must NOT be
    # advertised — otherwise it would error on every call.
    server = create_server(
        _settings(MCPG_ENABLE_ANALYTICAL_QUERIES="true"),
        database=FakeDatabase(FakeDriver()),  # type: ignore[arg-type]
    )
    names = {t.name for t in await server.list_tools()}
    assert "run_analytical_query" not in names


async def test_tool_present_with_injected_runner() -> None:
    # A test that wants a functional tool injects its own runner; then it is
    # registered even though the main database is a fake.
    settings = _settings(MCPG_ENABLE_ANALYTICAL_QUERIES="true")
    server = create_server(
        settings,
        database=FakeDatabase(FakeDriver()),  # type: ignore[arg-type]
        analytical_runner=AnalyticalRunner(settings),
    )
    names = {t.name for t in await server.list_tools()}
    assert "run_analytical_query" in names
