"""Tests for mcpg_audit.events partitioning retrofit (PR-4)."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg.audit_trail import (
    AUDIT_SCHEMA,
    AUDIT_TABLE,
    AuditTrailError,
    EventsAuditMigrationResult,
    _resolve_events_settings,
    migrate_audit_events_to_partitioned,
)


def _table_exists_routes() -> dict[str, list[dict[str, Any]]]:
    """The events table exists (pg_class probe returns a row)."""
    return {
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace": [{"present": 1}],
    }


def _native_existing_routes() -> dict[str, list[dict[str, Any]]]:
    """Events exists, is not partitioned, no extensions installed."""
    from datetime import UTC, datetime

    routes = _table_exists_routes()
    # pg_partitioned_table probe — empty so it's a plain heap table.
    routes["FROM pg_partitioned_table"] = []
    # timescaledb extension probe — empty.
    routes["FROM pg_extension WHERE extname"] = []
    # data range probe.
    routes["SELECT min(occurred_at) AS lo"] = [
        {
            "lo": datetime(2026, 3, 1, tzinfo=UTC),
            "hi": datetime(2026, 6, 1, tzinfo=UTC),
            "n": 1234,
        }
    ]
    return routes


def _already_partitioned_routes() -> dict[str, list[dict[str, Any]]]:
    routes = _table_exists_routes()
    routes["FROM pg_partitioned_table"] = [{"present": 1}]
    routes["FROM pg_extension WHERE extname"] = []
    return routes


def test_resolve_events_settings_defaults_are_safe() -> None:
    """Retention is intentionally None by default — HMAC chain anchors
    on the oldest event."""
    backend, retention, chunk, compress, rls, reader = _resolve_events_settings({})
    assert backend is None
    assert retention is None
    assert chunk == "1 day"
    assert compress == "7 days"
    assert rls is True
    assert reader is None


def test_resolve_events_settings_reads_env_knobs() -> None:
    backend, retention, chunk, compress, rls, reader = _resolve_events_settings(
        {
            "MCPG_AUDIT_EVENTS_BACKEND": "native",
            "MCPG_AUDIT_EVENTS_RETENTION_DAYS": "180",
            "MCPG_AUDIT_EVENTS_CHUNK_INTERVAL": "2 hours",
            "MCPG_AUDIT_EVENTS_COMPRESS_AFTER": "3 days",
            "MCPG_AUDIT_EVENTS_RLS": "false",
            "MCPG_AUDIT_EVENTS_READER_ROLE": "audit_ro",
        }
    )
    assert backend == "native"
    assert retention == 180
    assert chunk == "2 hours"
    assert compress == "3 days"
    assert rls is False
    assert reader == "audit_ro"


def test_resolve_events_settings_rejects_chunk_injection() -> None:
    """Interval strings reach DDL as INTERVAL '<value>' — they must
    match <digits> <unit>."""
    with pytest.raises(AuditTrailError, match="CHUNK_INTERVAL"):
        _resolve_events_settings({"MCPG_AUDIT_EVENTS_CHUNK_INTERVAL": "1 day'); DROP TABLE x"})


def test_resolve_events_settings_rejects_bad_reader_role() -> None:
    with pytest.raises(AuditTrailError, match="reader role"):
        _resolve_events_settings({"MCPG_AUDIT_EVENTS_READER_ROLE": "ro; DROP TABLE x"})


def test_resolve_events_settings_rejects_zero_retention() -> None:
    with pytest.raises(AuditTrailError, match="RETENTION_DAYS"):
        _resolve_events_settings({"MCPG_AUDIT_EVENTS_RETENTION_DAYS": "0"})


async def test_migrate_raises_when_events_table_missing() -> None:
    """The migration assumes ensure_audit_table has already run; we
    don't auto-create the table here because the operator may want to
    inspect the data first."""
    # All routes empty — pg_class probe returns nothing, so the
    # table is treated as missing.
    driver = FakeRoutingDriver({})
    with pytest.raises(AuditTrailError, match="does not exist"):
        await migrate_audit_events_to_partitioned(driver, env={})  # type: ignore[arg-type]


async def test_migrate_native_path_runs_rename_dance() -> None:
    """The native backend must emit: LOCK, sequence detach, RENAME,
    CREATE … PARTITION BY RANGE, sequence reattach, monthly+daily
    partitions, INSERT … SELECT, DROP legacy."""
    driver = FakeRoutingDriver(_native_existing_routes())
    result = await migrate_audit_events_to_partitioned(driver, env={})  # type: ignore[arg-type]

    queries = " | ".join(call[0] for call in driver.calls)
    assert result.migrated is True
    assert result.backend == "native"
    assert result.rows_copied == 1234
    assert "LOCK TABLE mcpg_audit.events IN ACCESS EXCLUSIVE MODE" in queries
    assert "ALTER SEQUENCE mcpg_audit.events_id_seq OWNED BY NONE" in queries
    assert "RENAME TO events_migration_legacy" in queries
    assert "PARTITION BY RANGE (occurred_at)" in queries
    assert "ALTER SEQUENCE mcpg_audit.events_id_seq OWNED BY mcpg_audit.events.id" in queries
    assert "INSERT INTO mcpg_audit.events" in queries
    assert "FROM mcpg_audit.events_migration_legacy" in queries
    assert "DROP TABLE mcpg_audit.events_migration_legacy" in queries
    # RLS defaults on.
    assert "ENABLE ROW LEVEL SECURITY" in queries


async def test_migrate_skips_when_already_partitioned() -> None:
    """Re-running on a partitioned events table is a near-no-op —
    no rename, no copy. RLS may still be (re-)applied since it's
    idempotent."""
    driver = FakeRoutingDriver(_already_partitioned_routes())
    result = await migrate_audit_events_to_partitioned(driver, env={})  # type: ignore[arg-type]

    queries = " | ".join(call[0] for call in driver.calls)
    assert result.migrated is False
    assert result.rows_copied == 0
    assert "RENAME TO events_migration_legacy" not in queries
    assert "INSERT INTO mcpg_audit.events" not in queries


async def test_migrate_native_with_reader_role_grants_select() -> None:
    routes = _native_existing_routes()
    driver = FakeRoutingDriver(routes)
    await migrate_audit_events_to_partitioned(
        driver,  # type: ignore[arg-type]
        env={"MCPG_AUDIT_EVENTS_READER_ROLE": "audit_ro"},
    )
    queries = " | ".join(call[0] for call in driver.calls)
    assert "CREATE POLICY events_reader_select" in queries
    assert "FOR SELECT TO audit_ro" in queries
    assert "GRANT SELECT ON mcpg_audit.events TO audit_ro" in queries


async def test_migrate_rls_can_be_disabled() -> None:
    routes = _native_existing_routes()
    driver = FakeRoutingDriver(routes)
    result = await migrate_audit_events_to_partitioned(
        driver,  # type: ignore[arg-type]
        env={"MCPG_AUDIT_EVENTS_RLS": "false"},
    )
    queries = " | ".join(call[0] for call in driver.calls)
    assert result.rls_enabled is False
    assert "ENABLE ROW LEVEL SECURITY" not in queries


async def test_migrate_empty_table_skips_historical_partitions() -> None:
    """No data → no monthly historical partitions; only the trailing
    daily window is pre-created so writes have somewhere to land."""
    routes = _table_exists_routes()
    routes["FROM pg_partitioned_table"] = []
    routes["FROM pg_extension WHERE extname"] = []
    routes["SELECT min(occurred_at) AS lo"] = [{"lo": None, "hi": None, "n": 0}]
    driver = FakeRoutingDriver(routes)

    result = await migrate_audit_events_to_partitioned(driver, env={})  # type: ignore[arg-type]

    queries = " | ".join(call[0] for call in driver.calls)
    assert result.migrated is True
    assert result.rows_copied == 0
    # Daily partition for today exists.
    from datetime import UTC, datetime

    today_suffix = datetime.now(UTC).strftime("%Y%m%d")
    assert f"events_p{today_suffix}" in queries


async def test_migrate_result_carries_setup_sql_for_audit() -> None:
    """Operators can inspect the executed DDL via result.setup_sql."""
    driver = FakeRoutingDriver(_native_existing_routes())
    result = await migrate_audit_events_to_partitioned(driver, env={})  # type: ignore[arg-type]
    assert any("CREATE TABLE mcpg_audit.events" in stmt for stmt in result.setup_sql)
    assert any("INSERT INTO mcpg_audit.events" in stmt for stmt in result.setup_sql)


def test_audit_events_migration_result_shape() -> None:
    """Sanity-check the dataclass — operators inspect this in scripts."""
    result = EventsAuditMigrationResult(
        migrated=True,
        backend="native",
        rows_copied=100,
        compression_enabled=True,
        retention_days=None,
        rls_enabled=True,
        reader_role=None,
        setup_sql=("CREATE TABLE …",),
    )
    assert result.migrated is True
    assert result.backend == "native"
    assert result.retention_days is None


def test_audit_constants_use_expected_names() -> None:
    assert AUDIT_SCHEMA == "mcpg_audit"
    assert AUDIT_TABLE == "events"
