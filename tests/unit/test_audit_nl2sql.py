"""Tests for the NL→SQL audit table (Phase 10.3)."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeRoutingDriver

from mcpg.audit_nl2sql import (
    AUDIT_SCHEMA,
    AUDIT_TABLE,
    NL2SQLAuditError,
    _reset_setup_cache,
    detect_backend,
    ensure_nl2sql_audit_table,
    extend_native_partitions,
    list_nl2sql_events,
    record_nl2sql_event,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    _reset_setup_cache()


def _no_extensions_routes() -> dict[str, list[dict[str, Any]]]:
    """The plain-PG case: pg_extension lookups return empty."""
    return {"FROM pg_extension WHERE extname": []}


def _timescaledb_present_routes() -> dict[str, list[dict[str, Any]]]:
    return {"FROM pg_extension WHERE extname": [{"present": 1}]}


async def test_detect_backend_picks_native_when_no_extensions() -> None:
    driver = FakeRoutingDriver(_no_extensions_routes())
    assert await detect_backend(driver) == "native"  # type: ignore[arg-type]


async def test_detect_backend_picks_timescaledb_when_installed() -> None:
    driver = FakeRoutingDriver(_timescaledb_present_routes())
    # Both extension probes return a row, but TimescaleDB has priority.
    assert await detect_backend(driver) == "timescaledb"  # type: ignore[arg-type]


async def test_detect_backend_honours_forced_choice() -> None:
    driver = FakeRoutingDriver(_no_extensions_routes())
    assert await detect_backend(driver, forced="native") == "native"  # type: ignore[arg-type]


async def test_detect_backend_rejects_unknown_forced_name() -> None:
    driver = FakeRoutingDriver(_no_extensions_routes())
    with pytest.raises(NL2SQLAuditError, match="unknown"):
        await detect_backend(driver, forced="cassandra")  # type: ignore[arg-type]


async def test_detect_backend_rejects_forced_timescaledb_when_missing() -> None:
    driver = FakeRoutingDriver(_no_extensions_routes())
    with pytest.raises(NL2SQLAuditError, match="not installed"):
        await detect_backend(driver, forced="timescaledb")  # type: ignore[arg-type]


async def test_ensure_table_native_path_emits_expected_ddl() -> None:
    """The native backend must produce: schema CREATE, partitioned table
    CREATE, index, ±N daily child partitions, RLS enable."""
    driver = FakeRoutingDriver(_no_extensions_routes())
    result = await ensure_nl2sql_audit_table(driver, env={})  # type: ignore[arg-type]

    queries = " | ".join(call[0] for call in driver.calls)
    assert result.backend == "native"
    assert result.schema_created is True
    assert result.table_created is True
    assert result.rls_enabled is True
    # Schema + parent + index + at least one child partition + RLS
    assert "CREATE SCHEMA IF NOT EXISTS mcpg_audit" in queries
    assert "PARTITION BY RANGE (occurred_at)" in queries
    assert "PARTITION OF mcpg_audit.nl2sql_events" in queries
    assert "ENABLE ROW LEVEL SECURITY" in queries
    # Setup SQL is returned so the operator can audit what ran.
    assert any("CREATE SCHEMA" in stmt for stmt in result.setup_sql)


async def test_ensure_table_is_idempotent_per_driver() -> None:
    """Second call on the same driver instance must short-circuit."""
    driver = FakeRoutingDriver(_no_extensions_routes())
    await ensure_nl2sql_audit_table(driver, env={})  # type: ignore[arg-type]
    calls_after_first = len(driver.calls)
    result2 = await ensure_nl2sql_audit_table(driver, env={})  # type: ignore[arg-type]
    assert len(driver.calls) == calls_after_first
    assert result2.table_created is False  # cache hit signals no fresh DDL


async def test_ensure_table_reader_role_grants_select_only() -> None:
    driver = FakeRoutingDriver(_no_extensions_routes())
    result = await ensure_nl2sql_audit_table(
        driver,  # type: ignore[arg-type]
        env={"MCPG_NL2SQL_AUDIT_READER_ROLE": "analytics_ro"},
    )

    queries = " | ".join(call[0] for call in driver.calls)
    assert result.reader_role == "analytics_ro"
    assert "CREATE POLICY nl2sql_events_reader_select" in queries
    assert "FOR SELECT TO analytics_ro" in queries
    assert "GRANT SELECT ON mcpg_audit.nl2sql_events TO analytics_ro" in queries


async def test_ensure_table_rejects_bad_reader_role_identifier() -> None:
    """The reader role lands in DDL verbatim, so it must be a valid
    unquoted identifier. ``"; DROP TABLE"`` is the classic vector."""
    with pytest.raises(NL2SQLAuditError, match="identifier"):
        await ensure_nl2sql_audit_table(
            FakeRoutingDriver(_no_extensions_routes()),  # type: ignore[arg-type]
            env={"MCPG_NL2SQL_AUDIT_READER_ROLE": "ro; DROP TABLE x"},
        )


async def test_ensure_table_rejects_invalid_backend_choice() -> None:
    with pytest.raises(NL2SQLAuditError, match="unknown"):
        await ensure_nl2sql_audit_table(
            FakeRoutingDriver(_no_extensions_routes()),  # type: ignore[arg-type]
            env={"MCPG_NL2SQL_AUDIT_BACKEND": "mongodb"},
        )


async def test_ensure_table_rls_can_be_disabled() -> None:
    driver = FakeRoutingDriver(_no_extensions_routes())
    result = await ensure_nl2sql_audit_table(
        driver,  # type: ignore[arg-type]
        env={"MCPG_NL2SQL_AUDIT_RLS": "false"},
    )
    queries = " | ".join(call[0] for call in driver.calls)
    assert result.rls_enabled is False
    assert "ENABLE ROW LEVEL SECURITY" not in queries


async def test_record_event_redacts_credentials_in_question_and_sql() -> None:
    driver = FakeRoutingDriver(_no_extensions_routes())
    await record_nl2sql_event(
        driver,  # type: ignore[arg-type]
        provider="anthropic",
        model="claude-sonnet-4-6",
        schema_arg="public",
        question="how many rows in postgres://alice:hunter2@db.example.com/x",
        sql_generated="-- nothing sensitive",
        sql_executed=False,
        row_count=None,
        error="connection failed for postgres://alice:hunter2@db.example.com/x",
        duration_ms=42,
        env={},
    )

    insert_calls = [c for c in driver.calls if "INSERT INTO mcpg_audit.nl2sql_events" in c[0]]
    assert len(insert_calls) == 1
    params = insert_calls[0][1]
    assert params is not None
    persisted_question = params[3]
    persisted_error = params[7]
    # obfuscate_password replaces the password with ``****`` in any
    # connection-string-shaped substring; check the literal didn't survive.
    assert "hunter2" not in persisted_question
    assert "hunter2" not in persisted_error


async def test_record_event_passes_through_when_no_secrets() -> None:
    driver = FakeRoutingDriver(_no_extensions_routes())
    await record_nl2sql_event(
        driver,  # type: ignore[arg-type]
        provider="openai",
        model="gpt-4o-mini",
        schema_arg="public",
        question="count active users",
        sql_generated="SELECT count(*) FROM users WHERE active",
        sql_executed=True,
        row_count=42,
        error=None,
        duration_ms=120,
        prompt_tokens=300,
        completion_tokens=25,
        env={},
    )
    insert = next(c for c in driver.calls if "INSERT INTO mcpg_audit.nl2sql_events" in c[0])
    params = insert[1]
    assert params[0] == "openai"
    assert params[3] == "count active users"
    assert params[6] == 42  # row_count
    assert params[9] == 300  # prompt_tokens
    assert params[10] == 25  # completion_tokens


async def test_extend_native_partitions_creates_n_days_ahead() -> None:
    driver = FakeRoutingDriver({})
    created = await extend_native_partitions(driver, days_ahead=3)  # type: ignore[arg-type]
    assert len(created) == 4  # today + 3 forward days
    assert all(name.startswith(f"{AUDIT_TABLE}_p") for name in created)
    # Each day produced one CREATE TABLE IF NOT EXISTS partition stmt.
    assert sum(1 for c in driver.calls if "PARTITION OF mcpg_audit.nl2sql_events" in c[0]) == 4


async def test_extend_native_partitions_rejects_zero() -> None:
    with pytest.raises(NL2SQLAuditError, match="positive integer"):
        await extend_native_partitions(FakeRoutingDriver({}), days_ahead=0)  # type: ignore[arg-type]


async def test_list_nl2sql_events_returns_empty_when_table_missing() -> None:
    """list_ on a fresh DB doesn't blow up — caller sees an empty list."""
    driver = FakeRoutingDriver({})  # nothing matches the pg_class probe
    rows = await list_nl2sql_events(driver, limit=10)  # type: ignore[arg-type]
    assert rows == []


