"""Tests for the TimescaleDB wrapper module."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.timescaledb import (
    ChunkListResult,
    HypertableListResult,
    TimescaleError,
    TimescaleWriteResult,
    _check_identifier,
    _check_interval,
    add_compression_policy,
    add_retention_policy,
    create_hypertable,
    list_chunks,
    list_hypertables,
)


def test_check_identifier_rejects_unsafe_names() -> None:
    _check_identifier("widget", "table")
    with pytest.raises(TimescaleError, match="invalid table"):
        _check_identifier('w"; DROP', "table")


def test_check_interval_accepts_common_timescaledb_intervals() -> None:
    _check_interval("7 days")
    _check_interval("1 hour")
    _check_interval("30 minutes")
    _check_interval("12 months")
    _check_interval("1 year")
    _check_interval("500 milliseconds")


def test_check_interval_rejects_freeform_input() -> None:
    with pytest.raises(TimescaleError, match="invalid interval"):
        _check_interval("'; DROP TABLE x; --")
    with pytest.raises(TimescaleError, match="invalid interval"):
        _check_interval("7days")  # missing space
    with pytest.raises(TimescaleError, match="invalid interval"):
        _check_interval("seven days")  # not a number


async def test_list_hypertables_reports_unavailable_without_timescaledb() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await list_hypertables(driver)  # type: ignore[arg-type]

    assert isinstance(result, HypertableListResult)
    assert result.available is False
    assert result.hypertables == []


async def test_list_hypertables_returns_typed_results_when_installed() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "timescaledb_information.hypertables": [
                {
                    "hypertable_schema": "public",
                    "hypertable_name": "metrics",
                    "num_dimensions": 1,
                    "num_chunks": 12,
                    "compression_enabled": True,
                    "total_size_bytes": 1_048_576,
                }
            ],
        }
    )

    result = await list_hypertables(driver)  # type: ignore[arg-type]

    assert result.available is True
    assert len(result.hypertables) == 1
    h = result.hypertables[0]
    assert h.schema == "public" and h.name == "metrics"
    assert h.num_chunks == 12 and h.compression_enabled is True


async def test_list_chunks_reports_unavailable_without_timescaledb() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await list_chunks(driver, "public", "metrics")  # type: ignore[arg-type]

    assert isinstance(result, ChunkListResult)
    assert result.available is False
    assert result.chunks == []


async def test_list_chunks_rejects_unsafe_identifiers() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(TimescaleError, match="invalid"):
        await list_chunks(driver, 'app"; DROP', "metrics")  # type: ignore[arg-type]


async def test_create_hypertable_reports_unavailable_without_timescaledb() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await create_hypertable(driver, "public", "metrics", "time")  # type: ignore[arg-type]

    assert isinstance(result, TimescaleWriteResult)
    assert result.available is False
    assert result.function == "create_hypertable"


async def test_create_hypertable_validates_inputs() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(TimescaleError, match="invalid table"):
        await create_hypertable(driver, "public", 'm"; DROP', "time")  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="invalid time_column"):
        await create_hypertable(driver, "public", "metrics", 'tcol"; DROP')  # type: ignore[arg-type]
    with pytest.raises(TimescaleError, match="invalid interval"):
        await create_hypertable(
            driver,  # type: ignore[arg-type]
            "public",
            "metrics",
            "time",
            chunk_time_interval="hax",
        )


async def test_create_hypertable_calls_timescale_function_when_extension_present() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "create_hypertable": [{"result": "(metrics, public)"}],
        }
    )

    result = await create_hypertable(driver, "public", "metrics", "ts")  # type: ignore[arg-type]

    assert result.available is True
    # The SQL got issued — fake driver records every call.
    create_call = next(call for call in driver.calls if "create_hypertable" in call[0])
    # Identifiers are double-quoted inside the SQL string literal so a
    # mixed-case relation name survives the regclass cast unchanged.
    assert '"public"."metrics"' in create_call[0]
    assert "INTERVAL '7 days'" in create_call[0]
    assert "if_not_exists => TRUE" in create_call[0]


async def test_add_compression_policy_validates_inputs() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(TimescaleError, match="invalid interval"):
        await add_compression_policy(
            driver,  # type: ignore[arg-type]
            "public",
            "metrics",
            compress_after="forever",
        )


async def test_add_compression_policy_issues_alter_and_policy_call() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "add_compression_policy": [{"job_id": 1001}],
        }
    )

    result = await add_compression_policy(driver, "public", "metrics", compress_after="14 days")  # type: ignore[arg-type]

    assert result.available is True
    assert "job_id=1001" in result.details
    alter_call = next(call for call in driver.calls if "ALTER TABLE" in call[0])
    assert "timescaledb.compress" in alter_call[0]
    policy_call = next(call for call in driver.calls if "add_compression_policy" in call[0])
    assert "INTERVAL '14 days'" in policy_call[0]


async def test_add_retention_policy_validates_inputs_and_issues_policy_call() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(TimescaleError, match="invalid interval"):
        await add_retention_policy(
            driver,  # type: ignore[arg-type]
            "public",
            "metrics",
            drop_after="forever",
        )

    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "add_retention_policy": [{"job_id": 2002}],
        }
    )

    result = await add_retention_policy(driver, "public", "metrics", drop_after="90 days")  # type: ignore[arg-type]

    assert result.available is True
    assert "job_id=2002" in result.details
    call = next(call for call in driver.calls if "add_retention_policy" in call[0])
    assert "INTERVAL '90 days'" in call[0]
