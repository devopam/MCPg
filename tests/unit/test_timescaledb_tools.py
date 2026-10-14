"""Unit tests for the tool wrappers in mcpg.timescaledb_tools (no MCP server needed)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpg import timescaledb_tools
from mcpg.timescaledb import TimescaleError


class _Recorder:
    """Stands in for MCPServer: records every ``@server.tool(...)`` function by name."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *, name: str, description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        assert description

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn

        return deco


class _Cells(dict[str, Any]):
    def __missing__(self, key: str) -> None:
        return None


class _Driver:
    """Every query returns one generic row that satisfies all the catalog look-ups."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    async def execute_query(self, query: str, params: Any = None, *, force_readonly: bool = False) -> list[Any]:
        self.sql.append(query)
        cells = {
            "present": 1,
            "is_compressed": False,
            "application_name": "Retention Policy [1000]",
            "chunk": "_timescaledb_internal._hyper_1_1_chunk",
            "dimension_id": 1,
            "job_id": 1000,
            "num_chunks": 1,
            "compressed_chunks": 0,
            "report": "{}",
            "fn": "hypertable_compression_stats",
            "n": 1,
        }
        row = MagicMock()
        row.cells = _Cells(cells)
        return [row]


def _ctx(*, default_role: str | None = None) -> tuple[MagicMock, _Driver]:
    driver = _Driver()
    app = MagicMock()
    app.database.driver.return_value = driver
    app.database.run_unmanaged = AsyncMock()
    app.cache.clear = AsyncMock()
    app.settings.statement_timeout_ms = 5000
    app.settings.default_role = default_role
    app.settings.allowed_roles = ()
    ctx = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx, driver


def _register(fn: Callable[[Any], None]) -> dict[str, Callable[..., Any]]:
    rec = _Recorder()
    fn(rec)
    return rec.tools


@pytest.mark.asyncio
async def test_read_tools() -> None:
    tools = _register(timescaledb_tools.register_timescaledb_reads)
    assert set(tools) == {
        "get_hypertable_stats",
        "list_timescale_jobs",
        "list_continuous_aggregates",
        "get_timescaledb_telemetry_report",
    }
    ctx, _ = _ctx()
    assert (await tools["get_hypertable_stats"](ctx, "s", "t")).available
    assert (await tools["list_timescale_jobs"](ctx)).available
    assert (await tools["list_continuous_aggregates"](ctx)).available
    assert (await tools["get_timescaledb_telemetry_report"](ctx)).available


@pytest.mark.asyncio
async def test_ddl_tools_clear_the_cache() -> None:
    tools = _register(timescaledb_tools.register_timescaledb_ddl)
    assert len(tools) == 8
    ctx, driver = _ctx()
    calls = [
        ("add_dimension", ("s", "t", "dev"), {"number_partitions": 2}),
        ("set_chunk_time_interval", ("s", "t", "1 day"), {}),
        ("add_continuous_aggregate_policy", ("s", "v", "1 hour"), {"start_offset": "3 days"}),
        ("add_reorder_policy", ("s", "t", "idx"), {}),
        ("remove_retention_policy", ("s", "t"), {}),
        ("remove_compression_policy", ("s", "t"), {}),
        ("remove_continuous_aggregate_policy", ("s", "v"), {}),
        ("remove_reorder_policy", ("s", "t"), {}),
    ]
    for name, args, kwargs in calls:
        assert (await tools[name](ctx, *args, **kwargs)).available, name
    assert ctx.request_context.lifespan_context.cache.clear.await_count == len(calls)
    assert driver.sql


@pytest.mark.asyncio
async def test_admin_tools_confirmation_flow() -> None:
    tools = _register(timescaledb_tools.register_timescaledb_admin)
    assert len(tools) == 9
    ctx, _ = _ctx()
    app = ctx.request_context.lifespan_context

    plan = await tools["compress_chunk"](ctx, "_timescaledb_internal", "c", dry_run=True)
    assert plan.status == "dry_run"
    done = await tools["decompress_chunk"](ctx, "_timescaledb_internal", "c", dry_run=True)
    assert done.status == "no_op"  # the fake chunk is not compressed
    assert (await tools["run_job"](ctx, 1000, dry_run=True)).status == "dry_run"
    assert (await tools["alter_job"](ctx, 1000, scheduled=False, dry_run=True)).status == "dry_run"
    assert (await tools["delete_job"](ctx, 1000, dry_run=True)).status == "dry_run"
    assert (await tools["drop_chunks"](ctx, "s", "t", older_than="1 day", dry_run=True)).status == "dry_run"
    assert (await tools["merge_chunks"](ctx, "s", ["a", "b"], dry_run=True)).status == "dry_run"
    app.cache.clear.assert_not_called()  # nothing applied yet

    token = (await tools["run_job"](ctx, 1000, dry_run=True)).confirmation_token
    applied = await tools["run_job"](ctx, 1000, confirmation_token=token, confirm=True)
    assert applied.status == "applied"
    app.cache.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_autocommit_tools_use_the_timeout_and_refuse_tenancy() -> None:
    tools = _register(timescaledb_tools.register_timescaledb_admin)
    ctx, _ = _ctx()
    app = ctx.request_context.lifespan_context
    for name, args in (
        ("reorder_chunk", ("_timescaledb_internal", "c", "s", "idx")),
        ("refresh_continuous_aggregate", ("s", "v")),
    ):
        plan = await tools[name](ctx, *args, dry_run=True)
        assert plan.status == "dry_run", name
        result = await tools[name](ctx, *args, confirm=True, confirmation_token=plan.confirmation_token)
        assert result.status == "applied", name
    plan = await tools["merge_chunks"](ctx, "s", ["a", "b"], concurrently=True, dry_run=True)
    token = plan.confirmation_token
    await tools["merge_chunks"](ctx, "s", ["a", "b"], concurrently=True, confirm=True, confirmation_token=token)
    assert app.database.run_unmanaged.await_count == 3

    tenant_ctx, _ = _ctx(default_role="app")
    with pytest.raises(TimescaleError, match="MCPG_DEFAULT_ROLE"):
        await tools["reorder_chunk"](tenant_ctx, "s", "c", "s", "idx", dry_run=True)
    with pytest.raises(TimescaleError, match="MCPG_DEFAULT_ROLE"):
        await tools["refresh_continuous_aggregate"](tenant_ctx, "s", "v", dry_run=True)
    with pytest.raises(TimescaleError, match="MCPG_DEFAULT_ROLE"):
        await tools["merge_chunks"](tenant_ctx, "s", ["a", "b"], concurrently=True, dry_run=True)
    # the transactional paths still work under tenancy (the driver applies the role)
    assert (await tools["merge_chunks"](tenant_ctx, "s", ["a", "b"], dry_run=True)).status == "dry_run"
