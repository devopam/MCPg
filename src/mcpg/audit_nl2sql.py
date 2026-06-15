"""NL→SQL audit table — partitioned, compressed, RLS-gated.

Companion to :mod:`mcpg.audit_trail` (which persists every write/DDL)
and :mod:`mcpg.rag_telemetry`. This module owns the
``mcpg_audit.nl2sql_events`` table — one row per
:func:`mcpg.nl2sql.translate_nl_to_sql` call when persistence is
enabled (``MCPG_NL2SQL_AUDIT_PERSIST``).

Design highlights:

* **Backend ladder** — ``timescaledb`` → ``pg_partman`` → ``native``.
  :func:`detect_backend` picks the best one available; operators can
  pin a choice via ``MCPG_NL2SQL_AUDIT_BACKEND``. Native uses
  PostgreSQL declarative range partitioning by ``occurred_at`` with
  daily child partitions pre-created for ±7 days from now.
* **Compression** — TimescaleDB columnar via ``add_compression_policy``
  (compresses chunks older than ``MCPG_NL2SQL_AUDIT_COMPRESS_AFTER``).
  Native + pg_partman fall back to LZ4 TOAST compression on the
  large text columns (``question``, ``sql_generated``, ``error``,
  ``user_prompt``).
* **Retention** — TimescaleDB ``add_retention_policy`` (drops chunks
  older than ``MCPG_NL2SQL_AUDIT_RETENTION_DAYS``). pg_partman uses
  ``partman_drop_partition`` (the operator wires the cron). Native
  has no automatic retention — operators wire pg_cron to
  :func:`prune_nl2sql_audit_partitions` themselves.
* **RLS** — Row-Level Security is enabled by default. The owner role
  bypasses; a dedicated read-only role (``MCPG_NL2SQL_AUDIT_READER_ROLE``)
  gets a SELECT-only policy. RLS can be disabled for legacy
  single-role setups via ``MCPG_NL2SQL_AUDIT_RLS=false``.
* **Idempotent** — :func:`ensure_nl2sql_audit_table` is safe to call
  on every record; a per-driver cache + ``IF NOT EXISTS`` everywhere
  means subsequent calls cost zero round-trips.
* **Redaction** — :func:`record_nl2sql_event` runs the question, SQL,
  and error through ``obfuscate_password`` so a question that quoted
  a connection string (``"how many rows in
  postgres://user:hunter2@db/x?"``) never lands on disk in plaintext.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from os import environ
from typing import Any, Literal

from mcpg._vendor.sql import SqlDriver, obfuscate_password
from mcpg.extensions import extension_installed

logger = logging.getLogger(__name__)

AUDIT_SCHEMA = "mcpg_audit"
AUDIT_TABLE = "nl2sql_events"
_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"

# Plain unquoted identifier — same rule the rest of the codebase uses
# for catalog-bound names. Reader role / backend name go through this
# before any DDL is built.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

Backend = Literal["timescaledb", "pg_partman", "native"]
_KNOWN_BACKENDS: frozenset[str] = frozenset({"timescaledb", "pg_partman", "native"})

# Native-backend window — how many days of forward + back partitions to
# pre-create on first setup. 7 each side is enough that an operator
# missing a pg_cron run for a few days doesn't lose writes; the
# maintenance helper extends the window on each invocation.
_NATIVE_WINDOW_DAYS = 7


class NL2SQLAuditError(Exception):
    """Raised when the NL→SQL audit subsystem can't satisfy a request."""


@dataclass(frozen=True, slots=True)
class NL2SQLAuditSetupResult:
    """Outcome of :func:`ensure_nl2sql_audit_table` — what we created and how."""

    schema_created: bool
    table_created: bool
    backend: Backend
    compression_enabled: bool
    retention_days: int | None
    rls_enabled: bool
    reader_role: str | None
    setup_sql: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class NL2SQLAuditEntry:
    """A row read back from ``mcpg_audit.nl2sql_events``."""

    id: int
    occurred_at: Any
    provider: str
    model: str
    schema_arg: str
    question: str
    sql_generated: str | None
    sql_executed: bool
    row_count: int | None
    error: str | None
    duration_ms: int | None


# Per-driver cache mirroring audit_trail.ensure_audit_table — keyed by
# id(driver) so it doesn't pin objects in memory or survive test teardown.
_ENSURED_DRIVER_IDS: set[int] = set()
_ENSURE_LOCK: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _ENSURE_LOCK
    if _ENSURE_LOCK is None:
        _ENSURE_LOCK = asyncio.Lock()
    return _ENSURE_LOCK