async def test_list_nl2sql_events_round_trips_a_row() -> None:
    """When the table exists, rows come back as typed entries."""
    from datetime import UTC, datetime

    routes = {
        # The exists check returns a present row.
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace": [{"present": 1}],
        # The select returns one canned row.
        "FROM mcpg_audit.nl2sql_events": [
            {
                "id": 1,
                "occurred_at": datetime(2026, 6, 14, tzinfo=UTC),
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "schema_arg": "public",
                "question": "count widgets",
                "sql_generated": "SELECT count(*) FROM widget",
                "sql_executed": True,
                "row_count": 7,
                "error": None,
                "duration_ms": 80,
            }
        ],
    }
    rows = await list_nl2sql_events(FakeRoutingDriver(routes), limit=10)  # type: ignore[arg-type]
    assert len(rows) == 1
    assert rows[0].provider == "anthropic"
    assert rows[0].row_count == 7
    assert rows[0].sql_executed is True


async def test_list_nl2sql_events_rejects_non_positive_limit() -> None:
    with pytest.raises(NL2SQLAuditError, match="positive integer"):
        await list_nl2sql_events(FakeRoutingDriver({}), limit=0)  # type: ignore[arg-type]


def test_audit_constants_use_expected_names() -> None:
    assert AUDIT_SCHEMA == "mcpg_audit"
    assert AUDIT_TABLE == "nl2sql_events"


