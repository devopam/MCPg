"""Unit tests for the tier-A/R additions in mcpg.timescaledb and the tier-B mcpg.timescaledb_admin."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpg import timescaledb, timescaledb_admin
from mcpg.timescaledb import TimescaleError

Rows = list[dict[str, Any]]


def _row(cells: dict[str, Any]) -> MagicMock:
    m = MagicMock()
    m.cells = cells
    return m


class FakeDriver:
    """Answers execute_query by matching SQL substrings; records every statement."""

    def __init__(self, answers: dict[str, Rows | Callable[[], Rows]] | None = None, *, installed: bool = True) -> None:
        self.answers = answers or {}
        self.installed = installed
        self.sql: list[str] = []

    async def execute_query(self, query: str, params: Any = None, *, force_readonly: bool = False) -> list[Any]:
        self.sql.append(query)
        if "FROM pg_extension" in query:
            return [_row({"present": 1})] if self.installed else []
        for needle, answer in self.answers.items():
            if needle in query:
                rows = answer() if callable(answer) else answer
                return [_row(r) for r in rows]
        return []


def _db(run_unmanaged: Any = None) -> MagicMock:
    db = MagicMock()
    db.run_unmanaged = run_unmanaged or AsyncMock()
    return db


CHUNK = {"FROM timescaledb_information.chunks WHERE chunk_schema": [{"is_compressed": False}]}
CAGG = {"FROM timescaledb_information.continuous_aggregates WHERE view_schema": [{"x": 1}]}
HYPER = {"FROM timescaledb_information.hypertables WHERE hypertable_schema": [{"x": 1}]}
JOB = {"FROM timescaledb_information.jobs WHERE job_id": [{"application_name": "Retention Policy [1000]"}]}


async def _confirm(fn: Any, *args: Any, **kwargs: Any) -> Any:
    plan = await fn(*args, dry_run=True, **kwargs)
    assert plan.status == "dry_run" and plan.confirmation_token
    blocked = await fn(*args, confirm=True, confirmation_token="wrong", **kwargs)
    assert blocked.status == "confirmation_required"
    assert "does not match" in blocked.details
    return await fn(*args, confirm=True, confirmation_token=plan.confirmation_token, **kwargs)


def test_helpers() -> None:
    assert timescaledb_admin._timestamp_literal(None, "x") == "NULL"
    assert (
        timescaledb_admin._timestamp_literal("2026-01-02 03:04:05+00", "x") == "'2026-01-02 03:04:05+00'::timestamptz"
    )
    with pytest.raises(TimescaleError, match="invalid window"):
        timescaledb_admin._timestamp_literal("now(); DROP", "window")
    assert timescaledb_admin._interval_literal("3 days", "k") == "INTERVAL '3 days'"
    with pytest.raises(TimescaleError, match="invalid interval"):
        timescaledb_admin._interval_literal("3 days; DROP", "k")
    assert timescaledb_admin._token("a", ["x", "y"]) == timescaledb_admin._token("a", ["y", "x"])
    assert timescaledb_admin._token("a", ["x"]) != timescaledb_admin._token("b", ["x"])
    timescaledb_admin.require_untenanted(None, ())
    with pytest.raises(TimescaleError, match="MCPG_DEFAULT_ROLE"):
        timescaledb_admin.require_untenanted("app", ())
    with pytest.raises(TimescaleError, match="MCPG_DEFAULT_ROLE"):
        timescaledb_admin.require_untenanted(None, ("a",))


@pytest.mark.asyncio
async def test_unavailable_when_extension_missing() -> None:
    d = FakeDriver(installed=False)
    db = _db()
    for result in [
        await timescaledb_admin.drop_chunks(d, "s", "t", older_than="1 day"),  # type: ignore[arg-type]
        await timescaledb_admin.compress_chunk(d, "s", "c"),  # type: ignore[arg-type]
        await timescaledb_admin.decompress_chunk(d, "s", "c"),  # type: ignore[arg-type]
        await timescaledb_admin.reorder_chunk(db, d, "s", "c", "s", "i"),  # type: ignore[arg-type]
        await timescaledb_admin.merge_chunks(db, d, "s", ["a", "b"]),  # type: ignore[arg-type]
        await timescaledb_admin.refresh_continuous_aggregate(db, d, "s", "v"),  # type: ignore[arg-type]
        await timescaledb_admin.run_job(d, 1000),  # type: ignore[arg-type]
        await timescaledb_admin.alter_job(d, 1000, scheduled=False),  # type: ignore[arg-type]
        await timescaledb_admin.delete_job(d, 1000),  # type: ignore[arg-type]
    ]:
        assert result.available is False and result.status == "unavailable"
    assert (await timescaledb.get_telemetry_report(d)).available is False  # type: ignore[arg-type]
    assert (await timescaledb.list_jobs(d)).available is False  # type: ignore[arg-type]
    assert (await timescaledb.list_continuous_aggregates(d)).available is False  # type: ignore[arg-type]
    assert (await timescaledb.get_hypertable_stats(d, "s", "t")).available is False  # type: ignore[arg-type]
    assert (await timescaledb.add_dimension(d, "s", "t", "c", number_partitions=2)).available is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_drop_chunks_flow() -> None:
    chunks = [{"chunk": "_timescaledb_internal._hyper_1_1_chunk"}, {"chunk": "_timescaledb_internal._hyper_1_2_chunk"}]
    d = FakeDriver(
        {**HYPER, "show_chunks": chunks, "SELECT drop_chunks": [{"drop_chunks": c["chunk"]} for c in chunks]}
    )
    result = await _confirm(timescaledb_admin.drop_chunks, d, "s", "t", older_than="30 days")
    assert result.status == "applied" and len(result.affected) == 2
    assert any(q.startswith("SELECT drop_chunks('\"s\".\"t\"', older_than => INTERVAL '30 days')") for q in d.sql)


@pytest.mark.asyncio
async def test_drop_chunks_validation_and_noop() -> None:
    d = FakeDriver({**HYPER})
    with pytest.raises(TimescaleError, match="at least one"):
        await timescaledb_admin.drop_chunks(d, "s", "t")  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="cannot be combined"):
        await timescaledb_admin.drop_chunks(d, "s", "t", older_than="1 day", created_after="1 day")  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="invalid interval"):
        await timescaledb_admin.drop_chunks(d, "s", "t", older_than="1 day'); DROP")  # type: ignore[arg-type]
    noop = await timescaledb_admin.drop_chunks(d, "s", "t", created_before="1 day")  # type: ignore[arg-type]
    assert noop.status == "no_op"
    with pytest.raises(TimescaleError, match="not a hypertable"):
        await timescaledb_admin.drop_chunks(FakeDriver(), "s", "t", older_than="1 day")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_drop_chunks_token_covers_chunk_set() -> None:
    state = {"rows": [{"chunk": "c1"}]}
    d = FakeDriver({**HYPER, "show_chunks": lambda: list(state["rows"])})
    plan = await timescaledb_admin.drop_chunks(d, "s", "t", older_than="1 day", dry_run=True)  # type: ignore[arg-type]
    state["rows"] = [{"chunk": "c1"}, {"chunk": "c2"}]  # a new chunk aged out meanwhile
    stale = await timescaledb_admin.drop_chunks(  # type: ignore[arg-type]
        d, "s", "t", older_than="1 day", confirm=True, confirmation_token=plan.confirmation_token
    )
    assert stale.status == "confirmation_required"
    assert not any(q.startswith("SELECT drop_chunks") for q in d.sql)


@pytest.mark.asyncio
async def test_compress_and_decompress() -> None:
    d = FakeDriver({"FROM timescaledb_information.chunks WHERE chunk_schema": [{"is_compressed": False}]})
    assert (await timescaledb_admin.decompress_chunk(d, "s", "c")).status == "no_op"  # type: ignore[arg-type]
    done = await _confirm(timescaledb_admin.compress_chunk, d, "s", "c")
    assert done.status == "applied"
    d2 = FakeDriver({"FROM timescaledb_information.chunks WHERE chunk_schema": [{"is_compressed": True}]})
    assert (await timescaledb_admin.compress_chunk(d2, "s", "c")).status == "no_op"  # type: ignore[arg-type]
    assert (await _confirm(timescaledb_admin.decompress_chunk, d2, "s", "c")).status == "applied"
    with pytest.raises(TimescaleError, match="not a TimescaleDB chunk"):
        await timescaledb_admin.compress_chunk(FakeDriver(), "s", "c")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_autocommit_paths_use_run_unmanaged() -> None:
    db = _db()
    d = FakeDriver({**CHUNK, **CAGG})
    reorder = await _confirm(timescaledb_admin.reorder_chunk, db, d, "s", "c", "s", "idx")
    assert reorder.status == "applied"
    assert "reorder_chunk(" in db.run_unmanaged.call_args.args[0]

    db = _db()
    refreshed = await _confirm(
        timescaledb_admin.refresh_continuous_aggregate,
        db,
        d,
        "s",
        "v",
        window_start="2026-01-01",
        window_end="2026-02-01",
        force=True,
    )
    assert refreshed.status == "applied"
    sql = db.run_unmanaged.call_args.args[0]
    assert sql.startswith("CALL refresh_continuous_aggregate(") and "force => TRUE" in sql

    db = _db()
    merged = await _confirm(timescaledb_admin.merge_chunks, db, d, "s", ["a", "b"], concurrently=True)
    assert merged.status == "applied" and "merge_chunks_concurrently" in db.run_unmanaged.call_args.args[0]

    db = _db()
    merged = await _confirm(timescaledb_admin.merge_chunks, db, d, "s", ["a", "b"])
    assert merged.status == "applied"
    db.run_unmanaged.assert_not_called()  # plain merge_chunks runs in a transaction via the driver
    assert any(q.startswith("CALL merge_chunks(") for q in d.sql)


@pytest.mark.asyncio
async def test_autocommit_timeout_and_validation() -> None:
    async def hang(_sql: str) -> None:
        await asyncio.sleep(5)

    d = FakeDriver({**CAGG})
    db = _db(hang)
    plan = await timescaledb_admin.refresh_continuous_aggregate(db, d, "s", "v", dry_run=True)  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="timed out"):
        await timescaledb_admin.refresh_continuous_aggregate(  # type: ignore[arg-type]
            db, d, "s", "v", confirm=True, confirmation_token=plan.confirmation_token, timeout_s=0.05
        )
    with pytest.raises(TimescaleError, match="not a continuous aggregate"):
        await timescaledb_admin.refresh_continuous_aggregate(db, FakeDriver(), "s", "v")  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="at least two"):
        await timescaledb_admin.merge_chunks(db, d, "s", ["a"])  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="distinct"):
        await timescaledb_admin.merge_chunks(db, d, "s", ["a", "a"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_job_tools() -> None:
    d = FakeDriver({**JOB})
    assert (await _confirm(timescaledb_admin.run_job, d, 1000)).status == "applied"
    assert (await _confirm(timescaledb_admin.delete_job, d, 1000)).status == "applied"
    altered = await _confirm(
        timescaledb_admin.alter_job, d, 1000, schedule_interval="2 hours", max_retries=3, scheduled=False
    )
    assert altered.status == "applied"
    assert any("alter_job(1000, schedule_interval => INTERVAL '2 hours'" in q for q in d.sql)
    for bad in (1, 999):
        with pytest.raises(TimescaleError, match="system job"):
            await timescaledb_admin.run_job(d, bad)  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="not found"):
        await timescaledb_admin.delete_job(FakeDriver(), 1000)  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="at least one"):
        await timescaledb_admin.alter_job(d, 1000)  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="max_retries"):
        await timescaledb_admin.alter_job(d, 1000, max_retries=-5)  # type: ignore[arg-type]
    full = await timescaledb_admin.alter_job(  # type: ignore[arg-type]
        d, 1000, max_runtime="1 hours", retry_period="5 minutes", dry_run=True
    )
    assert "max_runtime => INTERVAL '1 hours'" in full.planned_action


@pytest.mark.asyncio
async def test_reads() -> None:
    d = FakeDriver(
        {
            "FROM timescaledb_information.hypertables h": [{"num_chunks": 10, "compressed_chunks": 2}],
            "hypertable_detailed_size": [{"table_bytes": 1, "index_bytes": 2, "toast_bytes": 3, "total_bytes": 6}],
            "approximate_row_count": [{"n": 99}],
            "to_regproc": [{"fn": "hypertable_columnstore_stats"}],
            "hypertable_columnstore_stats(": [
                {"before_compression_total_bytes": 100, "after_compression_total_bytes": 10}
            ],
            "FROM timescaledb_information.jobs j": [
                {
                    "job_id": 1000,
                    "application_name": "Retention Policy [1000]",
                    "procedure": "_timescaledb_functions.policy_retention",
                    "schedule_interval": "1 day",
                    "scheduled": True,
                    "hypertable_schema": "s",
                    "hypertable_name": "t",
                    "next_start": None,
                    "last_run_status": None,
                    "last_run_started_at": None,
                    "total_runs": None,
                    "total_failures": 0,
                }
            ],
            "FROM timescaledb_information.continuous_aggregates": [
                {
                    "view_schema": "s",
                    "view_name": "v",
                    "hypertable_schema": "s",
                    "hypertable_name": "t",
                    "materialized_only": True,
                    "compression_enabled": False,
                }
            ],
            "get_telemetry_report": [{"report": '{"db_uuid": "x"}'}],
        }
    )
    stats = await timescaledb.get_hypertable_stats(d, "s", "t")  # type: ignore[arg-type]
    assert (stats.total_chunks, stats.compressed_chunks, stats.approximate_row_count) == (10, 2, 99)
    assert (stats.total_bytes, stats.before_compression_bytes, stats.after_compression_bytes) == (6, 100, 10)
    jobs = await timescaledb.list_jobs(d)  # type: ignore[arg-type]
    assert jobs.jobs[0].job_id == 1000 and jobs.jobs[0].total_runs is None and jobs.jobs[0].total_failures == 0
    aggs = await timescaledb.list_continuous_aggregates(d)  # type: ignore[arg-type]
    assert aggs.continuous_aggregates[0].view_name == "v"
    report = await timescaledb.get_telemetry_report(d)  # type: ignore[arg-type]
    assert report.report == {"db_uuid": "x"}
    with pytest.raises(TimescaleError, match="not a hypertable"):
        await timescaledb.get_hypertable_stats(FakeDriver(), "s", "t")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tier_a_sql() -> None:
    d = FakeDriver({"add_dimension": [{"dimension_id": 7, "created": True}], "AS job_id": [{"job_id": 42}]})
    r = await timescaledb.add_dimension(d, "s", "t", "dev", number_partitions=4)  # type: ignore[arg-type]
    assert r.details == "dimension_id=7"
    await timescaledb.add_dimension(d, "s", "t", "ts", chunk_time_interval="1 day", if_not_exists=False)  # type: ignore[arg-type]
    for kwargs in ({}, {"number_partitions": 2, "chunk_time_interval": "1 day"}):
        with pytest.raises(TimescaleError, match="exactly one"):
            await timescaledb.add_dimension(d, "s", "t", "dev", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match=">= 1"):
        await timescaledb.add_dimension(d, "s", "t", "dev", number_partitions=0)  # type: ignore[arg-type]
    await timescaledb.set_chunk_time_interval(d, "s", "t", "2 days")  # type: ignore[arg-type]
    cp = await timescaledb.add_continuous_aggregate_policy(  # type: ignore[arg-type]
        d, "s", "v", start_offset="3 days", end_offset=None, schedule_interval="1 hour"
    )
    assert cp.details == "job_id=42"
    rp = await timescaledb.add_reorder_policy(d, "s", "t", "idx")  # type: ignore[arg-type]
    assert rp.details == "job_id=42"
    for fn in (
        timescaledb.remove_retention_policy,
        timescaledb.remove_compression_policy,
        timescaledb.remove_continuous_aggregate_policy,
        timescaledb.remove_reorder_policy,
    ):
        assert (await fn(d, "s", "t")).available is True  # type: ignore[arg-type]
    joined = "\n".join(d.sql)
    assert "start_offset => INTERVAL '3 days', end_offset => NULL" in joined
    assert 'remove_reorder_policy(\'"s"."t"\', if_exists => TRUE)' in joined
