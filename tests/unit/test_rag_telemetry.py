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
        ("bi_encoder_rank", 99_999, "bi_encoder_rank"),  # above SMALLINT rank max
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


@pytest.mark.parametrize("bad", [-32_769, 32_768, 100_000])
async def test_log_rejects_ground_truth_relevance_outside_smallint(bad: int) -> None:
    # SMALLINT range is [-32768, 32767]. Values outside that would
    # historically pass Python validation and fail at INSERT time as
    # a DB error — sourcery + gemini both flagged this independently.
    driver = FakeRoutingDriver({})
    kwargs = _valid_event_kwargs(ground_truth_relevance=bad)
    with pytest.raises(RagTelemetryError, match="ground_truth_relevance"):
        await log_rerank_event(driver, **kwargs)  # type: ignore[arg-type]


async def test_log_accepts_ground_truth_relevance_zero() -> None:
    # Relevance grades use 0 (irrelevant). The earlier validator
    # used the rank lower bound (1) which would have wrongly rejected
    # this. Regression coverage for the rank/grade distinction.
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.rerank_events": [{"event_id": 1}]})
    await log_rerank_event(driver, **_valid_event_kwargs(ground_truth_relevance=0))  # type: ignore[arg-type]


async def test_log_rejects_non_json_serialisable_extra() -> None:
    # Datetime, custom classes, sets, etc. — json.dumps raises
    # TypeError. The wrapper turns it into RagTelemetryError so
    # callers don't see stdlib internals leaking through.
    import datetime as _dt

    driver = FakeRoutingDriver({})
    kwargs = _valid_event_kwargs(extra={"when": _dt.datetime.now(_dt.UTC)})
    with pytest.raises(RagTelemetryError, match="JSON-serialisable"):
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


# --- Phase E: efficiency observations + adaptive thresholds ---------------


from mcpg.rag_telemetry import (  # noqa: E402
    EfficiencyObservationsSetupResult,
    RecordEfficiencyObservationResult,
    recommend_efficiency_thresholds,
    record_efficiency_observation,
    setup_efficiency_observations,
)


def _valid_observation_kwargs(**overrides: Any) -> dict[str, Any]:
    base = {
        "schema_name": "public",
        "table_name": "embeddings",
        "column_name": "embedding",
        "index_name": "embeddings_hnsw_idx",
        "backend": "hnsw",
        "metric": "cosine",
        "k": 10,
        "sample_size": 30,
        "recall_baseline": 0.95,
        "rerank_lift_curve": [
            {"candidate_multiplier": 1, "knob_name": "ef_search", "knob_value": 20, "recall_at_k": 0.95},
        ],
        "spearman": 0.92,
        "kendall": 0.88,
        "pages_pruned_ratio_p50": None,
        "duration_seconds": 1.234,
    }
    base.update(overrides)
    return base


async def test_setup_efficiency_observations_first_run_creates_table_and_indexes() -> None:
    db = FakeDatabase(FakeRoutingDriver({}))  # type: ignore[arg-type]
    result = await setup_efficiency_observations(db)  # type: ignore[arg-type]
    assert result == EfficiencyObservationsSetupResult(
        schema_created=True,
        table_created=True,
        indexes_created=2,
    )
    # Four run_unmanaged calls: schema + table + 2 indexes.
    assert len(db.unmanaged) == 4


async def test_setup_efficiency_observations_idempotent_when_exists() -> None:
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_namespace WHERE nspname": [{"found": 1}],
                "pg_class c JOIN pg_namespace": [{"found": 1}],
            }
        )
    )
    result = await setup_efficiency_observations(db)  # type: ignore[arg-type]
    assert result.schema_created is False
    assert result.table_created is False
    assert result.indexes_created == 0


async def test_record_efficiency_observation_happy_path_returns_id() -> None:
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.efficiency_observations": [{"observation_id": 42}]})
    result = await record_efficiency_observation(driver, **_valid_observation_kwargs())  # type: ignore[arg-type]
    assert result == RecordEfficiencyObservationResult(observation_id=42)


async def test_record_efficiency_observation_marks_call_write_capable() -> None:
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.efficiency_observations": [{"observation_id": 1}]})
    await record_efficiency_observation(driver, **_valid_observation_kwargs())  # type: ignore[arg-type]
    insert_calls = [c for c in driver.calls if "INSERT INTO mcpg_rag.efficiency_observations" in c[0]]
    assert len(insert_calls) == 1
    _query, _params, force_readonly = insert_calls[0]
    assert force_readonly is False


async def test_record_efficiency_observation_rejects_invalid_k() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(RagTelemetryError, match="k"):
        await record_efficiency_observation(driver, **_valid_observation_kwargs(k=0))  # type: ignore[arg-type]


