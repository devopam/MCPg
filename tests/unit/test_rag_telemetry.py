"""Tests for the RAG telemetry surface — Phase C."""

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.rag_telemetry import (
    LogRerankEventResult,
    RagTelemetryError,
    RagTelemetrySetupResult,
    log_rerank_event,
    setup_rag_telemetry,
)
from mcpg.server import create_server

_READ_ONLY = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})
_UNRESTRICTED = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)
_DDL = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_DDL": "true",
    }
)


def _valid_event_kwargs(**overrides: Any) -> dict[str, Any]:
    base = {
        "query_hash": b"\x01\x02\x03\x04",
        "retrieval_index": "public.embeddings_hnsw_idx",
        "retrieval_backend": "hnsw",
        "candidate_id": 42,
        "bi_encoder_score": 0.95,
        "bi_encoder_rank": 1,
        "cross_encoder_score": 0.87,
        "cross_encoder_rank": 2,
        "reranker_model": "voyage-rerank-2",
    }
    base.update(overrides)
    return base


# --- setup_rag_telemetry ----------------------------------------------------


async def test_setup_first_run_reports_everything_created() -> None:
    # Catalog probes all return empty → everything is "new".
    db = FakeDatabase(FakeRoutingDriver({}))  # type: ignore[arg-type]
    result = await setup_rag_telemetry(db)  # type: ignore[arg-type]
    assert result == RagTelemetrySetupResult(
        schema_created=True,
        table_created=True,
        indexes_created=3,
    )
    # Five run_unmanaged calls: schema + table + 3 indexes.
    assert len(db.unmanaged) == 5
    assert "CREATE SCHEMA IF NOT EXISTS mcpg_rag" in db.unmanaged[0]
    assert "CREATE TABLE IF NOT EXISTS mcpg_rag.rerank_events" in db.unmanaged[1]


async def test_setup_idempotent_when_everything_already_exists() -> None:
    # All catalog probes return a row → no-op result, but DDL still runs
    # (the IF NOT EXISTS is what makes it safe).
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_namespace WHERE nspname": [{"found": 1}],
                "pg_class c JOIN pg_namespace": [{"found": 1}],
            }
        )
    )
    result = await setup_rag_telemetry(db)  # type: ignore[arg-type]
    assert result == RagTelemetrySetupResult(
        schema_created=False,
        table_created=False,
        indexes_created=0,
    )
    # All five DDL statements still ran (idempotent — the table /
    # indexes / schema may exist by name but with a different shape).
    assert len(db.unmanaged) == 5


# --- log_rerank_event validation -------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("query_hash", b"", "non-empty bytes"),
        ("query_hash", "not-bytes", "non-empty bytes"),
        ("retrieval_index", "", "non-empty string"),
        ("retrieval_backend", "", "non-empty string"),
        ("reranker_model", "", "non-empty string"),
        ("candidate_id", "42", "candidate_id"),
        ("bi_encoder_rank", 0, "bi_encoder_rank"),  # below SMALLINT min (1)
        ("bi_encoder_rank", 99_999, "bi_encoder_rank"),  # above SMALLINT max
        ("bi_encoder_rank", True, "bi_encoder_rank"),  # bool rejected
        ("cross_encoder_rank", 0, "cross_encoder_rank"),
        ("cross_encoder_score", "0.5", "cross_encoder_score"),
        ("bi_encoder_score", "0.5", "bi_encoder_score"),
        ("used_in_context", "yes", "used_in_context"),
        ("ground_truth_relevance", "1", "ground_truth_relevance"),
        ("extra", "not-a-dict", "extra"),
    ],
)
async def test_log_rejects_invalid_field(field: str, value: Any, match: str) -> None:
    driver = FakeRoutingDriver({})
    kwargs = _valid_event_kwargs(**{field: value})
    with pytest.raises(RagTelemetryError, match=match):
        await log_rerank_event(driver, **kwargs)  # type: ignore[arg-type]


async def test_log_happy_path_returns_event_id() -> None:
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.rerank_events": [{"event_id": 17}]})
    result = await log_rerank_event(driver, **_valid_event_kwargs())  # type: ignore[arg-type]
    assert result == LogRerankEventResult(event_id=17)


async def test_log_happy_path_marks_call_write_capable() -> None:
    # The INSERT must NOT carry force_readonly=True — a WRITE-capability
    # gate at the driver layer relies on that flag.
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.rerank_events": [{"event_id": 1}]})
    await log_rerank_event(driver, **_valid_event_kwargs())  # type: ignore[arg-type]

    insert_calls = [c for c in driver.calls if "INSERT INTO mcpg_rag" in c[0]]
    assert len(insert_calls) == 1
    _query, _params, force_readonly = insert_calls[0]
    assert force_readonly is False


async def test_log_bi_encoder_score_may_be_null() -> None:
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.rerank_events": [{"event_id": 1}]})
    result = await log_rerank_event(driver, **_valid_event_kwargs(bi_encoder_score=None))  # type: ignore[arg-type]
    assert result.event_id == 1
    # Confirm None made it into the params verbatim.
    insert_call = next(c for c in driver.calls if "INSERT INTO mcpg_rag" in c[0])
    _query, params, _ro = insert_call
    assert params is not None
    # bi_encoder_score is the 5th positional param (after query_hash,
    # retrieval_index, retrieval_backend, candidate_id).
    assert params[4] is None


async def test_log_extra_defaults_to_empty_json() -> None:
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.rerank_events": [{"event_id": 1}]})
    await log_rerank_event(driver, **_valid_event_kwargs())  # type: ignore[arg-type]
    insert_call = next(c for c in driver.calls if "INSERT INTO mcpg_rag" in c[0])
    _query, params, _ro = insert_call
    assert params is not None
    # extra is the last positional param.
    assert params[-1] == "{}"


async def test_log_extra_serializes_dict_to_jsonb() -> None:
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.rerank_events": [{"event_id": 1}]})
    await log_rerank_event(
        driver,  # type: ignore[arg-type]
        **_valid_event_kwargs(extra={"variant": "A", "latency_ms": 123}),
    )
    insert_call = next(c for c in driver.calls if "INSERT INTO mcpg_rag" in c[0])
    _query, params, _ro = insert_call
    assert params is not None
    assert '"variant": "A"' in params[-1]
    assert '"latency_ms": 123' in params[-1]


async def test_log_raises_when_insert_returns_no_event_id() -> None:
    # Pathological: PG returned no row. Shouldn't happen with a valid
    # RETURNING clause, but the wrapper should fail loud rather than
    # invent an event_id.
    driver = FakeRoutingDriver({})
    with pytest.raises(RagTelemetryError, match="did not return event_id"):
        await log_rerank_event(driver, **_valid_event_kwargs())  # type: ignore[arg-type]


# --- MCP layer wiring ------------------------------------------------------


async def test_rag_telemetry_tools_absent_in_read_only_mode() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "log_rerank_event" not in listed
    assert "setup_rag_telemetry" not in listed


async def test_log_rerank_event_registers_in_unrestricted_mode() -> None:
    server = create_server(_UNRESTRICTED, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    # WRITE-gated, available in plain unrestricted mode.
    assert "log_rerank_event" in listed
    # DDL-gated, NOT available without MCPG_ALLOW_DDL.
    assert "setup_rag_telemetry" not in listed


async def test_setup_rag_telemetry_registers_with_ddl_opt_in() -> None:
    server = create_server(_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "setup_rag_telemetry" in listed
    assert "log_rerank_event" in listed  # WRITE is implied by unrestricted
