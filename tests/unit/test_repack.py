"""Tests for the PG 19 in-server REPACK coverage module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver

from mcpg.repack import (
    RepackError,
    RepackResult,
    RepackStatus,
    get_repack_status,
    repack_table,
)


def _version_route(num: int, ver: str) -> dict[str, list[dict[str, object]]]:
    """Helper — wires the server-version probe to a specific version."""
    return {"current_setting('server_version_num')": [{"ver_num": num, "ver": ver}]}


# --- get_repack_status -----------------------------------------------------


async def test_status_available_on_pg19() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    status = await get_repack_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, RepackStatus)
    assert status.available is True
    assert status.server_version_num == 190001
    assert "REPACK" in status.detail


async def test_status_unavailable_on_pg18_with_pg_repack_fallback_hint() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    status = await get_repack_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    # The diagnostic must point the agent at the pg_repack fallback.
    assert "pg_repack" in status.detail


async def test_status_handles_missing_version_row() -> None:
    driver = FakeRoutingDriver({})
    status = await get_repack_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.server_version_num == 0


async def test_status_never_raises_on_driver_failure() -> None:
    """Gemini review on PR #129: get_repack_status must satisfy its
    'never raises' contract even when the version probe itself fails.
    """
    driver = FakeDriver(fail=True)
    status = await get_repack_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.server_version_num == 0
    # The diagnostic must point the agent at the pg_repack fallback even
    # when the failure mode is a driver-level error rather than an
    # old-PG-version answer.
    assert "pg_repack" in status.detail


# --- repack_table ----------------------------------------------------------


def _pg19_database() -> FakeDatabase:
    """Wire a FakeDatabase whose driver reports PG 19."""
    driver = FakeDriver()
    # The version probe goes through driver.execute_query — FakeDriver
    # returns fixed rows from its ``_rows`` slot.
    driver._rows = [{"ver_num": 190001, "ver": "19beta1"}]  # type: ignore[attr-defined]
    return FakeDatabase(driver)


def _pg18_database() -> FakeDatabase:
    driver = FakeDriver()
    driver._rows = [{"ver_num": 180003, "ver": "18.3"}]  # type: ignore[attr-defined]
    return FakeDatabase(driver)


async def test_repack_table_emits_concurrent_ddl_by_default() -> None:
    db = _pg19_database()
    result = await repack_table(db, schema="public", table="orders")  # type: ignore[arg-type]
    assert result == RepackResult(
        schema="public",
        table="orders",
        concurrently=True,
        repack_sql='REPACK "public"."orders" CONCURRENTLY',
    )
    assert db.unmanaged == ['REPACK "public"."orders" CONCURRENTLY']


async def test_repack_table_emits_blocking_ddl_when_concurrently_false() -> None:
    db = _pg19_database()
    result = await repack_table(  # type: ignore[arg-type]
        db, schema="public", table="orders", concurrently=False
    )
    assert result.concurrently is False
    assert result.repack_sql == 'REPACK "public"."orders"'
    assert db.unmanaged == ['REPACK "public"."orders"']


async def test_repack_table_quotes_embedded_quotes_in_identifier() -> None:
    db = _pg19_database()
    result = await repack_table(  # type: ignore[arg-type]
        db, schema='weird"schema', table='odd"table'
    )
    # Embedded quotes must be doubled per Postgres identifier rules.
    assert '"weird""schema"."odd""table"' in result.repack_sql


async def test_repack_table_rejects_empty_identifier() -> None:
    db = _pg19_database()
    with pytest.raises(RepackError, match="invalid identifier"):
        await repack_table(db, schema="public", table="")  # type: ignore[arg-type]


async def test_repack_table_rejects_null_byte_in_identifier() -> None:
    db = _pg19_database()
    with pytest.raises(RepackError, match="invalid identifier"):
        await repack_table(db, schema="public", table="bad\x00name")  # type: ignore[arg-type]


async def test_repack_table_rejects_pg18_with_pg_repack_hint() -> None:
    db = _pg18_database()
    with pytest.raises(RepackError, match="pg_repack"):
        await repack_table(db, schema="public", table="orders")  # type: ignore[arg-type]
    # No DDL should have been dispatched.
    assert db.unmanaged == []


async def test_repack_table_wraps_unmanaged_failure_as_repack_error() -> None:
    """Gemini review on PR #129: a lock-timeout / permission / syntax
    failure from run_unmanaged must surface as RepackError, not a raw
    driver exception — consistent with turboquant / pg_search modules
    which already wrap unmanaged-call failures.
    """
    db = _pg19_database()
    db.unmanaged_fail = True
    with pytest.raises(RepackError, match="REPACK execution failed"):
        await repack_table(db, schema="public", table="orders")  # type: ignore[arg-type]


# --- Dataclass shape -------------------------------------------------------


def test_dataclass_shapes() -> None:
    status = RepackStatus(available=True, server_version_num=190001, server_version="19beta1", detail="ok")
    assert status.available is True
    result = RepackResult(
        schema="public", table="orders", concurrently=True, repack_sql='REPACK "public"."orders" CONCURRENTLY'
    )
    assert result.repack_sql.endswith("CONCURRENTLY")