async def test_record_efficiency_observation_serializes_curve_as_jsonb() -> None:
    driver = FakeRoutingDriver({"INSERT INTO mcpg_rag.efficiency_observations": [{"observation_id": 1}]})
    await record_efficiency_observation(driver, **_valid_observation_kwargs())  # type: ignore[arg-type]
    insert_call = next(c for c in driver.calls if "INSERT INTO mcpg_rag.efficiency_observations" in c[0])
    _query, params, _ro = insert_call
    assert params is not None
    # curve is the 10th positional param (after schema/table/column/index/
    # backend/metric/k/sample_size/recall_baseline).
    assert "candidate_multiplier" in params[9]


async def test_recommend_thresholds_returns_defaults_when_corpus_small() -> None:
    # Default thresholds (from rag_efficiency module constants) when
    # there's not enough corpus to learn from.
    driver = FakeRoutingDriver({"FROM mcpg_rag.efficiency_observations": []})
    result = await recommend_efficiency_thresholds(driver)  # type: ignore[arg-type]
    assert result.derived_from_corpus is False
    assert result.corpus_size == 0
    # Sanity: the defaults are the values from rag_efficiency.
    from mcpg.rag_efficiency import _THRESHOLD_PRUNING_INEFFECTIVE, _THRESHOLD_RECALL_LOW

    assert result.baseline_recall_low == _THRESHOLD_RECALL_LOW
    assert result.pruning_ineffective == _THRESHOLD_PRUNING_INEFFECTIVE


async def test_recommend_thresholds_derives_from_corpus_at_threshold() -> None:
    # 30 observations is the minimum; recall_baseline values span
    # 0.5 .. 1.0, so p10 should land near 0.55.
    rows = []
    for i in range(30):
        rows.append(
            {
                "recall_baseline": 0.5 + i / 100.0,
                "spearman": 0.6 + i / 100.0,
                "pages_pruned_ratio_p50": 0.1 + i / 100.0,
            }
        )
    driver = FakeRoutingDriver({"FROM mcpg_rag.efficiency_observations": rows})
    result = await recommend_efficiency_thresholds(driver)  # type: ignore[arg-type]
    assert result.derived_from_corpus is True
    assert result.corpus_size == 30
    # p10 of (0.50..0.79) ≈ 0.529 — adaptive, not the 0.80 default.
    assert 0.50 < result.baseline_recall_low < 0.60
    assert 0.60 < result.ranking_degraded_spearman < 0.70
    assert 0.10 < result.pruning_ineffective < 0.20


async def test_recommend_thresholds_falls_back_per_metric_when_too_few_non_nulls() -> None:
    # 30 rows total, but only 5 have non-null spearman → spearman
    # threshold falls back to default, while recall + pruning are
    # corpus-derived.
    rows = []
    for i in range(30):
        rows.append(
            {
                "recall_baseline": 0.5 + i / 100.0,
                "spearman": 0.5 if i < 5 else None,
                "pages_pruned_ratio_p50": 0.1 + i / 100.0,
            }
        )
    driver = FakeRoutingDriver({"FROM mcpg_rag.efficiency_observations": rows})
    result = await recommend_efficiency_thresholds(driver)  # type: ignore[arg-type]
    assert result.derived_from_corpus is True
    from mcpg.rag_efficiency import _THRESHOLD_RANKING_DEGRADED_SPEARMAN

    assert result.ranking_degraded_spearman == _THRESHOLD_RANKING_DEGRADED_SPEARMAN  # fallback
    assert 0.50 < result.baseline_recall_low < 0.60  # adapted


async def test_recommend_thresholds_window_validation() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(RagTelemetryError, match="days"):
        await recommend_efficiency_thresholds(driver, days=0)  # type: ignore[arg-type]


# --- MCP layer wiring -----------------------------------------------------


async def test_efficiency_tools_absent_in_read_only_mode() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    # Read mode: only the read tool, but NOT the read tool yet because
    # an empty events table is fine — confirm absence of the write/DDL.
    assert "record_efficiency_observation" not in listed
    assert "setup_efficiency_observations" not in listed
    # recommend_efficiency_thresholds IS a read tool, available in every mode.
    assert "recommend_efficiency_thresholds" in listed


async def test_efficiency_write_tool_registers_in_unrestricted() -> None:
    server = create_server(_UNRESTRICTED, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "record_efficiency_observation" in listed
    # DDL still absent without MCPG_ALLOW_DDL
    assert "setup_efficiency_observations" not in listed


async def test_efficiency_setup_tool_registers_with_ddl_opt_in() -> None:
    server = create_server(_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "setup_efficiency_observations" in listed