def _reset_setup_cache() -> None:
    """Test-only escape hatch — forget which drivers have been initialised."""
    _ENSURED_DRIVER_IDS.clear()


def _check_identifier(name: str, *, kind: str) -> None:
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise NL2SQLAuditError(
            f"{kind} {name!r} is not a valid unquoted SQL identifier — must match {_IDENTIFIER.pattern}"
        )


async def detect_backend(
    driver: SqlDriver,
    *,
    forced: str | None = None,
) -> Backend:
    """Pick the best partitioning backend available on this DB.

    ``forced`` (typically ``MCPG_NL2SQL_AUDIT_BACKEND``) overrides the
    auto-detection but still validates the choice — asking for
    ``timescaledb`` on a DB that doesn't have it raises rather than
    silently downgrading. Auto-detect tries TimescaleDB first
    (columnar compression + native retention), then pg_partman
    (declarative partitioning + managed maintenance), then native
    (always available since PG 10).
    """
    if forced is not None:
        normalised = forced.strip().lower()
        if normalised not in _KNOWN_BACKENDS:
            raise NL2SQLAuditError(
                f"unknown NL→SQL audit backend {forced!r}; expected one of {sorted(_KNOWN_BACKENDS)}"
            )
        if normalised == "timescaledb" and not await extension_installed(driver, "timescaledb"):
            raise NL2SQLAuditError("MCPG_NL2SQL_AUDIT_BACKEND=timescaledb but the extension is not installed")
        if normalised == "pg_partman" and not await extension_installed(driver, "pg_partman"):
            raise NL2SQLAuditError("MCPG_NL2SQL_AUDIT_BACKEND=pg_partman but the extension is not installed")
        return normalised  # type: ignore[return-value]
    if await extension_installed(driver, "timescaledb"):
        return "timescaledb"
    if await extension_installed(driver, "pg_partman"):
        return "pg_partman"
    return "native"


def _native_partition_name(day: datetime) -> str:
    return f"{AUDIT_TABLE}_p{day.strftime('%Y%m%d')}"


