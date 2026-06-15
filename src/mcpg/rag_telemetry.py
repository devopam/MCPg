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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from os import environ
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.audit_nl2sql import Backend
from mcpg.audit_nl2sql import NL2SQLAuditError as _SharedAuditError
from mcpg.audit_nl2sql import _check_identifier as _shared_check_identifier
from mcpg.audit_nl2sql import _check_interval as _shared_check_interval
from mcpg.audit_nl2sql import detect_backend as _detect_backend
from mcpg.database import Database

_SCHEMA_NAME = "mcpg_rag"
_TABLE_NAME = "rerank_events"

# Ranks are 1-based positions in the candidate list. Stored as
# SMALLINT, so the column-type ceiling caps the upper bound; the
# lower bound is the smallest valid rank (1, not 0).
_RANK_MIN, _RANK_MAX = 1, 32767

# Full signed SMALLINT range — applies to ``ground_truth_relevance``
# (which is a relevance *grade*, so 0 and small negatives are valid;
# distinct from a position rank).
_SMALLINT_LO, _SMALLINT_HI = -32768, 32767

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
    idempotent). :attr:`setup_sql` carries every DDL statement that
    actually ran (in execution order) so audit / change-review
    callers can record exactly what hit the database — same
    invariant ``CreateIndexResult.create_sql`` enforces on the
    pg_search / turboquant DDL surfaces.
    """

    schema_created: bool
    table_created: bool
    indexes_created: int
    setup_sql: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LogRerankEventResult:
    """Outcome of a :func:`log_rerank_event` call."""

    event_id: int


async def _exists(driver: SqlDriver, sql: str, params: list[Any]) -> bool:
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    return bool(rows)


async def _run_setup_ddl(database: Database, sql: str, executed: list[str]) -> None:
    """Run one setup-time DDL statement and record it for the result.

    Mirrors the create_pg_search_index / create_turboquant_index
    pattern: catch the raw driver exception (psycopg, DatabaseError,
    or anything else that escapes ``run_unmanaged``) and re-raise as
    the module's typed error so callers always see
    :class:`RagTelemetryError` on a setup failure rather than a
    psycopg traceback bleeding out of the wrapper. The successful
    SQL is appended to ``executed`` so the caller can report the
    full DDL sequence even if a later statement fails.
    """
    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        raise RagTelemetryError(f"setup DDL failed ({_short_sql(sql)}): {exc}") from exc
    executed.append(sql)


def _short_sql(sql: str) -> str:
    """A one-line preview of a DDL statement for error messages.

    Collapses whitespace and caps at 80 chars — enough to identify
    which statement failed without dumping the whole CREATE INDEX
    body into the exception text.
    """
    flat = " ".join(sql.split())
    return flat if len(flat) <= 80 else flat[:77] + "..."


async def setup_rag_telemetry(database: Database) -> RagTelemetrySetupResult:
    """Create the ``mcpg_rag`` schema and ``rerank_events`` table + indexes.

    Idempotent — safe to re-run. Catalog probes before each DDL
    statement let the result honestly report first-run vs no-op.

    The DDL runs through :meth:`Database.run_unmanaged` because
    ``CREATE SCHEMA`` cannot be re-issued inside a failed transaction
    and we want each statement to commit independently.

    **Concurrent-setup caveat.** Two callers racing the probe-then-
    DDL pattern can both observe "not there" and both run their
    ``CREATE … IF NOT EXISTS``; the ``IF NOT EXISTS`` makes the
    second one a no-op (so correctness is preserved), but both
    results will report ``created=True`` for the same object. The
    flag is for telling first-call from steady-state callers, not
    for atomic ownership; treat it as advisory under concurrency.
    """
    driver = database.driver()
    executed: list[str] = []
    had_schema = await _exists(driver, _PROBE_SCHEMA_SQL, [_SCHEMA_NAME])
    await _run_setup_ddl(database, _SETUP_SQL_SCHEMA, executed)

    had_table = await _exists(driver, _PROBE_TABLE_SQL, [_SCHEMA_NAME, _TABLE_NAME])
    await _run_setup_ddl(database, _SETUP_SQL_TABLE, executed)

    indexes_created = 0
    for index_name, sql in _SETUP_SQL_INDEXES:
        had_index = await _exists(driver, _PROBE_INDEX_SQL, [_SCHEMA_NAME, index_name])
        await _run_setup_ddl(database, sql, executed)
        if not had_index:
            indexes_created += 1

    return RagTelemetrySetupResult(
        schema_created=not had_schema,
        table_created=not had_table,
        indexes_created=indexes_created,
        setup_sql=tuple(executed),
    )


def _validate_int(
    name: str,
    value: Any,
    *,
    allow_none: bool = False,
    min_value: int | None = None,
    max_value: int | None = None,
) -> None:
    """Type + optional bounds check for an integer field.

    Bools are explicitly rejected even though ``bool`` is a subclass
    of ``int`` — same trap caught on TQ-4 (#74) where
    ``concurrently=True`` would have slipped through if we hadn't
    been strict. Bound checks are skipped when their argument is
    ``None``, so callers can opt into range checking per field.
    """
    if value is None:
        if allow_none:
            return
        raise RagTelemetryError(f"{name} must be int; got None")
    if not isinstance(value, int) or isinstance(value, bool):
        raise RagTelemetryError(f"{name} must be int; got {value!r}")
    if min_value is not None and value < min_value:
        raise RagTelemetryError(f"{name} must be >= {min_value}; got {value!r}")
    if max_value is not None and value > max_value:
        raise RagTelemetryError(f"{name} must be <= {max_value}; got {value!r}")


def _validate_rank(name: str, value: int) -> None:
    """Thin wrapper over :func:`_validate_int` for rank fields."""
    _validate_int(name, value, min_value=_RANK_MIN, max_value=_RANK_MAX)


def _validate_numeric(name: str, value: Any, *, allow_none: bool = False) -> None:
    """Type check for ``DOUBLE PRECISION`` columns. Rejects bools."""
    if value is None:
        if allow_none:
            return
        raise RagTelemetryError(f"{name} must be numeric; got None")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RagTelemetryError(f"{name} must be numeric; got {value!r}")


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
    _validate_int("candidate_id", candidate_id)
    _validate_rank("bi_encoder_rank", bi_encoder_rank)
    _validate_rank("cross_encoder_rank", cross_encoder_rank)
    _validate_numeric("cross_encoder_score", cross_encoder_score)
    _validate_numeric("bi_encoder_score", bi_encoder_score, allow_none=True)
    if not isinstance(used_in_context, bool):
        raise RagTelemetryError(f"used_in_context must be bool; got {used_in_context!r}")
    # ground_truth_relevance is a relevance *grade*, not a position
    # rank — 0 (irrelevant) is a valid value, so the bound is the
    # full signed SMALLINT range, not the rank range starting at 1.
    _validate_int(
        "ground_truth_relevance",
        ground_truth_relevance,
        allow_none=True,
        min_value=_SMALLINT_LO,
        max_value=_SMALLINT_HI,
    )
    if extra is not None and not isinstance(extra, dict):
        raise RagTelemetryError(f"extra must be dict or None; got {type(extra).__name__}")

    # Wrap json.dumps so non-serialisable values (datetimes, custom
    # classes, …) surface as RagTelemetryError rather than a generic
    # TypeError from deep inside the stdlib.
    if extra is None:
        extra_json = "{}"
    else:
        try:
            extra_json = json.dumps(extra)
        except (TypeError, ValueError) as exc:
            raise RagTelemetryError(f"extra must be JSON-serialisable: {exc}") from exc

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


# --- Phase E: efficiency observations + adaptive thresholds ---------------
#
# Optional storage + corpus-percentile threshold framework for
# analyze_vector_search_efficiency (RAG-A). Callers record one
# observation per run; recommend_efficiency_thresholds computes
# per-deployment thresholds from accumulated history; the analytic
# (or anyone evaluating its rules) can swap the hardcoded defaults
# for the corpus-derived values.

_EFFICIENCY_TABLE = "efficiency_observations"

# Minimum corpus size before adaptive thresholds replace the
# hardcoded defaults. Three observations isn't a corpus; thirty
# starts to look like a deployment baseline.
_MIN_CORPUS_FOR_ADAPT = 30

_SETUP_SQL_EFFICIENCY_TABLE = f"""
CREATE TABLE IF NOT EXISTS {_SCHEMA_NAME}.{_EFFICIENCY_TABLE} (
    observation_id            BIGSERIAL PRIMARY KEY,
    observed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_name               TEXT        NOT NULL,
    table_name                TEXT        NOT NULL,
    column_name               TEXT        NOT NULL,
    index_name                TEXT        NOT NULL,
    backend                   TEXT        NOT NULL,
    metric                    TEXT        NOT NULL,
    k                         INT         NOT NULL,
    sample_size               INT         NOT NULL,
    recall_baseline           DOUBLE PRECISION,
    rerank_lift_curve         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    spearman                  DOUBLE PRECISION,
    kendall                   DOUBLE PRECISION,
    pages_pruned_ratio_p50    DOUBLE PRECISION,
    duration_seconds          DOUBLE PRECISION,
    extra                     JSONB       NOT NULL DEFAULT '{{}}'::jsonb
)
"""

_SETUP_SQL_EFFICIENCY_INDEXES: tuple[tuple[str, str], ...] = (
    (
        f"{_EFFICIENCY_TABLE}_observed_at_idx",
        f"CREATE INDEX IF NOT EXISTS {_EFFICIENCY_TABLE}_observed_at_idx "
        f"ON {_SCHEMA_NAME}.{_EFFICIENCY_TABLE} (observed_at)",
    ),
    (
        f"{_EFFICIENCY_TABLE}_backend_metric_k_idx",
        f"CREATE INDEX IF NOT EXISTS {_EFFICIENCY_TABLE}_backend_metric_k_idx "
        f"ON {_SCHEMA_NAME}.{_EFFICIENCY_TABLE} (backend, metric, k, observed_at)",
    ),
)


@dataclass(frozen=True, slots=True)
class EfficiencyObservationsSetupResult:
    """Outcome of :func:`setup_efficiency_observations`.

    :attr:`setup_sql` carries every DDL statement that actually ran
    (in execution order) — same record-the-SQL invariant the
    sibling :class:`RagTelemetrySetupResult` and the pg_search /
    turboquant DDL surfaces hold.
    """

    schema_created: bool
    table_created: bool
    indexes_created: int
    setup_sql: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordEfficiencyObservationResult:
    """Outcome of :func:`record_efficiency_observation`."""

    observation_id: int


@dataclass(frozen=True, slots=True)
class EfficiencyThresholds:
    """Thresholds for evaluating an :func:`analyze_vector_search_efficiency` report.

    Each of the three adapted thresholds carries its own ``*_adapted``
    boolean: ``True`` means the value was computed as a corpus
    percentile, ``False`` means it fell back to the module-level
    default from :mod:`mcpg.rag_efficiency`. Fallback happens both
    when the overall corpus is below :data:`_MIN_CORPUS_FOR_ADAPT`
    *and* per-metric when an individual column has too few non-null
    rows to compute a meaningful percentile.

    The roll-up ``derived_from_corpus`` is ``any(...)`` of the three
    per-metric flags — true when *at least one* threshold actually
    came from corpus data. Earlier shape (single flag set on
    corpus_size alone) misled callers when individual metrics were
    sparsely populated.

    ``corpus_size`` reports the total filtered row count (zero when
    the full-defaults branch returned). The four non-adapted
    thresholds (``rerank_lift_*``, ``ranking_degraded_recall``) are
    re-exported as defaults so callers see one unified threshold
    surface.
    """

    baseline_recall_low: float
    baseline_recall_low_adapted: bool
    ranking_degraded_spearman: float
    ranking_degraded_spearman_adapted: bool
    pruning_ineffective: float
    pruning_ineffective_adapted: bool
    # Non-adapted (defaults always):
    rerank_lift_flat_delta: float
    rerank_lift_steep_low: float
    rerank_lift_steep_high: float
    ranking_degraded_recall: float
    corpus_size: int
    derived_from_corpus: bool


async def setup_efficiency_observations(database: Database) -> EfficiencyObservationsSetupResult:
    """Create the ``mcpg_rag.efficiency_observations`` table + indexes.

    Idempotent. Uses the same probe-then-DDL pattern as
    :func:`setup_rag_telemetry`; the same concurrent-setup caveat
    applies (the result's ``created`` flags are advisory, not
    atomic).
    """
    driver = database.driver()
    executed: list[str] = []
    had_schema = await _exists(driver, _PROBE_SCHEMA_SQL, [_SCHEMA_NAME])
    await _run_setup_ddl(database, _SETUP_SQL_SCHEMA, executed)

    had_table = await _exists(driver, _PROBE_TABLE_SQL, [_SCHEMA_NAME, _EFFICIENCY_TABLE])
    await _run_setup_ddl(database, _SETUP_SQL_EFFICIENCY_TABLE, executed)

    indexes_created = 0
    for index_name, sql in _SETUP_SQL_EFFICIENCY_INDEXES:
        had_index = await _exists(driver, _PROBE_INDEX_SQL, [_SCHEMA_NAME, index_name])
        await _run_setup_ddl(database, sql, executed)
        if not had_index:
            indexes_created += 1

    return EfficiencyObservationsSetupResult(
        schema_created=not had_schema,
        table_created=not had_table,
        indexes_created=indexes_created,
        setup_sql=tuple(executed),
    )


_INSERT_OBSERVATION_SQL = f"""
INSERT INTO {_SCHEMA_NAME}.{_EFFICIENCY_TABLE} (
    schema_name, table_name, column_name, index_name,
    backend, metric, k, sample_size,
    recall_baseline, rerank_lift_curve, spearman, kendall,
    pages_pruned_ratio_p50, duration_seconds, extra
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb)
RETURNING observation_id
"""


async def record_efficiency_observation(
    driver: SqlDriver,
    *,
    schema_name: str,
    table_name: str,
    column_name: str,
    index_name: str,
    backend: str,
    metric: str,
    k: int,
    sample_size: int,
    recall_baseline: float | None,
    rerank_lift_curve: list[dict[str, Any]] | None,
    spearman: float | None,
    kendall: float | None,
    pages_pruned_ratio_p50: float | None,
    duration_seconds: float | None,
    extra: dict[str, Any] | None = None,
) -> RecordEfficiencyObservationResult:
    """Insert one row into ``mcpg_rag.efficiency_observations``.

    Fields mirror :class:`mcpg.rag_efficiency.VectorEfficiencyReport`
    one-to-one; callers typically take a freshly-produced report and
    forward its fields here. ``rerank_lift_curve`` is the list of
    ``{candidate_multiplier, knob_value, recall_at_k, ...}`` dicts
    (the report's ``rerank_lift_curve`` after ``asdict``).
    """
    _validate_text("schema_name", schema_name)
    _validate_text("table_name", table_name)
    _validate_text("column_name", column_name)
    _validate_text("index_name", index_name)
    _validate_text("backend", backend)
    _validate_text("metric", metric)
    _validate_int("k", k, min_value=1, max_value=10_000)
    _validate_int("sample_size", sample_size, min_value=0, max_value=10_000)
    _validate_numeric("recall_baseline", recall_baseline, allow_none=True)
    _validate_numeric("spearman", spearman, allow_none=True)
    _validate_numeric("kendall", kendall, allow_none=True)
    _validate_numeric("pages_pruned_ratio_p50", pages_pruned_ratio_p50, allow_none=True)
    _validate_numeric("duration_seconds", duration_seconds, allow_none=True)
    if rerank_lift_curve is not None and not isinstance(rerank_lift_curve, list):
        raise RagTelemetryError(f"rerank_lift_curve must be list[dict] or None; got {type(rerank_lift_curve).__name__}")
    if extra is not None and not isinstance(extra, dict):
        raise RagTelemetryError(f"extra must be dict or None; got {type(extra).__name__}")

    # Both jsonb-bound fields wrap json.dumps in try/except so
    # non-serialisable values (datetimes, custom classes, Decimals
    # …) surface as RagTelemetryError instead of stdlib TypeError.
    # The two paths are kept symmetric on purpose.
    try:
        curve_json = json.dumps(rerank_lift_curve) if rerank_lift_curve is not None else "[]"
    except (TypeError, ValueError) as exc:
        raise RagTelemetryError(f"rerank_lift_curve must be JSON-serialisable: {exc}") from exc
    try:
        extra_json = json.dumps(extra) if extra is not None else "{}"
    except (TypeError, ValueError) as exc:
        raise RagTelemetryError(f"extra must be JSON-serialisable: {exc}") from exc

    rows = await driver.execute_query(
        _INSERT_OBSERVATION_SQL,
        params=[
            schema_name,
            table_name,
            column_name,
            index_name,
            backend,
            metric,
            k,
            sample_size,
            recall_baseline,
            curve_json,
            spearman,
            kendall,
            pages_pruned_ratio_p50,
            duration_seconds,
            extra_json,
        ],
        force_readonly=False,
    )
    if not rows:
        raise RagTelemetryError("INSERT did not return observation_id")
    return RecordEfficiencyObservationResult(observation_id=int(rows[0].cells["observation_id"]))


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile; mirrors mcpg.rag_efficiency._percentile.

    Local copy so this module doesn't depend on rag_efficiency at
    import time — the dependency direction stays
    rag_efficiency → rag_telemetry (analytics over telemetry storage).
    """
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = lo + 1 if lo < len(s) - 1 else lo
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


async def recommend_efficiency_thresholds(
    driver: SqlDriver,
    *,
    days: int = 30,
    backend: str | None = None,
    metric: str | None = None,
    k: int | None = None,
) -> EfficiencyThresholds:
    """Compute corpus-percentile thresholds for the vector-search rule table.

    Pulls non-null ``recall_baseline``, ``spearman``, and
    ``pages_pruned_ratio_p50`` over the window and computes the 10th
    percentile of each — those become the per-deployment "you're
    below the corpus" threshold for ``baseline_recall_low``,
    ``ranking_degraded_spearman``, and ``pruning_ineffective``
    respectively. When the matching corpus is smaller than
    :data:`_MIN_CORPUS_FOR_ADAPT`, falls back to the module-level
    defaults from :mod:`mcpg.rag_efficiency`.

    Filters (``backend``, ``metric``, ``k``) all default to ``None``
    (no filter) so a caller can ask "what's normal across all my
    HNSW indexes" or "what's normal for HNSW+cosine+k=10
    specifically" depending on how much corpus they have.
    """
    from mcpg.rag_efficiency import (
        _THRESHOLD_PRUNING_INEFFECTIVE,
        _THRESHOLD_RANKING_DEGRADED_RECALL,
        _THRESHOLD_RANKING_DEGRADED_SPEARMAN,
        _THRESHOLD_RECALL_LOW,
        _THRESHOLD_RERANK_FLAT_DELTA,
        _THRESHOLD_RERANK_STEEP_HIGH,
        _THRESHOLD_RERANK_STEEP_LOW,
    )

    _validate_int("days", days, min_value=1, max_value=365)
    if backend is not None:
        _validate_text("backend", backend)
    if metric is not None:
        _validate_text("metric", metric)
    if k is not None:
        _validate_int("k", k, min_value=1, max_value=10_000)

    parts = ["observed_at >= now() - make_interval(days => %s)"]
    params: list[Any] = [days]
    if backend is not None:
        parts.append("backend = %s")
        params.append(backend)
    if metric is not None:
        parts.append("metric = %s")
        params.append(metric)
    if k is not None:
        parts.append("k = %s")
        params.append(k)
    where_sql = " AND ".join(parts)

    sql = (
        "SELECT recall_baseline, spearman, pages_pruned_ratio_p50 "
        f"FROM {_SCHEMA_NAME}.{_EFFICIENCY_TABLE} WHERE {where_sql}"
    )
    rows = await driver.execute_query(sql, params=params, force_readonly=True)
    corpus_size = len(rows or [])

    if corpus_size < _MIN_CORPUS_FOR_ADAPT:
        return EfficiencyThresholds(
            baseline_recall_low=_THRESHOLD_RECALL_LOW,
            baseline_recall_low_adapted=False,
            ranking_degraded_spearman=_THRESHOLD_RANKING_DEGRADED_SPEARMAN,
            ranking_degraded_spearman_adapted=False,
            pruning_ineffective=_THRESHOLD_PRUNING_INEFFECTIVE,
            pruning_ineffective_adapted=False,
            rerank_lift_flat_delta=_THRESHOLD_RERANK_FLAT_DELTA,
            rerank_lift_steep_low=_THRESHOLD_RERANK_STEEP_LOW,
            rerank_lift_steep_high=_THRESHOLD_RERANK_STEEP_HIGH,
            ranking_degraded_recall=_THRESHOLD_RANKING_DEGRADED_RECALL,
            corpus_size=corpus_size,
            derived_from_corpus=False,
        )

    recall_values: list[float] = []
    spearman_values: list[float] = []
    pruning_values: list[float] = []
    for row in rows or []:
        rb = row.cells.get("recall_baseline")
        if rb is not None:
            recall_values.append(float(rb))
        sp = row.cells.get("spearman")
        if sp is not None:
            spearman_values.append(float(sp))
        pp = row.cells.get("pages_pruned_ratio_p50")
        if pp is not None:
            pruning_values.append(float(pp))

    # p10 → "you're in the bottom decile of the corpus". Each metric
    # has its own ``adapted`` flag so callers can tell which
    # individual thresholds came from corpus vs default; the
    # roll-up ``derived_from_corpus`` is ``any(...)`` of the three.
    # Per-metric fallback fires when a column has too few non-null
    # contributors — we don't synthesise a threshold from 3 rows.
    recall_adapted = len(recall_values) >= _MIN_CORPUS_FOR_ADAPT
    spearman_adapted = len(spearman_values) >= _MIN_CORPUS_FOR_ADAPT
    pruning_adapted = len(pruning_values) >= _MIN_CORPUS_FOR_ADAPT

    return EfficiencyThresholds(
        baseline_recall_low=(_percentile(recall_values, 0.10) if recall_adapted else _THRESHOLD_RECALL_LOW),
        baseline_recall_low_adapted=recall_adapted,
        ranking_degraded_spearman=(
            _percentile(spearman_values, 0.10) if spearman_adapted else _THRESHOLD_RANKING_DEGRADED_SPEARMAN
        ),
        ranking_degraded_spearman_adapted=spearman_adapted,
        pruning_ineffective=(_percentile(pruning_values, 0.10) if pruning_adapted else _THRESHOLD_PRUNING_INEFFECTIVE),
        pruning_ineffective_adapted=pruning_adapted,
        rerank_lift_flat_delta=_THRESHOLD_RERANK_FLAT_DELTA,
        rerank_lift_steep_low=_THRESHOLD_RERANK_STEEP_LOW,
        rerank_lift_steep_high=_THRESHOLD_RERANK_STEEP_HIGH,
        ranking_degraded_recall=_THRESHOLD_RANKING_DEGRADED_RECALL,
        corpus_size=corpus_size,
        derived_from_corpus=any((recall_adapted, spearman_adapted, pruning_adapted)),
    )


# --- mcpg_rag partitioning retrofit (PR-5) ---------------------------------
#
# Mirrors the audit-events retrofit (PR #109) but for the two
# mcpg_rag time-series tables: rerank_events + efficiency_observations.
# These have no HMAC chain, so retention can default on (90 days) —
# operators rarely keep raw per-event rerank telemetry past a quarter.
#
# Backend ladder is the same: timescaledb > pg_partman > native. The
# native path does the rename + create partitioned + copy + drop dance
# inside one ACCESS EXCLUSIVE lock per table.


_RAG_NATIVE_WINDOW_DAYS = 7


def _rag_check_identifier(name: str, *, kind: str) -> None:
    try:
        _shared_check_identifier(name, kind=kind)
    except _SharedAuditError as exc:
        raise RagTelemetryError(str(exc)) from exc


def _rag_check_interval(value: str, *, kind: str) -> None:
    try:
        _shared_check_interval(value, kind=kind)
    except _SharedAuditError as exc:
        raise RagTelemetryError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RagTelemetryMigrationResult:
    """Outcome of :func:`migrate_rag_telemetry_to_partitioned`."""

    migrated_rerank: bool
    migrated_efficiency: bool
    backend: Backend
    rerank_rows_copied: int
    efficiency_rows_copied: int
    compression_enabled: bool
    retention_days: int | None
    rls_enabled: bool
    reader_role: str | None
    setup_sql: tuple[str, ...]


# Per-table migration parameters: PK column + timestamp column +
# qualified table + column list (for INSERT) + LZ4-target text columns.
@dataclass(frozen=True, slots=True)
class _RagTableSpec:
    table: str
    pk_column: str
    ts_column: str
    columns: tuple[str, ...]
    lz4_columns: tuple[str, ...]
    create_ddl: str


_RERANK_SPEC = _RagTableSpec(
    table=_TABLE_NAME,
    pk_column="event_id",
    ts_column="occurred_at",
    columns=(
        "event_id",
        "occurred_at",
        "query_hash",
        "retrieval_index",
        "retrieval_backend",
        "candidate_id",
        "bi_encoder_score",
        "bi_encoder_rank",
        "cross_encoder_score",
        "cross_encoder_rank",
        "reranker_model",
        "used_in_context",
        "ground_truth_relevance",
        "extra",
    ),
    # JSONB extra is a fat target; query_hash is bytea (LZ4 still
    # applies but the column is usually small). Stick to the
    # heavyweight ones.
    lz4_columns=("extra",),
    create_ddl=(
        f"CREATE TABLE {_SCHEMA_NAME}.{_TABLE_NAME} ("
        f"  event_id bigint NOT NULL DEFAULT nextval('{_SCHEMA_NAME}.{_TABLE_NAME}_event_id_seq'), "
        "  occurred_at timestamptz NOT NULL DEFAULT now(), "
        "  query_hash bytea NOT NULL, "
        "  retrieval_index text NOT NULL, "
        "  retrieval_backend text NOT NULL, "
        "  candidate_id bigint NOT NULL, "
        "  bi_encoder_score double precision, "
        "  bi_encoder_rank smallint NOT NULL, "
        "  cross_encoder_score double precision NOT NULL, "
        "  cross_encoder_rank smallint NOT NULL, "
        "  reranker_model text NOT NULL, "
        "  used_in_context boolean NOT NULL DEFAULT FALSE, "
        "  ground_truth_relevance smallint, "
        "  extra jsonb NOT NULL DEFAULT '{}'::jsonb, "
        "  PRIMARY KEY (event_id, occurred_at)"
        ") PARTITION BY RANGE (occurred_at)"
    ),
)


_EFFICIENCY_SPEC = _RagTableSpec(
    table=_EFFICIENCY_TABLE,
    pk_column="observation_id",
    ts_column="observed_at",
    columns=(
        "observation_id",
        "observed_at",
        "schema_name",
        "table_name",
        "column_name",
        "index_name",
        "backend",
        "metric",
        "k",
        "sample_size",
        "recall_baseline",
        "rerank_lift_curve",
        "spearman",
        "kendall",
        "pages_pruned_ratio_p50",
        "duration_seconds",
        "extra",
    ),
    lz4_columns=("rerank_lift_curve", "extra"),
    create_ddl=(
        f"CREATE TABLE {_SCHEMA_NAME}.{_EFFICIENCY_TABLE} ("
        f"  observation_id bigint NOT NULL "
        f"    DEFAULT nextval('{_SCHEMA_NAME}.{_EFFICIENCY_TABLE}_observation_id_seq'), "
        "  observed_at timestamptz NOT NULL DEFAULT now(), "
        "  schema_name text NOT NULL, "
        "  table_name text NOT NULL, "
        "  column_name text NOT NULL, "
        "  index_name text NOT NULL, "
        "  backend text NOT NULL, "
        "  metric text NOT NULL, "
        "  k int NOT NULL, "
        "  sample_size int NOT NULL, "
        "  recall_baseline double precision, "
        "  rerank_lift_curve jsonb NOT NULL DEFAULT '[]'::jsonb, "
        "  spearman double precision, "
        "  kendall double precision, "
        "  pages_pruned_ratio_p50 double precision, "
        "  duration_seconds double precision, "
        "  extra jsonb NOT NULL DEFAULT '{}'::jsonb, "
        "  PRIMARY KEY (observation_id, observed_at)"
        ") PARTITION BY RANGE (observed_at)"
    ),
)


def _resolve_rag_telemetry_settings(
    env: Mapping[str, str] | None,
) -> tuple[str | None, int, str, str, bool, str | None]:
    """Read MCPG_RAG_TELEMETRY_* knobs out of env.

    Returns ``(backend, retention_days, chunk_interval, compress_after,
    rls, reader_role)``. Retention defaults to 90 days — the RAG
    tables have no HMAC chain to anchor, so periodic chunk-drops are
    safe (and operators rarely keep raw per-event rerank rows past
    a quarter).
    """
    source = env if env is not None else environ
    backend_raw = (source.get("MCPG_RAG_TELEMETRY_BACKEND") or "").strip() or None
    retention_raw = (source.get("MCPG_RAG_TELEMETRY_RETENTION_DAYS") or "").strip()
    if retention_raw:
        parsed = int(retention_raw)
        if parsed < 1:
            raise RagTelemetryError(
                f"MCPG_RAG_TELEMETRY_RETENTION_DAYS must be a positive integer; got {retention_raw!r}"
            )
        retention_days: int = parsed
    else:
        retention_days = 90
    chunk_interval = (source.get("MCPG_RAG_TELEMETRY_CHUNK_INTERVAL") or "").strip() or "1 day"
    _rag_check_interval(chunk_interval, kind="MCPG_RAG_TELEMETRY_CHUNK_INTERVAL")
    compress_after = (source.get("MCPG_RAG_TELEMETRY_COMPRESS_AFTER") or "").strip() or "7 days"
    _rag_check_interval(compress_after, kind="MCPG_RAG_TELEMETRY_COMPRESS_AFTER")
    rls_raw = (source.get("MCPG_RAG_TELEMETRY_RLS") or "").strip().lower()
    rls = rls_raw in ("", "true", "1", "yes", "on")
    reader_role = (source.get("MCPG_RAG_TELEMETRY_READER_ROLE") or "").strip() or None
    if reader_role is not None:
        _rag_check_identifier(reader_role, kind="rag telemetry reader role")
    return backend_raw, retention_days, chunk_interval, compress_after, rls, reader_role


async def _rag_table_exists(driver: SqlDriver, table: str) -> bool:
    rows = await driver.execute_query(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[_SCHEMA_NAME, table],
        force_readonly=True,
    )
    return bool(rows)


async def _rag_table_is_partitioned(driver: SqlDriver, table: str) -> bool:
    rows = await driver.execute_query(
        "SELECT 1 FROM pg_partitioned_table pt "
        "JOIN pg_class c ON c.oid = pt.partrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[_SCHEMA_NAME, table],
        force_readonly=True,
    )
    if rows:
        return True
    # TimescaleDB hypertable probe.
    ht_rows = await driver.execute_query(
        "SELECT 1 FROM pg_extension WHERE extname = %s",
        params=["timescaledb"],
        force_readonly=True,
    )
    if ht_rows:
        ht = await driver.execute_query(
            "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_schema = %s AND hypertable_name = %s",
            params=[_SCHEMA_NAME, table],
            force_readonly=True,
        )
        if ht:
            return True
    return False


async def _rag_data_range(driver: SqlDriver, spec: _RagTableSpec) -> tuple[datetime | None, datetime | None, int]:
    rows = await driver.execute_query(
        f"SELECT min({spec.ts_column}) AS lo, max({spec.ts_column}) AS hi, count(*) AS n "
        f"FROM {_SCHEMA_NAME}.{spec.table}",
        force_readonly=True,
    )
    if not rows:
        return (None, None, 0)
    c = rows[0].cells
    return (c.get("lo"), c.get("hi"), int(c.get("n") or 0))


def _rag_native_monthly_partition_sql(spec: _RagTableSpec, month_start: datetime) -> str:
    start = month_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return (
        f"CREATE TABLE IF NOT EXISTS {_SCHEMA_NAME}.{spec.table}_p{start.strftime('%Y%m')} "
        f"PARTITION OF {_SCHEMA_NAME}.{spec.table} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def _rag_native_daily_partition_sql(spec: _RagTableSpec, day: datetime) -> str:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (
        f"CREATE TABLE IF NOT EXISTS {_SCHEMA_NAME}.{spec.table}_p{start.strftime('%Y%m%d')} "
        f"PARTITION OF {_SCHEMA_NAME}.{spec.table} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


async def _rag_migrate_native(
    driver: SqlDriver,
    spec: _RagTableSpec,
) -> tuple[int, bool, list[str]]:
    """Rename + create partitioned + copy + drop legacy under one
    ACCESS EXCLUSIVE lock. Returns
    ``(rows_copied, compression_enabled, statements)``.

    .. important::
       All migration DDL is concatenated into a single multi-statement
       string and sent via one :meth:`execute_query` call. The driver
       commits at each ``execute_query`` boundary (line 249/260 of
       sql_driver.py), so issuing the LOCK in its own call would
       release the ACCESS EXCLUSIVE mode immediately and let
       concurrent writers race the rename dance (gemini critical
       review, PR #110). PG's simple-query protocol treats a
       semicolon-separated batch as one implicit transaction, so the
       lock holds for the entire migration when sent as one call.
    """
    # Reads are issued separately — they happen before the lock and
    # don't need to be in the migration transaction.
    lo, hi, row_count = await _rag_data_range(driver, spec)
    version_rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::integer AS ver",
        force_readonly=True,
    )
    pg_version = int(version_rows[0].cells["ver"]) if version_rows else 0

    qualified = f"{_SCHEMA_NAME}.{spec.table}"
    seq_qualified = f"{_SCHEMA_NAME}.{spec.table}_{spec.pk_column}_seq"
    legacy = f"{spec.table}_migration_legacy"
    legacy_qualified = f"{_SCHEMA_NAME}.{legacy}"

    statements: list[str] = [
        f"LOCK TABLE {qualified} IN ACCESS EXCLUSIVE MODE",
        f"ALTER SEQUENCE {seq_qualified} OWNED BY NONE",
        f"ALTER TABLE {qualified} RENAME TO {legacy}",
        spec.create_ddl,
        f"ALTER SEQUENCE {seq_qualified} OWNED BY {qualified}.{spec.pk_column}",
    ]

    # Monthly partitions across data range + ±7 daily trailing/forward.
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if lo is not None and hi is not None:
        cursor = lo.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cursor <= hi:
            statements.append(_rag_native_monthly_partition_sql(spec, cursor))
            cursor = (
                cursor.replace(year=cursor.year + 1, month=1)
                if cursor.month == 12
                else cursor.replace(month=cursor.month + 1)
            )
    for offset_d in range(-_RAG_NATIVE_WINDOW_DAYS, _RAG_NATIVE_WINDOW_DAYS + 1):
        day = today + timedelta(days=offset_d)
        if lo is not None and hi is not None:
            month_start = day.replace(day=1)
            if month_start <= hi.replace(day=1):
                # Monthly partition for this month already covers it.
                continue
        statements.append(_rag_native_daily_partition_sql(spec, day))

    col_list = ", ".join(spec.columns)
    statements.append(
        f"INSERT INTO {qualified} ({col_list}) SELECT {col_list} FROM {legacy_qualified} ORDER BY {spec.pk_column}"
    )

    # LZ4 (PG 14+). Decided up front from the version probe — a
    # failed ALTER inside the migration batch would abort everything
    # that follows (DROP legacy).
    compression_enabled = False
    if pg_version >= 140000:
        for col in spec.lz4_columns:
            statements.append(f"ALTER TABLE {qualified} ALTER COLUMN {col} SET COMPRESSION lz4")
            compression_enabled = True

    statements.append(f"DROP TABLE {legacy_qualified}")

    # Send the entire migration as one query. PG's simple-query
    # protocol holds the implicit transaction across all statements
    # in the batch, so the LOCK acquired in the first statement is
    # held through the DROP at the end.
    batch_sql = ";\n".join(statements)
    await driver.execute_query(batch_sql, force_readonly=False)

    return row_count, compression_enabled, statements


async def _rag_migrate_timescaledb(
    driver: SqlDriver,
    spec: _RagTableSpec,
    *,
    chunk_interval: str,
    compress_after: str,
    retention_days: int | None,
) -> tuple[int, bool, list[str]]:
    """In-place create_hypertable(migrate_data => TRUE).

    The PK rebuild (DROP + ADD) needs to be atomic — between the two
    statements the table has no PK constraint and a concurrent writer
    could insert duplicates. Bundle into one execute_query call so
    the simple-query protocol holds an implicit transaction across
    both statements (same fix as :func:`_rag_migrate_native`,
    gemini critical review PR #110).
    """
    statements: list[str] = []
    qualified = f"{_SCHEMA_NAME}.{spec.table}"
    _, _, row_count = await _rag_data_range(driver, spec)

    # Atomic PK rebuild — single batch so the no-PK window is invisible
    # to other writers.
    pk_rebuild = (
        f"ALTER TABLE {qualified} DROP CONSTRAINT IF EXISTS {spec.table}_pkey;\n"
        f"ALTER TABLE {qualified} ADD PRIMARY KEY ({spec.pk_column}, {spec.ts_column})"
    )
    await driver.execute_query(pk_rebuild, force_readonly=False)
    statements.append(pk_rebuild)

    sql_ht = (
        f"SELECT create_hypertable('{qualified}', '{spec.ts_column}', "
        f"chunk_time_interval => INTERVAL '{chunk_interval}', "
        "migrate_data => TRUE, "
        "if_not_exists => TRUE)"
    )
    await driver.execute_query(sql_ht, force_readonly=False)
    statements.append(sql_ht)

    sql_compress = f"ALTER TABLE {qualified} SET (timescaledb.compress = TRUE)"
    await driver.execute_query(sql_compress, force_readonly=False)
    statements.append(sql_compress)

    compression_enabled = False
    sql_cp = f"SELECT add_compression_policy('{qualified}', INTERVAL '{compress_after}', if_not_exists => TRUE)"
    try:
        await driver.execute_query(sql_cp, force_readonly=False)
        statements.append(sql_cp)
        compression_enabled = True
    except Exception:
        pass

    if retention_days is not None:
        sql_rp = f"SELECT add_retention_policy('{qualified}', INTERVAL '{retention_days} days', if_not_exists => TRUE)"
        try:
            await driver.execute_query(sql_rp, force_readonly=False)
            statements.append(sql_rp)
        except Exception:
            pass

    return row_count, compression_enabled, statements


async def _rag_apply_rls(
    driver: SqlDriver,
    *,
    table: str,
    enabled: bool,
    reader_role: str | None,
    statements: list[str],
) -> None:
    if not enabled:
        return
    qualified = f"{_SCHEMA_NAME}.{table}"
    sql_rls = f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"
    await driver.execute_query(sql_rls, force_readonly=False)
    statements.append(sql_rls)
    sql_force = f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"
    await driver.execute_query(sql_force, force_readonly=False)
    statements.append(sql_force)
    if reader_role is None:
        return
    policy_name = f"{table}_reader_select"
    sql_policy = (
        "DO $$ BEGIN "
        f"  CREATE POLICY {policy_name} ON {qualified} "
        f"    FOR SELECT TO {reader_role} USING (true); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    await driver.execute_query(sql_policy, force_readonly=False)
    statements.append(sql_policy)
    sql_grant_schema = f"GRANT USAGE ON SCHEMA {_SCHEMA_NAME} TO {reader_role}"
    await driver.execute_query(sql_grant_schema, force_readonly=False)
    statements.append(sql_grant_schema)
    sql_grant_tab = f"GRANT SELECT ON {qualified} TO {reader_role}"
    await driver.execute_query(sql_grant_tab, force_readonly=False)
    statements.append(sql_grant_tab)


async def _migrate_one_rag_table(
    driver: SqlDriver,
    spec: _RagTableSpec,
    *,
    backend: Backend,
    chunk_interval: str,
    compress_after: str,
    retention_days: int | None,
    rls: bool,
    reader_role: str | None,
) -> tuple[bool, int, bool, list[str]]:
    """Migrate one RAG telemetry table. Returns
    ``(migrated, rows_copied, compression_enabled, statements)``.

    Skips when the table is missing (telemetry was never set up) or
    already partitioned (re-run is a no-op).
    """
    if not await _rag_table_exists(driver, spec.table):
        return (False, 0, False, [])
    statements: list[str] = []
    if await _rag_table_is_partitioned(driver, spec.table):
        await _rag_apply_rls(
            driver,
            table=spec.table,
            enabled=rls,
            reader_role=reader_role,
            statements=statements,
        )
        return (False, 0, False, statements)

    if backend == "timescaledb":
        rows_copied, compression_enabled, table_stmts = await _rag_migrate_timescaledb(
            driver,
            spec,
            chunk_interval=chunk_interval,
            compress_after=compress_after,
            retention_days=retention_days,
        )
    else:
        rows_copied, compression_enabled, table_stmts = await _rag_migrate_native(driver, spec)
        if backend == "pg_partman":
            sql_partman = (
                f"SELECT partman.create_parent("
                f"  p_parent_table := '{_SCHEMA_NAME}.{spec.table}', "
                f"  p_control := '{spec.ts_column}', "
                "  p_type := 'range', "
                f"  p_interval := '{chunk_interval}'"
                ")"
            )
            try:
                await driver.execute_query(sql_partman, force_readonly=False)
                table_stmts.append(sql_partman)
            except Exception:
                pass

    statements.extend(table_stmts)
    await _rag_apply_rls(
        driver,
        table=spec.table,
        enabled=rls,
        reader_role=reader_role,
        statements=statements,
    )
    return (True, rows_copied, compression_enabled, statements)


async def migrate_rag_telemetry_to_partitioned(
    driver: SqlDriver,
    *,
    env: Mapping[str, str] | None = None,
) -> RagTelemetryMigrationResult:
    """Retrofit ``mcpg_rag.rerank_events`` + ``mcpg_rag.efficiency_observations``
    onto the partitioning stack.

    Idempotent — re-running on already-partitioned tables is a near-no-op.
    Either table may be absent (telemetry never set up); the migration
    handles each independently. Returns one consolidated result so the
    operator can inspect everything that ran.

    Backend ladder (auto-detected, overrideable via
    ``MCPG_RAG_TELEMETRY_BACKEND``):

    * **timescaledb** — in-place ``create_hypertable(migrate_data => TRUE)``
      per table. Compression + retention policies applied with
      ``if_not_exists => TRUE``.
    * **pg_partman** / **native** — rename + create partitioned + copy +
      drop dance per table, each under its own ACCESS EXCLUSIVE lock.
      Monthly historical partitions + daily trailing/forward window.
      LZ4 column compression on JSONB columns (PG 14+).

    Retention defaults to **90 days** — no HMAC chain to anchor here,
    unlike :func:`migrate_audit_events_to_partitioned`. Operators with
    longer audit horizons override via
    ``MCPG_RAG_TELEMETRY_RETENTION_DAYS``.
    """
    forced, retention_days, chunk_interval, compress_after, rls, reader_role = _resolve_rag_telemetry_settings(env)
    backend = await _detect_backend(driver, forced=forced)
    all_statements: list[str] = []

    migrated_rerank, rerank_rows, comp_a, stmts_a = await _migrate_one_rag_table(
        driver,
        _RERANK_SPEC,
        backend=backend,
        chunk_interval=chunk_interval,
        compress_after=compress_after,
        retention_days=retention_days,
        rls=rls,
        reader_role=reader_role,
    )
    all_statements.extend(stmts_a)

    migrated_eff, eff_rows, comp_b, stmts_b = await _migrate_one_rag_table(
        driver,
        _EFFICIENCY_SPEC,
        backend=backend,
        chunk_interval=chunk_interval,
        compress_after=compress_after,
        retention_days=retention_days,
        rls=rls,
        reader_role=reader_role,
    )
    all_statements.extend(stmts_b)

    return RagTelemetryMigrationResult(
        migrated_rerank=migrated_rerank,
        migrated_efficiency=migrated_eff,
        backend=backend,
        rerank_rows_copied=rerank_rows,
        efficiency_rows_copied=eff_rows,
        compression_enabled=(comp_a or comp_b),
        retention_days=retention_days,
        rls_enabled=rls,
        reader_role=reader_role,
        setup_sql=tuple(all_statements),
    )
