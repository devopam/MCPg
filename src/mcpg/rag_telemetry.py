"""RAG telemetry — Phase C of the RAG efficiency suite.

MCPg-owned schema (`mcpg_rag.rerank_events`) for accumulating
cross-encoder rerank observations, plus the setup tool that creates
it and the write tool that ingests events.

This is the storage layer for the Phase D analytics
(`analyze_reranker_lift`, `analyze_topk_stability`,
`analyze_rerank_score_distribution`, `analyze_rerank_ndcg`,
`recommend_rerank_strategy`) and the `audit_rag_pipeline` category.
Callers — the RAG application, not MCPg itself — write one row per
``(query, candidate)`` pair via :func:`log_rerank_event` (or by
INSERTing directly with their own DB client for throughput); MCPg
provides the schema and the analytics over it.

**PII boundary.** The schema stores ``query_hash`` (BYTEA, caller-
computed) rather than the raw query text. The caller can stash the
text in ``extra`` if they accept the responsibility. The MCPg layer
never sees plaintext queries by default.

**Idempotency.** :func:`setup_rag_telemetry` is safe to re-run. It
checks the catalog before running the DDL and reports what was
actually created so the caller can tell first-run from no-op.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.database import Database

_SCHEMA_NAME = "mcpg_rag"
_TABLE_NAME = "rerank_events"

# SMALLINT bounds — bi/cross encoder ranks are 1-based positions in
# the candidate list, so 1..32767 covers any sane candidate pool.
_SMALLINT_MIN, _SMALLINT_MAX = 1, 32767

_SETUP_SQL_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA_NAME}"

_SETUP_SQL_TABLE = f"""
CREATE TABLE IF NOT EXISTS {_SCHEMA_NAME}.{_TABLE_NAME} (
    event_id               BIGSERIAL PRIMARY KEY,
    occurred_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    query_hash             BYTEA       NOT NULL,
    retrieval_index        TEXT        NOT NULL,
    retrieval_backend      TEXT        NOT NULL,
    candidate_id           BIGINT      NOT NULL,
    bi_encoder_score       DOUBLE PRECISION,
    bi_encoder_rank        SMALLINT    NOT NULL,
    cross_encoder_score    DOUBLE PRECISION NOT NULL,
    cross_encoder_rank     SMALLINT    NOT NULL,
    reranker_model         TEXT        NOT NULL,
    used_in_context        BOOLEAN     NOT NULL DEFAULT FALSE,
    ground_truth_relevance SMALLINT,
    extra                  JSONB       NOT NULL DEFAULT '{{}}'::jsonb
)
"""

# Three indexes per the plan: window scans by ``occurred_at``,
# per-query rollups by ``query_hash``, and per-model time-window
# slicing via the composite ``(reranker_model, occurred_at)``.
_SETUP_SQL_INDEXES: tuple[tuple[str, str], ...] = (
    (
        f"{_TABLE_NAME}_occurred_at_idx",
        f"CREATE INDEX IF NOT EXISTS {_TABLE_NAME}_occurred_at_idx ON {_SCHEMA_NAME}.{_TABLE_NAME} (occurred_at)",
    ),
    (
        f"{_TABLE_NAME}_query_hash_idx",
        f"CREATE INDEX IF NOT EXISTS {_TABLE_NAME}_query_hash_idx ON {_SCHEMA_NAME}.{_TABLE_NAME} (query_hash)",
    ),
    (
        f"{_TABLE_NAME}_model_time_idx",
        f"CREATE INDEX IF NOT EXISTS {_TABLE_NAME}_model_time_idx "
        f"ON {_SCHEMA_NAME}.{_TABLE_NAME} (reranker_model, occurred_at)",
    ),
)


# Catalog probes: "does the schema / table / index exist?" so the
# setup result can honestly report first-run vs no-op.
_PROBE_SCHEMA_SQL = "SELECT 1 FROM pg_namespace WHERE nspname = %s"
_PROBE_TABLE_SQL = (
    "SELECT 1 FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'r'"
)
_PROBE_INDEX_SQL = (
    "SELECT 1 FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'i'"
)


class RagTelemetryError(Exception):
    """Raised when a RAG telemetry operation cannot complete."""


@dataclass(frozen=True, slots=True)
class RagTelemetrySetupResult:
    """Outcome of a :func:`setup_rag_telemetry` call.

    The booleans/counts report what actually changed in the catalog
    on this call. All-``False`` / zero means everything was already
    in place — the call was a no-op (intended; the operation is
    idempotent).
    """

    schema_created: bool
    table_created: bool
    indexes_created: int


@dataclass(frozen=True, slots=True)
class LogRerankEventResult:
    """Outcome of a :func:`log_rerank_event` call."""

    event_id: int


async def _exists(driver: SqlDriver, sql: str, params: list[Any]) -> bool:
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    return bool(rows)


async def setup_rag_telemetry(database: Database) -> RagTelemetrySetupResult:
    """Create the ``mcpg_rag`` schema and ``rerank_events`` table + indexes.

    Idempotent — safe to re-run. Catalog probes before each DDL
    statement let the result honestly report first-run vs no-op.

    The DDL runs through :meth:`Database.run_unmanaged` because
    ``CREATE SCHEMA`` cannot be re-issued inside a failed transaction
    and we want each statement to commit independently.
    """
    driver = database.driver()
    had_schema = await _exists(driver, _PROBE_SCHEMA_SQL, [_SCHEMA_NAME])
    await database.run_unmanaged(_SETUP_SQL_SCHEMA)

    had_table = await _exists(driver, _PROBE_TABLE_SQL, [_SCHEMA_NAME, _TABLE_NAME])
    await database.run_unmanaged(_SETUP_SQL_TABLE)

    indexes_created = 0
    for index_name, sql in _SETUP_SQL_INDEXES:
        had_index = await _exists(driver, _PROBE_INDEX_SQL, [_SCHEMA_NAME, index_name])
        await database.run_unmanaged(sql)
        if not had_index:
            indexes_created += 1

    return RagTelemetrySetupResult(
        schema_created=not had_schema,
        table_created=not had_table,
        indexes_created=indexes_created,
    )


def _validate_rank(name: str, value: int) -> None:
    """Bounds check for SMALLINT-stored ranks.

    Bools are explicitly rejected (subclass-of-int caught the
    `concurrently=True` bug back on TQ-4 #74; same trap here).
    """
    if not isinstance(value, int) or isinstance(value, bool) or not _SMALLINT_MIN <= value <= _SMALLINT_MAX:
        raise RagTelemetryError(f"{name} must be an int in [{_SMALLINT_MIN}..{_SMALLINT_MAX}]; got {value!r}")


def _validate_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise RagTelemetryError(f"{name} must be a non-empty string; got {value!r}")


def _validate_bytes(name: str, value: bytes) -> None:
    if not isinstance(value, (bytes, bytearray)) or len(value) == 0:
        raise RagTelemetryError(f"{name} must be non-empty bytes; got {type(value).__name__}")


_INSERT_EVENT_SQL = f"""
INSERT INTO {_SCHEMA_NAME}.{_TABLE_NAME} (
    query_hash, retrieval_index, retrieval_backend,
    candidate_id, bi_encoder_score, bi_encoder_rank,
    cross_encoder_score, cross_encoder_rank,
    reranker_model, used_in_context,
    ground_truth_relevance, extra
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
RETURNING event_id
"""


async def log_rerank_event(
    driver: SqlDriver,
    *,
    query_hash: bytes,
    retrieval_index: str,
    retrieval_backend: str,
    candidate_id: int,
    bi_encoder_score: float | None,
    bi_encoder_rank: int,
    cross_encoder_score: float,
    cross_encoder_rank: int,
    reranker_model: str,
    used_in_context: bool = False,
    ground_truth_relevance: int | None = None,
    extra: dict[str, Any] | None = None,
) -> LogRerankEventResult:
    """Insert one row into ``mcpg_rag.rerank_events``.

    Most callers will write events directly via their own DB client
    for throughput; this wrapper is for the cases where the agent
    itself is curating an evaluation set or where the integration
    surface needs a typed-validation layer.

    All required fields are validated up front. ``query_hash`` is
    the join key for the Phase-D analytics — the caller computes it
    over the normalised query text (SHA-256 is the conventional
    choice but MCPg doesn't impose one — any non-empty bytes work).

    Raises:
        RagTelemetryError: any required field fails its type / range
            check, or ``extra`` isn't a dict.
    """
    _validate_bytes("query_hash", query_hash)
    _validate_text("retrieval_index", retrieval_index)
    _validate_text("retrieval_backend", retrieval_backend)
    _validate_text("reranker_model", reranker_model)
    if not isinstance(candidate_id, int) or isinstance(candidate_id, bool):
        raise RagTelemetryError(f"candidate_id must be int; got {candidate_id!r}")
    _validate_rank("bi_encoder_rank", bi_encoder_rank)
    _validate_rank("cross_encoder_rank", cross_encoder_rank)
    if not isinstance(cross_encoder_score, (int, float)) or isinstance(cross_encoder_score, bool):
        raise RagTelemetryError(f"cross_encoder_score must be numeric; got {cross_encoder_score!r}")
    if bi_encoder_score is not None and (
        not isinstance(bi_encoder_score, (int, float)) or isinstance(bi_encoder_score, bool)
    ):
        raise RagTelemetryError(f"bi_encoder_score must be numeric or None; got {bi_encoder_score!r}")
    if not isinstance(used_in_context, bool):
        raise RagTelemetryError(f"used_in_context must be bool; got {used_in_context!r}")
    if ground_truth_relevance is not None and (
        not isinstance(ground_truth_relevance, int) or isinstance(ground_truth_relevance, bool)
    ):
        raise RagTelemetryError(f"ground_truth_relevance must be int or None; got {ground_truth_relevance!r}")
    if extra is not None and not isinstance(extra, dict):
        raise RagTelemetryError(f"extra must be dict or None; got {type(extra).__name__}")

    extra_json = json.dumps(extra) if extra is not None else "{}"

    rows = await driver.execute_query(
        _INSERT_EVENT_SQL,
        params=[
            bytes(query_hash),
            retrieval_index,
            retrieval_backend,
            candidate_id,
            bi_encoder_score,
            bi_encoder_rank,
            float(cross_encoder_score),
            cross_encoder_rank,
            reranker_model,
            used_in_context,
            ground_truth_relevance,
            extra_json,
        ],
        force_readonly=False,
    )
    if not rows:
        raise RagTelemetryError("INSERT did not return event_id")
    return LogRerankEventResult(event_id=int(rows[0].cells["event_id"]))