async def test_resolve_settings_rejects_chunk_interval_with_injection() -> None:
    """Interval strings flow into DDL as ``INTERVAL '<value>'`` and
    can't be parameterised — anything that doesn't match
    ``<digits> <unit>`` is refused before reaching the driver
    (gemini critical review, PR #107)."""
    with pytest.raises(NL2SQLAuditError, match="CHUNK_INTERVAL"):
        await ensure_nl2sql_audit_table(
            FakeRoutingDriver(_no_extensions_routes()),  # type: ignore[arg-type]
            env={"MCPG_NL2SQL_AUDIT_CHUNK_INTERVAL": "1 day'); DROP TABLE x; --"},
        )


async def test_resolve_settings_rejects_compress_after_with_injection() -> None:
    with pytest.raises(NL2SQLAuditError, match="COMPRESS_AFTER"):
        await ensure_nl2sql_audit_table(
            FakeRoutingDriver(_no_extensions_routes()),  # type: ignore[arg-type]
            env={"MCPG_NL2SQL_AUDIT_COMPRESS_AFTER": "7 days; SELECT 1"},
        )


async def test_resolve_settings_accepts_legitimate_interval_shapes() -> None:
    """Bare digit + unit (singular or plural) passes; the DDL doesn't
    care about whitespace or case."""
    for value in ("1 day", "7 DAYS", "30 minutes", "12 hours", "2 weeks"):
        # No raise — ensure_nl2sql_audit_table runs through the validator
        # on its way to the driver.
        _reset_setup_cache()
        await ensure_nl2sql_audit_table(
            FakeRoutingDriver(_no_extensions_routes()),  # type: ignore[arg-type]
            env={"MCPG_NL2SQL_AUDIT_CHUNK_INTERVAL": value, "MCPG_NL2SQL_AUDIT_COMPRESS_AFTER": value},
        )


async def test_cached_path_returns_real_backend_not_default() -> None:
    """Second call on the same driver must report whichever backend
    was chosen on the first call — the previous version recomputed via
    env and silently defaulted to ``'native'`` when the operator's
    forced choice was TimescaleDB but env was empty afterwards
    (sourcery review, PR #107)."""
    # Mock TimescaleDB as available so first call selects it.
    driver = FakeRoutingDriver(_timescaledb_present_routes())
    first = await ensure_nl2sql_audit_table(driver, env={})  # type: ignore[arg-type]
    assert first.backend == "timescaledb"
    # Same driver, no env at all on the second call.
    second = await ensure_nl2sql_audit_table(driver, env={})  # type: ignore[arg-type]
    assert second.backend == "timescaledb"
    assert second.table_created is False
