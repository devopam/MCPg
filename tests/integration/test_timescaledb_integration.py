"""Integration tests for the TimescaleDB tool family (needs a TimescaleDB server).

Skipped unless ``MCPG_TEST_DATABASE_URL`` points at a database where the
``timescaledb`` extension can be created (the CI "TimescaleDB" job uses the
``timescale/timescaledb`` image). Verified against TimescaleDB 2.30.2.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from mcpg import timescaledb, timescaledb_admin
from mcpg.database import Database
from mcpg.sql import SafeSqlDriver, SqlDriver

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def ts(connected_database: Database) -> AsyncIterator[tuple[Database, SqlDriver, str]]:
    """A hypertable ``<schema>.m`` with 10 daily chunks and a continuous aggregate ``<schema>.cagg``."""
    db = connected_database
    try:
        await db.run_unmanaged("CREATE EXTENSION IF NOT EXISTS timescaledb")
    except Exception as exc:
        pytest.skip(f"timescaledb extension not available: {exc}")
    schema = "ts_it_" + uuid.uuid4().hex[:8]
    await db.run_unmanaged(f'CREATE SCHEMA "{schema}"')
    await db.run_unmanaged(f'CREATE TABLE "{schema}".m (ts timestamptz NOT NULL, dev int, v double precision)')
    await db.run_unmanaged(f"SELECT create_hypertable('\"{schema}\".m', 'ts', chunk_time_interval => interval '1 day')")
    await db.run_unmanaged(
        f'INSERT INTO "{schema}".m SELECT t, 1, random() '
        "FROM generate_series(now() - interval '9 days', now(), interval '1 hour') t"
    )
    await db.run_unmanaged(f'CREATE INDEX ON "{schema}".m (dev, ts DESC)')
    await db.run_unmanaged(
        f'CREATE MATERIALIZED VIEW "{schema}".cagg WITH (timescaledb.continuous) AS '
        f"SELECT time_bucket('1 day', ts) AS b, avg(v) AS a FROM \"{schema}\".m GROUP BY 1 WITH NO DATA"
    )
    try:
        yield db, db.driver(), schema
    finally:
        await db.run_unmanaged(f'DROP SCHEMA "{schema}" CASCADE')


async def _chunk_names(driver: SqlDriver, schema: str) -> list[str]:
    result = await timescaledb.list_chunks(driver, schema, "m")
    return [c.chunk_name for c in result.chunks]


async def test_reads(ts: tuple[Database, SqlDriver, str]) -> None:
    _, driver, schema = ts
    stats = await timescaledb.get_hypertable_stats(driver, schema, "m")
    assert stats.available and stats.total_chunks and stats.total_chunks >= 9
    assert stats.total_bytes and stats.total_bytes > 0
    assert (await timescaledb.list_jobs(driver)).available
    aggs = await timescaledb.list_continuous_aggregates(driver)
    assert any(a.view_schema == schema and a.view_name == "cagg" for a in aggs.continuous_aggregates)
    report = await timescaledb.get_telemetry_report(driver)
    assert report.available and report.report
    with pytest.raises(timescaledb.TimescaleError, match="not a hypertable"):
        await timescaledb.get_hypertable_stats(driver, schema, "nope")


async def test_readonly_functions_pass_the_safe_driver(ts: tuple[Database, SqlDriver, str]) -> None:
    _, driver, schema = ts
    safe = SafeSqlDriver(driver)
    rows = await safe.execute_query(
        f"SELECT time_bucket('1 day', ts) AS b, first(v, ts) AS f, last(v, ts) AS l FROM \"{schema}\".m GROUP BY 1"
    )
    assert rows
    sized = await safe.execute_query(f"SELECT hypertable_size('\"{schema}\".m') AS s")
    assert sized and int(sized[0].cells["s"]) > 0
    with pytest.raises(ValueError, match="Error validating query"):
        await safe.execute_query(f"SELECT drop_chunks('\"{schema}\".m', older_than => interval '1 day')")


async def test_tier_a_ddl(ts: tuple[Database, SqlDriver, str]) -> None:
    _, driver, schema = ts
    r = await timescaledb.add_dimension(driver, schema, "m", "dev", number_partitions=2)
    assert r.available and "dimension_id=" in r.details
    with pytest.raises(timescaledb.TimescaleError, match="exactly one"):
        await timescaledb.add_dimension(driver, schema, "m", "dev")
    await timescaledb.set_chunk_time_interval(driver, schema, "m", "2 days")

    cp = await timescaledb.add_continuous_aggregate_policy(
        driver, schema, "cagg", start_offset="3 days", end_offset="1 hour", schedule_interval="1 hour"
    )
    assert cp.details.startswith("job_id=")
    await timescaledb.remove_continuous_aggregate_policy(driver, schema, "cagg")

    rp = await timescaledb.add_reorder_policy(driver, schema, "m", "m_dev_ts_idx")
    assert rp.details.startswith("job_id=")
    await timescaledb.remove_reorder_policy(driver, schema, "m")

    await timescaledb.add_retention_policy(driver, schema, "m", drop_after="90 days")
    await timescaledb.remove_retention_policy(driver, schema, "m")
    await timescaledb.add_compression_policy(driver, schema, "m", compress_after="30 days")
    await timescaledb.remove_compression_policy(driver, schema, "m")


async def test_drop_chunks_confirmation_flow(ts: tuple[Database, SqlDriver, str]) -> None:
    _, driver, schema = ts
    before = await _chunk_names(driver, schema)

    plan = await timescaledb_admin.drop_chunks(driver, schema, "m", older_than="5 days", dry_run=True)
    assert plan.status == "dry_run" and plan.confirmation_token and plan.affected
    assert await _chunk_names(driver, schema) == before

    blocked = await timescaledb_admin.drop_chunks(driver, schema, "m", older_than="5 days", confirm=True)
    assert blocked.status == "confirmation_required"
    wrong = await timescaledb_admin.drop_chunks(
        driver, schema, "m", older_than="6 days", confirm=True, confirmation_token=plan.confirmation_token
    )
    assert wrong.status == "confirmation_required"
    assert await _chunk_names(driver, schema) == before

    done = await timescaledb_admin.drop_chunks(
        driver, schema, "m", older_than="5 days", confirm=True, confirmation_token=plan.confirmation_token
    )
    assert done.status == "applied"
    after = await _chunk_names(driver, schema)
    assert len(after) == len(before) - len(plan.affected)

    nothing = await timescaledb_admin.drop_chunks(driver, schema, "m", older_than="400 days")
    assert nothing.status == "no_op"
    with pytest.raises(timescaledb.TimescaleError, match="at least one"):
        await timescaledb_admin.drop_chunks(driver, schema, "m")
    with pytest.raises(timescaledb.TimescaleError, match="cannot be combined"):
        await timescaledb_admin.drop_chunks(driver, schema, "m", older_than="1 day", created_before="1 day")


async def _confirmed(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    plan = await fn(*args, dry_run=True, **kwargs)
    assert plan.status == "dry_run", plan
    return await fn(*args, confirm=True, confirmation_token=plan.confirmation_token, **kwargs)


async def test_compress_decompress_and_merge(ts: tuple[Database, SqlDriver, str]) -> None:
    db, driver, schema = ts
    await db.run_unmanaged(f'ALTER TABLE "{schema}".m SET (timescaledb.compress)')
    chunks = sorted(await _chunk_names(driver, schema))
    c = chunks[0]

    assert (await timescaledb_admin.decompress_chunk(driver, "_timescaledb_internal", c)).status == "no_op"
    done = await _confirmed(timescaledb_admin.compress_chunk, driver, "_timescaledb_internal", c)
    assert done.status == "applied"
    assert (await timescaledb_admin.compress_chunk(driver, "_timescaledb_internal", c)).status == "no_op"
    back = await _confirmed(timescaledb_admin.decompress_chunk, driver, "_timescaledb_internal", c)
    assert back.status == "applied"

    with pytest.raises(timescaledb.TimescaleError, match="not a TimescaleDB chunk"):
        await timescaledb_admin.compress_chunk(driver, "public", "pg_class")

    pair = chunks[3:5]
    merged = await _confirmed(timescaledb_admin.merge_chunks, db, driver, "_timescaledb_internal", pair)
    assert merged.status == "applied"
    assert len(await _chunk_names(driver, schema)) == len(chunks) - 1

    with pytest.raises(timescaledb.TimescaleError, match="at least two"):
        await timescaledb_admin.merge_chunks(db, driver, "_timescaledb_internal", [chunks[0]])


async def test_autocommit_procedures(ts: tuple[Database, SqlDriver, str]) -> None:
    db, driver, schema = ts
    chunk = sorted(await _chunk_names(driver, schema))[2]
    reordered = await _confirmed(
        timescaledb_admin.reorder_chunk, db, driver, "_timescaledb_internal", chunk, schema, "m_dev_ts_idx"
    )
    assert reordered.status == "applied"

    refreshed = await _confirmed(
        timescaledb_admin.refresh_continuous_aggregate,
        db,
        driver,
        schema,
        "cagg",
        window_start="2000-01-01",
        window_end="2100-01-01",
        force=True,
    )
    assert refreshed.status == "applied"
    rows = await driver.execute_query(f'SELECT count(*) AS n FROM "{schema}".cagg', force_readonly=True)
    assert rows and int(rows[0].cells["n"]) > 0

    with pytest.raises(timescaledb.TimescaleError, match="not a continuous aggregate"):
        await timescaledb_admin.refresh_continuous_aggregate(db, driver, schema, "m")
    with pytest.raises(timescaledb.TimescaleError, match="window_start"):
        await timescaledb_admin.refresh_continuous_aggregate(db, driver, schema, "cagg", window_start="1; DROP")


async def test_job_control(ts: tuple[Database, SqlDriver, str]) -> None:
    _, driver, schema = ts
    added = await timescaledb.add_retention_policy(driver, schema, "m", drop_after="400 days")
    job_id = int(added.details.split("=")[1])

    altered = await _confirmed(timescaledb_admin.alter_job, driver, job_id, scheduled=False, max_retries=3)
    assert altered.status == "applied"
    jobs = (await timescaledb.list_jobs(driver)).jobs
    mine = next(j for j in jobs if j.job_id == job_id)
    assert mine.scheduled is False

    ran = await _confirmed(timescaledb_admin.run_job, driver, job_id)
    assert ran.status == "applied"

    deleted = await _confirmed(timescaledb_admin.delete_job, driver, job_id)
    assert deleted.status == "applied"
    assert all(j.job_id != job_id for j in (await timescaledb.list_jobs(driver)).jobs)

    with pytest.raises(timescaledb.TimescaleError, match="system job"):
        await timescaledb_admin.run_job(driver, 1)
    with pytest.raises(timescaledb.TimescaleError, match="not found"):
        await timescaledb_admin.delete_job(driver, 999999)
    with pytest.raises(timescaledb.TimescaleError, match="at least one"):
        await timescaledb_admin.alter_job(driver, 1000)