def _build_native_partition_sql(day: datetime) -> str:
    """``CREATE TABLE IF NOT EXISTS`` for one daily child partition."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    child = _native_partition_name(start)
    return (
        f"CREATE TABLE IF NOT EXISTS {AUDIT_SCHEMA}.{child} "
        f"PARTITION OF {_QUALIFIED} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def _resolve_settings(
    env: Mapping[str, str] | None,
) -> tuple[str | None, int, str, str, bool, str | None]:
    """Pull NL2SQL_AUDIT_* knobs out of env. Returns
    ``(backend, retention_days, chunk_interval, compress_after, rls, reader_role)``.
    """
    source = env if env is not None else environ
    backend_raw = (source.get("MCPG_NL2SQL_AUDIT_BACKEND") or "").strip() or None
    retention_raw = (source.get("MCPG_NL2SQL_AUDIT_RETENTION_DAYS") or "").strip()
    retention_days = int(retention_raw) if retention_raw else 90
    if retention_days < 1:
        raise NL2SQLAuditError(f"MCPG_NL2SQL_AUDIT_RETENTION_DAYS must be a positive integer; got {retention_raw!r}")
    chunk_interval = (source.get("MCPG_NL2SQL_AUDIT_CHUNK_INTERVAL") or "").strip() or "1 day"
    compress_after = (source.get("MCPG_NL2SQL_AUDIT_COMPRESS_AFTER") or "").strip() or "7 days"
    rls_raw = (source.get("MCPG_NL2SQL_AUDIT_RLS") or "").strip().lower()
    rls = True if rls_raw in ("", "true", "1", "yes", "on") else False
    reader_role = (source.get("MCPG_NL2SQL_AUDIT_READER_ROLE") or "").strip() or None
    if reader_role is not None:
        _check_identifier(reader_role, kind="reader role")
    return backend_raw, retention_days, chunk_interval, compress_after, rls, reader_role


async def ensure_nl2sql_audit_table(
    driver: SqlDriver,
    *,
    env: Mapping[str, str] | None = None,
) -> NL2SQLAuditSetupResult:
    """Idempotently provision ``mcpg_audit.nl2sql_events``.

    The first call materialises:

    1. The ``mcpg_audit`` schema (``CREATE SCHEMA IF NOT EXISTS``).
    2. The parent table — partitioned by ``occurred_at`` when the
       backend is native or pg_partman; a plain table converted into a
       hypertable when TimescaleDB is selected.
    3. Compression + retention policies appropriate to the backend.
    4. RLS + reader-role policy (when ``MCPG_NL2SQL_AUDIT_RLS`` is on,
       the default).

    Subsequent calls on the same driver instance are O(1) — the cache
    short-circuits before any catalog round-trip.
    """
    if id(driver) in _ENSURED_DRIVER_IDS:
        # Cached — return a minimal result; real values were logged on
        # the first call and the table is already in steady state.
        backend, *_ = _resolve_settings(env)
        return NL2SQLAuditSetupResult(
            schema_created=False,
            table_created=False,
            backend=(backend or "native"),  # type: ignore[arg-type]
            compression_enabled=False,
            retention_days=None,
            rls_enabled=False,
            reader_role=None,
        )
    async with _get_lock():
        if id(driver) in _ENSURED_DRIVER_IDS:
            backend, *_ = _resolve_settings(env)
            return NL2SQLAuditSetupResult(
                schema_created=False,
                table_created=False,
                backend=(backend or "native"),  # type: ignore[arg-type]
                compression_enabled=False,
                retention_days=None,
                rls_enabled=False,
                reader_role=None,
            )

        forced_backend, retention_days, chunk_interval, compress_after, rls, reader_role = _resolve_settings(env)
        backend = await detect_backend(driver, forced=forced_backend)
        statements: list[str] = []

        # 1. Schema
        sql_schema = f"CREATE SCHEMA IF NOT EXISTS {AUDIT_SCHEMA}"
        await driver.execute_query(sql_schema, force_readonly=False)
        statements.append(sql_schema)

        # 2. Table — column layout is the same across all three
        # backends. The primary key is (id, occurred_at) so it's
        # compatible with declarative partitioning on occurred_at;
        # TimescaleDB requires the partition column to be part of the
        # PK as well so the same shape works there.
        partition_clause = "" if backend == "timescaledb" else " PARTITION BY RANGE (occurred_at)"
        sql_table = (
            f"CREATE TABLE IF NOT EXISTS {_QUALIFIED} ("
            "  id bigserial NOT NULL, "
            "  occurred_at timestamptz NOT NULL DEFAULT now(), "
            "  provider text NOT NULL, "
            "  model text NOT NULL, "
            "  schema_arg text NOT NULL, "
            "  question text NOT NULL, "
            "  sql_generated text, "
            "  sql_executed boolean NOT NULL DEFAULT false, "
            "  row_count integer, "
            "  error text, "
            "  duration_ms integer, "
            "  prompt_tokens integer, "
            "  completion_tokens integer, "
            "  PRIMARY KEY (id, occurred_at)"
            f"){partition_clause}"
        )
        await driver.execute_query(sql_table, force_readonly=False)
        statements.append(sql_table)

        # 3. Helpful index for "show recent" queries — partial on
        # error IS NOT NULL keeps the error-walk fast without bloating
        # the happy-path index.
        sql_idx = f"CREATE INDEX IF NOT EXISTS {AUDIT_TABLE}_occurred_at_idx ON {_QUALIFIED} (occurred_at DESC)"
        await driver.execute_query(sql_idx, force_readonly=False)
        statements.append(sql_idx)

        # 4. Backend-specific partitioning + compression + retention.
        compression_enabled = False
        if backend == "timescaledb":
            sql_ht = (
                f"SELECT create_hypertable('{_QUALIFIED}', 'occurred_at', "
                f"chunk_time_interval => INTERVAL '{chunk_interval}', "
                "if_not_exists => TRUE)"
            )
            await driver.execute_query(sql_ht, force_readonly=False)
            statements.append(sql_ht)

            sql_compress = f"ALTER TABLE {_QUALIFIED} SET (timescaledb.compress = TRUE)"
            await driver.execute_query(sql_compress, force_readonly=False)
            statements.append(sql_compress)

            # add_compression_policy raises on re-add; swallow the
            # idempotency error so subsequent setup calls stay no-ops.
            sql_compress_policy = (
                f"SELECT add_compression_policy('{_QUALIFIED}', INTERVAL '{compress_after}', if_not_exists => TRUE)"
            )
            try:
                await driver.execute_query(sql_compress_policy, force_readonly=False)
                statements.append(sql_compress_policy)
                compression_enabled = True
            except Exception as exc:  # pragma: no cover - depends on TSDB version
                logger.warning("add_compression_policy raised, continuing: %s", exc)

            sql_retention = (
                f"SELECT add_retention_policy('{_QUALIFIED}', INTERVAL '{retention_days} days', if_not_exists => TRUE)"
            )
            try:
                await driver.execute_query(sql_retention, force_readonly=False)
                statements.append(sql_retention)
            except Exception as exc:  # pragma: no cover
                logger.warning("add_retention_policy raised, continuing: %s", exc)
        elif backend == "pg_partman":
            # pg_partman is the partition manager; LZ4 TOAST compression
            # is the storage-level codec. Both are independent layers.
            sql_partman = (
                f"SELECT partman.create_parent("
                f"  p_parent_table := '{_QUALIFIED}', "
                "  p_control := 'occurred_at', "
                "  p_type := 'range', "
                f"  p_interval := '{chunk_interval}'"
                ")"
            )
            try:
                await driver.execute_query(sql_partman, force_readonly=False)
                statements.append(sql_partman)
            except Exception as exc:
                # create_parent throws when the parent already exists —
                # treat as idempotent success.
                logger.info("partman.create_parent: %s (treating as idempotent)", exc)

            # LZ4 TOAST compression on the big text columns. Available
            # PG 14+, silently ignored on older versions.
            for col in ("question", "sql_generated", "error"):
                sql_lz4 = f"ALTER TABLE {_QUALIFIED} ALTER COLUMN {col} SET COMPRESSION lz4"
                try:
                    await driver.execute_query(sql_lz4, force_readonly=False)
                    statements.append(sql_lz4)
                    compression_enabled = True
                except Exception as exc:  # PG < 14
                    logger.debug("LZ4 column compression not supported, skipping: %s", exc)
                    break
        else:  # native
            # Pre-create ±_NATIVE_WINDOW_DAYS daily child partitions so
            # writes have somewhere to land even if the operator
            # forgets to wire pg_cron. The maintenance helper extends
            # the window on every call.
            today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            for offset in range(-_NATIVE_WINDOW_DAYS, _NATIVE_WINDOW_DAYS + 1):
                day = today + timedelta(days=offset)
                sql_child = _build_native_partition_sql(day)
                await driver.execute_query(sql_child, force_readonly=False)
                statements.append(sql_child)

            for col in ("question", "sql_generated", "error"):
                sql_lz4 = f"ALTER TABLE {_QUALIFIED} ALTER COLUMN {col} SET COMPRESSION lz4"
                try:
                    await driver.execute_query(sql_lz4, force_readonly=False)
                    statements.append(sql_lz4)
                    compression_enabled = True
                except Exception as exc:  # PG < 14
                    logger.debug("LZ4 column compression not supported, skipping: %s", exc)
                    break

        # 5. RLS — enable + reader-role policy. Reader role must exist;
        # we don't create it (avoids needing CREATEROLE here). When
        # rls is on but reader_role isn't set, RLS is still enabled
        # but no policy is created — that means only the table owner
        # can read, which is the safest default for "secure access".
        if rls:
            sql_rls = f"ALTER TABLE {_QUALIFIED} ENABLE ROW LEVEL SECURITY"
            await driver.execute_query(sql_rls, force_readonly=False)
            statements.append(sql_rls)
            sql_force = f"ALTER TABLE {_QUALIFIED} FORCE ROW LEVEL SECURITY"
            await driver.execute_query(sql_force, force_readonly=False)
            statements.append(sql_force)

            if reader_role is not None:
                # CREATE POLICY has no IF NOT EXISTS; emulate with a
                # DO block that catches duplicate_object.
                policy_name = f"{AUDIT_TABLE}_reader_select"
                sql_policy = (
                    "DO $$ BEGIN "
                    f"  CREATE POLICY {policy_name} ON {_QUALIFIED} "
                    f"    FOR SELECT TO {reader_role} USING (true); "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
                await driver.execute_query(sql_policy, force_readonly=False)
                statements.append(sql_policy)

                sql_grant = f"GRANT USAGE ON SCHEMA {AUDIT_SCHEMA} TO {reader_role}"
                await driver.execute_query(sql_grant, force_readonly=False)
                statements.append(sql_grant)
                sql_grant_t = f"GRANT SELECT ON {_QUALIFIED} TO {reader_role}"
                await driver.execute_query(sql_grant_t, force_readonly=False)
                statements.append(sql_grant_t)

        _ENSURED_DRIVER_IDS.add(id(driver))
        return NL2SQLAuditSetupResult(
            schema_created=True,
            table_created=True,
            backend=backend,
            compression_enabled=compression_enabled,
            retention_days=retention_days,
            rls_enabled=rls,
            reader_role=reader_role,
            setup_sql=tuple(statements),
        )


async def extend_native_partitions(driver: SqlDriver, *, days_ahead: int = 7) -> list[str]:
    """Add forward child partitions for the native backend.

    Idempotent — re-running for the same days is a no-op (CREATE TABLE
    IF NOT EXISTS). Returns the partition names that were created (or
    already existed) so a pg_cron caller can log progress.
    """
    if days_ahead < 1:
        raise NL2SQLAuditError("days_ahead must be a positive integer")
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    created: list[str] = []
    for offset in range(days_ahead + 1):
        day = today + timedelta(days=offset)
        await driver.execute_query(_build_native_partition_sql(day), force_readonly=False)
        created.append(_native_partition_name(day))
    return created


async def record_nl2sql_event(
    driver: SqlDriver,
    *,
    provider: str,
    model: str,
    schema_arg: str,
    question: str,
    sql_generated: str | None,
    sql_executed: bool,
    row_count: int | None,
    error: str | None,
    duration_ms: int | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Persist one NL→SQL event row.

    The free-text fields (``question``, ``sql_generated``, ``error``)
    are piped through :func:`obfuscate_password` so embedded
    credentials never reach the audit table. ``ensure_nl2sql_audit_table``
    runs on first call per driver (cached afterward) so callers
    don't need a setup hook in their bootstrap.
    """
    await ensure_nl2sql_audit_table(driver, env=env)
    safe_question = obfuscate_password(question)
    safe_sql = obfuscate_password(sql_generated) if sql_generated is not None else None
    safe_error = obfuscate_password(error) if error is not None else None
    await driver.execute_query(
        f"INSERT INTO {_QUALIFIED} ("
        "  provider, model, schema_arg, question, sql_generated, sql_executed, "
        "  row_count, error, duration_ms, prompt_tokens, completion_tokens"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        params=[
            provider,
            model,
            schema_arg,
            safe_question,
            safe_sql,
            sql_executed,
            row_count,
            safe_error,
            duration_ms,
            prompt_tokens,
            completion_tokens,
        ],
        force_readonly=False,
    )


