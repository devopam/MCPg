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
    _events_migrate_timescaledb,
    _resolve_events_settings,
    migrate_audit_events_to_partitioned,
)


class _SelectiveFailDriver:
    """Routes like FakeRoutingDriver, but raises for queries matching any
    of ``fail_substrings`` — used to simulate a TimescaleDB policy call
    (``add_compression_policy`` / ``add_retention_policy``) rejecting on
    an unsupported TSDB edition/version, distinct from every other
    statement in the same migration succeeding."""

    def __init__(self, routes: dict[str, list[dict[str, Any]]], fail_substrings: tuple[str, ...]) -> None:
        self._routing = FakeRoutingDriver(routes)
        self._fail_substrings = fail_substrings
        self.calls: list[Any] = []

    async def execute_query(self, query: str, params: Any = None, *, force_readonly: bool = False) -> Any:
        self.calls.append((query, params, force_readonly))
        if any(s in query for s in self._fail_substrings):
            raise RuntimeError("simulated TimescaleDB policy failure")
        return await self._routing.execute_query(query, params, force_readonly=force_readonly)


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


async def test_migrate_native_applies_lz4_on_pg_14_plus() -> None:
    """When server_version_num >= 140000 the LZ4 ALTER must fire on
    each large text column. We probe up front instead of try/except
    so a pre-14 server doesn't abort the migration transaction
    (gemini critical review, PR #109)."""
    routes = _native_existing_routes()
    # Mock the version probe — PG 16.4 = 160004.
    routes["current_setting('server_version_num')"] = [{"ver": 160004}]
    driver = FakeRoutingDriver(routes)

    result = await migrate_audit_events_to_partitioned(driver, env={})  # type: ignore[arg-type]
    queries = " | ".join(call[0] for call in driver.calls)

    assert result.compression_enabled is True
    assert "ALTER COLUMN arguments SET COMPRESSION lz4" in queries
    assert "ALTER COLUMN result SET COMPRESSION lz4" in queries
    assert "ALTER COLUMN error SET COMPRESSION lz4" in queries


async def test_native_migration_sent_as_single_write_call() -> None:
    """The migration DDL must be batched into ONE execute_query call so
    the ACCESS EXCLUSIVE lock holds across all statements. If the
    LOCK / RENAME / CREATE / INSERT / DROP each ran as separate
    execute_query calls, the driver's per-call COMMIT
    (sql_driver.py:249/260) would release the lock immediately,
    letting concurrent record_audit calls race the rename dance and
    risk row loss / FK violations (symmetric fix to the one applied
    to mcpg.rag_telemetry after the gemini critical review on
    PR #110)."""
    driver = FakeRoutingDriver(_native_existing_routes())
    await migrate_audit_events_to_partitioned(driver, env={})  # type: ignore[arg-type]

    write_calls = [c for c in driver.calls if c[2] is False]
    migration_write_calls = [c for c in write_calls if "LOCK TABLE" in c[0] or "INSERT INTO mcpg_audit.events" in c[0]]
    # Exactly one batched write — the migration itself. RLS apply
    # statements that follow may be separate (independent
    # operations).
    assert len(migration_write_calls) == 1
    sql = migration_write_calls[0][0]
    assert "LOCK TABLE mcpg_audit.events IN ACCESS EXCLUSIVE MODE" in sql
    assert "INSERT INTO mcpg_audit.events" in sql
    assert "DROP TABLE mcpg_audit.events_migration_legacy" in sql


async def test_migrate_native_skips_lz4_on_pg_13() -> None:
    """Server_version_num < 140000 → version probe returns and we
    skip the LZ4 ALTERs entirely, preserving transaction integrity."""
    routes = _native_existing_routes()
    routes["current_setting('server_version_num')"] = [{"ver": 130012}]
    driver = FakeRoutingDriver(routes)

    result = await migrate_audit_events_to_partitioned(driver, env={})  # type: ignore[arg-type]
    queries = " | ".join(call[0] for call in driver.calls)

    assert result.compression_enabled is False
    assert "SET COMPRESSION lz4" not in queries
    # The DROP TABLE legacy step (which follows compression) still
    # runs — transaction integrity preserved.
    assert "DROP TABLE mcpg_audit.events_migration_legacy" in queries


async def test_timescaledb_migrate_logs_debug_when_compression_policy_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected add_compression_policy call is swallowed (best-effort —
    not every TSDB edition supports compression) but now logs at debug."""
    import logging

    driver = _SelectiveFailDriver({}, fail_substrings=("add_compression_policy",))

    root_logger = logging.getLogger("mcpg")
    old_propagate = root_logger.propagate
    root_logger.propagate = True
    try:
        caplog.set_level(logging.DEBUG, logger="mcpg.audit_trail")

        _rows_copied, compression_enabled, _statements = await _events_migrate_timescaledb(
            driver,  # type: ignore[arg-type]
            chunk_interval="7 days",
            compress_after="30 days",
            retention_days=None,
            rls=False,
            reader_role=None,
        )
    finally:
        root_logger.propagate = old_propagate

    assert compression_enabled is False
    matches = [
        r
        for r in caplog.records
        if r.name == "mcpg.audit_trail" and "add_compression_policy" in r.message and r.levelno == logging.DEBUG
    ]
    assert len(matches) == 1
    # exc_info=True must actually attach a traceback, not just the message.
    assert matches[0].exc_info is not None


async def test_timescaledb_migrate_logs_debug_when_retention_policy_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected add_retention_policy call is swallowed (best-effort —
    the operator opted in but the TSDB edition rejected it) but now logs
    at debug."""
    import logging

    driver = _SelectiveFailDriver({}, fail_substrings=("add_retention_policy",))

    root_logger = logging.getLogger("mcpg")
    old_propagate = root_logger.propagate
    root_logger.propagate = True
    try:
        caplog.set_level(logging.DEBUG, logger="mcpg.audit_trail")

        _rows_copied, _compression_enabled, statements = await _events_migrate_timescaledb(
            driver,  # type: ignore[arg-type]
            chunk_interval="7 days",
            compress_after="30 days",
            retention_days=90,
            rls=False,
            reader_role=None,
        )
    finally:
        root_logger.propagate = old_propagate

    assert "add_retention_policy" not in " | ".join(statements)
    matches = [
        r
        for r in caplog.records
        if r.name == "mcpg.audit_trail" and "add_retention_policy" in r.message and r.levelno == logging.DEBUG
    ]
    assert len(matches) == 1
    # exc_info=True must actually attach a traceback, not just the message.
    assert matches[0].exc_info is not None
