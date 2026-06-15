"""Tests for mcpg_rag.* partitioning retrofit (PR-5)."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg.rag_telemetry import (
    RagTelemetryError,
    RagTelemetryMigrationResult,
    _resolve_rag_telemetry_settings,
    migrate_rag_telemetry_to_partitioned,
)


def _rerank_table_exists_route() -> dict[str, list[dict[str, Any]]]:
    """A single route that matches the pg_class probe for any of the
    rag tables — since FakeRoutingDriver matches by substring, we
    return present=1 for any "FROM pg_class" lookup."""
    return {
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace": [{"present": 1}],
    }


def _native_existing_routes() -> dict[str, list[dict[str, Any]]]:
    """Both tables exist, neither is partitioned, no extensions
    installed. Data range probes return a Q2 2026 window with
    100 / 50 rows respectively."""
    from datetime import UTC, datetime

    routes = _rerank_table_exists_route()
    routes["FROM pg_partitioned_table"] = []
    routes["FROM pg_extension WHERE extname"] = []
    routes["timescaledb_information.hypertables"] = []
    # min/max/count probes — the FakeRoutingDriver matches by
    # substring, so the rerank events query and efficiency
    # observations query both share the same min(...) AS lo prefix
    # — we hand back the same row to both. Tests that need them
    # distinguished can override.
    routes["SELECT min("] = [
        {
            "lo": datetime(2026, 3, 1, tzinfo=UTC),
            "hi": datetime(2026, 6, 1, tzinfo=UTC),
            "n": 100,
        }
    ]
    return routes


def _already_partitioned_routes() -> dict[str, list[dict[str, Any]]]:
    routes = _rerank_table_exists_route()
    # The pg_partitioned_table probe returns a row → tables are
    # already partitioned.
    routes["FROM pg_partitioned_table"] = [{"present": 1}]
    routes["FROM pg_extension WHERE extname"] = []
    return routes


def _missing_tables_routes() -> dict[str, list[dict[str, Any]]]:
    """Neither table exists — telemetry was never set up."""
    return {"FROM pg_extension WHERE extname": []}


def test_resolve_settings_defaults() -> None:
    backend, retention, chunk, compress, rls, reader = _resolve_rag_telemetry_settings({})
    assert backend is None
    # Unlike audit_events, retention is ON by default (no HMAC anchor).
    assert retention == 90
    assert chunk == "1 day"
    assert compress == "7 days"
    assert rls is True
    assert reader is None


def test_resolve_settings_reads_env() -> None:
    backend, retention, chunk, compress, rls, reader = _resolve_rag_telemetry_settings(
        {
            "MCPG_RAG_TELEMETRY_BACKEND": "native",
            "MCPG_RAG_TELEMETRY_RETENTION_DAYS": "30",
            "MCPG_RAG_TELEMETRY_CHUNK_INTERVAL": "12 hours",
            "MCPG_RAG_TELEMETRY_COMPRESS_AFTER": "3 days",
            "MCPG_RAG_TELEMETRY_RLS": "false",
            "MCPG_RAG_TELEMETRY_READER_ROLE": "rag_ro",
        }
    )
    assert backend == "native"
    assert retention == 30
    assert chunk == "12 hours"
    assert compress == "3 days"
    assert rls is False
    assert reader == "rag_ro"


def test_resolve_settings_rejects_chunk_injection() -> None:
    with pytest.raises(RagTelemetryError, match="CHUNK_INTERVAL"):
        _resolve_rag_telemetry_settings({"MCPG_RAG_TELEMETRY_CHUNK_INTERVAL": "1 day'); DROP"})


def test_resolve_settings_rejects_bad_reader_role() -> None:
    with pytest.raises(RagTelemetryError, match="reader role"):
        _resolve_rag_telemetry_settings({"MCPG_RAG_TELEMETRY_READER_ROLE": "ro; DROP"})


def test_resolve_settings_rejects_zero_retention() -> None:
    with pytest.raises(RagTelemetryError, match="RETENTION_DAYS"):
        _resolve_rag_telemetry_settings({"MCPG_RAG_TELEMETRY_RETENTION_DAYS": "0"})


async def test_migrate_no_op_when_neither_table_exists() -> None:
    """Telemetry never set up → migration returns cleanly without
    issuing any rename/copy DDL."""
    driver = FakeRoutingDriver(_missing_tables_routes())
    result = await migrate_rag_telemetry_to_partitioned(driver, env={})  # type: ignore[arg-type]
    queries = " | ".join(call[0] for call in driver.calls)
    assert result.migrated_rerank is False
    assert result.migrated_efficiency is False
    assert "RENAME TO" not in queries
    assert "INSERT INTO mcpg_rag" not in queries


async def test_migrate_native_path_runs_for_both_tables() -> None:
    """Both rerank_events and efficiency_observations get the
    rename-create-insert-drop dance."""
    driver = FakeRoutingDriver(_native_existing_routes())
    result = await migrate_rag_telemetry_to_partitioned(driver, env={})  # type: ignore[arg-type]

    queries = " | ".join(call[0] for call in driver.calls)
    assert result.migrated_rerank is True
    assert result.migrated_efficiency is True
    assert result.backend == "native"
    # rerank dance
    assert "RENAME TO rerank_events_migration_legacy" in queries
    assert "PARTITION BY RANGE (occurred_at)" in queries
    assert "INSERT INTO mcpg_rag.rerank_events" in queries
    assert "DROP TABLE mcpg_rag.rerank_events_migration_legacy" in queries
    # efficiency dance
    assert "RENAME TO efficiency_observations_migration_legacy" in queries
    assert "PARTITION BY RANGE (observed_at)" in queries
    assert "INSERT INTO mcpg_rag.efficiency_observations" in queries
    assert "DROP TABLE mcpg_rag.efficiency_observations_migration_legacy" in queries
    # RLS defaults on for both.
    assert queries.count("ENABLE ROW LEVEL SECURITY") == 2


async def test_migrate_skips_when_already_partitioned() -> None:
    driver = FakeRoutingDriver(_already_partitioned_routes())
    result = await migrate_rag_telemetry_to_partitioned(driver, env={})  # type: ignore[arg-type]
    queries = " | ".join(call[0] for call in driver.calls)
    assert result.migrated_rerank is False
    assert result.migrated_efficiency is False
    assert "RENAME TO" not in queries


async def test_migrate_with_reader_role_grants_select_on_both_tables() -> None:
    driver = FakeRoutingDriver(_native_existing_routes())
    await migrate_rag_telemetry_to_partitioned(
        driver,  # type: ignore[arg-type]
        env={"MCPG_RAG_TELEMETRY_READER_ROLE": "rag_ro"},
    )
    queries = " | ".join(call[0] for call in driver.calls)
    assert "CREATE POLICY rerank_events_reader_select" in queries
    assert "CREATE POLICY efficiency_observations_reader_select" in queries
    assert "GRANT SELECT ON mcpg_rag.rerank_events TO rag_ro" in queries
    assert "GRANT SELECT ON mcpg_rag.efficiency_observations TO rag_ro" in queries


async def test_migrate_rls_can_be_disabled() -> None:
    driver = FakeRoutingDriver(_native_existing_routes())
    result = await migrate_rag_telemetry_to_partitioned(
        driver,  # type: ignore[arg-type]
        env={"MCPG_RAG_TELEMETRY_RLS": "false"},
    )
    queries = " | ".join(call[0] for call in driver.calls)
    assert result.rls_enabled is False
    assert "ENABLE ROW LEVEL SECURITY" not in queries


async def test_migrate_native_applies_lz4_on_pg_14_plus() -> None:
    """Version probe returns ≥ 140000 → LZ4 ALTERs fire on JSONB
    columns of both tables."""
    routes = _native_existing_routes()
    routes["current_setting('server_version_num')"] = [{"ver": 160004}]
    driver = FakeRoutingDriver(routes)

    result = await migrate_rag_telemetry_to_partitioned(driver, env={})  # type: ignore[arg-type]
    queries = " | ".join(call[0] for call in driver.calls)
    assert result.compression_enabled is True
    assert "ALTER COLUMN extra SET COMPRESSION lz4" in queries
    assert "ALTER COLUMN rerank_lift_curve SET COMPRESSION lz4" in queries


async def test_migrate_native_skips_lz4_on_pg_13() -> None:
    """Version probe < 140000 → no LZ4 ALTERs. The DROP legacy step
    still runs (transaction integrity preserved)."""
    routes = _native_existing_routes()
    routes["current_setting('server_version_num')"] = [{"ver": 130012}]
    driver = FakeRoutingDriver(routes)

    result = await migrate_rag_telemetry_to_partitioned(driver, env={})  # type: ignore[arg-type]
    queries = " | ".join(call[0] for call in driver.calls)
    assert result.compression_enabled is False
    assert "SET COMPRESSION lz4" not in queries
    assert "DROP TABLE mcpg_rag.rerank_events_migration_legacy" in queries


async def test_migration_only_one_table_when_other_missing() -> None:
    """Operator only set up rerank_events, never efficiency_observations
    — migration handles each independently."""
    from datetime import UTC, datetime

    routes: dict[str, list[dict[str, Any]]] = {}
    # Only the rerank_events probe is wired to return a row;
    # efficiency observations probe is absent (empty routes match
    # → returns []).
    routes["FROM pg_partitioned_table"] = []
    routes["FROM pg_extension WHERE extname"] = []
    # pg_class probe — we need a more specific match. Since
    # FakeRoutingDriver only matches one substring, we use the
    # full table probe to gate which table is "present". Use
    # the param-aware fake instead.
    from _fakes import FakeParamRoutingDriver

    routes_param: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]] = {
        ("FROM pg_class", ("mcpg_rag", "rerank_events")): [{"present": 1}],
        ("FROM pg_class", ("mcpg_rag", "efficiency_observations")): [],
        ("FROM pg_partitioned_table", None): [],
        ("FROM pg_extension", None): [],
        ("SELECT min(", None): [
            {"lo": datetime(2026, 3, 1, tzinfo=UTC), "hi": datetime(2026, 6, 1, tzinfo=UTC), "n": 100}
        ],
    }
    driver = FakeParamRoutingDriver(routes_param)
    result = await migrate_rag_telemetry_to_partitioned(driver, env={})  # type: ignore[arg-type]
    assert result.migrated_rerank is True
    assert result.migrated_efficiency is False


async def test_native_migration_sent_as_single_write_call() -> None:
    """The migration DDL must be batched into ONE execute_query call so
    the ACCESS EXCLUSIVE lock is held across all statements. If the
    LOCK / RENAME / CREATE / INSERT / DROP each ran as separate
    execute_query calls, the driver's per-call COMMIT (sql_driver.py
    L249/260) would release the lock immediately and let concurrent
    writers race the rename dance (gemini critical review PR #110).
    """
    driver = FakeRoutingDriver(_native_existing_routes())
    await migrate_rag_telemetry_to_partitioned(driver, env={})  # type: ignore[arg-type]

    write_calls = [c for c in driver.calls if c[2] is False]
    migration_write_calls = [c for c in write_calls if "LOCK TABLE" in c[0] or "INSERT INTO mcpg_rag" in c[0]]
    # Exactly two write calls — one batch per table (rerank +
    # efficiency). Each batch carries LOCK + RENAME + CREATE + INSERT
    # + DROP in a single execute_query.
    assert len(migration_write_calls) == 2
    for call in migration_write_calls:
        sql = call[0]
        assert "LOCK TABLE" in sql
        assert "INSERT INTO mcpg_rag" in sql
        assert "DROP TABLE" in sql


def test_migration_result_shape() -> None:
    result = RagTelemetryMigrationResult(
        migrated_rerank=True,
        migrated_efficiency=False,
        backend="native",
        rerank_rows_copied=100,
        efficiency_rows_copied=0,
        compression_enabled=True,
        retention_days=90,
        rls_enabled=True,
        reader_role=None,
        setup_sql=("CREATE TABLE …",),
    )
    assert result.migrated_rerank is True
    assert result.migrated_efficiency is False
    assert result.backend == "native"