async def _audit_table_exists(driver: SqlDriver) -> bool:
    rows = await driver.execute_query(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[AUDIT_SCHEMA, AUDIT_TABLE],
        force_readonly=True,
    )
    return bool(rows)


async def list_nl2sql_events(
    driver: SqlDriver,
    *,
    limit: int = 100,
    provider: str | None = None,
) -> list[NL2SQLAuditEntry]:
    """Read recent NL→SQL audit rows, newest first.

    Returns ``[]`` when the table doesn't exist yet (no events ever
    written — preferable to a hard error since the caller may be
    polling at startup).
    """
    if limit < 1:
        raise NL2SQLAuditError("limit must be a positive integer")
    if not await _audit_table_exists(driver):
        return []
    where = ""
    params: list[Any] = []
    if provider is not None:
        where = "WHERE provider = %s "
        params.append(provider)
    params.append(limit)
    rows = await driver.execute_query(
        f"SELECT id, occurred_at, provider, model, schema_arg, question, "
        f"  sql_generated, sql_executed, row_count, error, duration_ms "
        f"FROM {_QUALIFIED} {where}ORDER BY occurred_at DESC LIMIT %s",
        params=params,
        force_readonly=True,
    )
    return [
        NL2SQLAuditEntry(
            id=row.cells["id"],
            occurred_at=row.cells["occurred_at"],
            provider=row.cells["provider"],
            model=row.cells["model"],
            schema_arg=row.cells["schema_arg"],
            question=row.cells["question"],
            sql_generated=row.cells["sql_generated"],
            sql_executed=row.cells["sql_executed"],
            row_count=row.cells["row_count"],
            error=row.cells["error"],
            duration_ms=row.cells["duration_ms"],
        )
        for row in rows or []
    ]
