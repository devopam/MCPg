"""Staged-migration workflow — Batch F / Phase 27 per ADR-0006.

``prepare_migration`` clones a target schema's structure into a shadow
schema, applies a candidate SQL statement (or batch) against the
shadow, runs :func:`mcpg.schema_diff.compare_schemas` against the
target, and persists the staged migration in ``mcpg_migrations.staged``.
The agent reviews the structural diff. ``complete_migration`` then
applies the same candidate SQL to the target and drops the shadow;
``cancel_migration`` drops the shadow without applying.

Per ADR-0006, the shadow strategy is **same-database**: a sibling
schema cloned via the existing introspection (no ``pg_dump`` shell-out,
no cross-batch dependency on Batch D). Cross-schema references in
table or constraint definitions stay pointing at the original (or are
rewritten to the shadow for FKs whose target is the same schema),
which is a documented limitation for v1.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import describe_table, list_constraints, list_indexes, list_tables
from mcpg.schema_diff import SchemaDiff, compare_schemas

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHADOW_PREFIX = "mcpg_shadow_"
_NAME_SUFFIX_RE = re.compile(r"[^A-Za-z0-9_]")


class MigrationError(Exception):
    """Raised when a migration tool call is rejected or fails."""


@dataclass(frozen=True)
class MigrationRecord:
    """A row from ``mcpg_migrations.staged``.

    ``status`` is one of ``prepared``, ``completed``, ``cancelled``,
    ``expired``. ``expired`` is set lazily when a prepared migration's
    ``ttl_expires_at`` has passed — the sweeper drops the shadow and
    flips the row.
    """

    id: str
    prepared_at: datetime
    target_schema: str
    shadow_schema: str
    candidate_sql: str
    status: str
    ttl_expires_at: datetime
    completed_at: datetime | None = None


@dataclass(frozen=True)
class PrepareResult:
    """The outcome of :func:`prepare_migration`.

    ``diff`` is the structural diff between the target schema (before
    the migration) and the shadow (after the candidate SQL was
    applied). An agent reviewing the diff sees exactly the changes
    that ``complete_migration`` will land on the target.
    """

    id: str
    target_schema: str
    shadow_schema: str
    ttl_expires_at: datetime
    diff: SchemaDiff


@dataclass(frozen=True)
class CompleteResult:
    """The outcome of :func:`complete_migration`."""

    id: str
    target_schema: str
    completed_at: datetime
    statements_run: int


@dataclass(frozen=True)
class CancelResult:
    """The outcome of :func:`cancel_migration`."""

    id: str
    shadow_dropped: bool


@dataclass(frozen=True)
class TableSampleStat:
    """Per-table sampling stats for a validation run.

    ``rows_before`` is the count in the sampled shadow before the
    candidate SQL ran; ``rows_after`` is the count after. A drop is
    not by itself a failure (DELETE can be intentional), but a large
    unexpected drop is worth surfacing — agents diff the two.
    """

    table: str
    rows_before: int
    rows_after: int


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of :func:`validate_migration`.

    Reports whether the candidate SQL applied cleanly to a sampled
    copy of ``target_schema``'s rows and how each table's row count
    moved. ``error`` is non-empty iff the candidate raised — the
    transient shadow has already been dropped.
    """

    target_schema: str
    sample_rows_per_table: int
    tables_sampled: int
    table_stats: list[TableSampleStat]
    candidate_applied: bool
    error: str | None


@dataclass(frozen=True)
class MigrationSchemaValidationResult:
    """The outcome of :func:`validate_migration_schema`."""

    target_schema: str
    reference_schema: str
    applied: bool
    error: str | None
    diff: SchemaDiff | None


# --- table bootstrap -------------------------------------------------


_ENSURE_STATE_SQL = """
CREATE SCHEMA IF NOT EXISTS mcpg_migrations;
CREATE TABLE IF NOT EXISTS mcpg_migrations.staged (
    id text PRIMARY KEY,
    prepared_at timestamptz NOT NULL DEFAULT now(),
    target_schema text NOT NULL,
    shadow_schema text NOT NULL,
    candidate_sql text NOT NULL,
    status text NOT NULL DEFAULT 'prepared',
    ttl_expires_at timestamptz NOT NULL,
    completed_at timestamptz
);
"""


# Cache so we don't pay the round-trip on every call. Keyed by the
# driver instance — mirrors the pattern in mcpg.audit_trail.
_ensure_cache: set[int] = set()


async def _ensure_state_table(driver: SqlDriver) -> None:
    key = id(driver)
    if key in _ensure_cache:
        return
    await driver.execute_query(_ENSURE_STATE_SQL)
    _ensure_cache.add(key)


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise MigrationError(f"invalid {kind} name: {name!r}")


def _make_migration_id(name: str) -> str:
    """Build a unique migration id from the caller-supplied name + timestamp.

    The slice for the user-supplied portion is sized so the derived
    shadow schema name (``mcpg_shadow_<id>``) stays under PostgreSQL's
    63-byte NAMEDATALEN limit — otherwise CREATE SCHEMA would silently
    truncate and downstream lookups by the un-truncated name would
    fail.

    Budget: NAMEDATALEN=63. Reserved: ``mcpg_shadow_`` (12) +
    ``_<ms-timestamp>`` (1+13=14). User portion: 63 - 12 - 14 = 37.
    """
    # Strip non-identifier chars from the name so the id stays
    # opaque-but-readable (and so the derived shadow schema name is a
    # plain identifier).
    safe = _NAME_SUFFIX_RE.sub("_", name).strip("_")[:_USER_NAME_MAX] or "migration"
    return f"{safe}_{int(time.time() * 1000)}"


# Keep the shadow schema name under PG's 63-byte identifier limit.
# See _make_migration_id for the budget calculation.
_USER_NAME_MAX = 37


def _shadow_name_for(migration_id: str) -> str:
    return f"{_SHADOW_PREFIX}{migration_id}"


# --- DDL replay ------------------------------------------------------


async def _replay_target_into_shadow(driver: SqlDriver, target_schema: str, shadow_schema: str) -> None:
    """Replicate ``target_schema``'s structure into ``shadow_schema``.

    Replays plain (non-partitioned, non-view) tables with their
    columns, then their primary keys, unique constraints, check
    constraints, foreign keys, and indexes. Cross-schema FK targets
    that point to ``target_schema`` are rewritten to point at the
    shadow so intra-schema FKs survive the clone; FKs whose target
    sits in another schema are left pointing at the original, which
    the Phase-18 diff will surface as "removed" — a noisy but correct
    signal documented in ADR-0006.
    """
    tables = await list_tables(driver, target_schema)
    # information_schema reports ``BASE TABLE`` for ordinary tables;
    # ``VIEW`` / ``FOREIGN`` / etc. for the others. Replay covers only
    # the base tables in v1.
    plain_tables = [t for t in tables if t.type == "BASE TABLE" and not t.is_partition]
    # 1. Create empty tables with columns first so FKs (which can
    #    reference any of them) can land in pass 2.
    for table in plain_tables:
        columns = await describe_table(driver, target_schema, table.name)
        if not columns:
            continue
        column_clauses = [_column_clause(col) for col in columns]
        ddl = f'CREATE TABLE "{shadow_schema}"."{table.name}" ({", ".join(column_clauses)})'
        await driver.execute_query(ddl)

    # 2. Add constraints + indexes. PK first so unique-constraint /
    #    FK errors don't shadow it.
    for table in plain_tables:
        constraints = await list_constraints(driver, target_schema, table.name)
        for con in sorted(constraints, key=_constraint_sort_key):
            # Only rewrite schema references for FOREIGN KEYs — those
            # are the only constraints whose definition can name another
            # table by schema. CHECK / UNIQUE / PK definitions may
            # legitimately contain string literals that happen to start
            # with the target schema name (e.g. CHECK (path LIKE
            # 'public.%')) which a blanket regex sub would corrupt.
            if con.type == "foreign_key":
                definition = _rewrite_schema_reference(con.definition, target_schema, shadow_schema)
            else:
                definition = con.definition
            await driver.execute_query(
                f'ALTER TABLE "{shadow_schema}"."{table.name}" ADD CONSTRAINT "{con.name}" {definition}'
            )
        indexes = await list_indexes(driver, target_schema, table.name)
        for idx in indexes:
            # pg_get_indexdef returns a full CREATE INDEX statement;
            # rewrite the target.table reference to the shadow.
            rewritten = _rewrite_index_definition(idx.definition, target_schema, shadow_schema)
            # PK / unique indexes are created implicitly by the
            # ADD CONSTRAINT pass above; skip them here.
            if any(c.name == idx.name for c in constraints):
                continue
            await driver.execute_query(rewritten)


def _column_clause(column: Any) -> str:
    """Build a CREATE TABLE column clause from a ``ColumnInfo``-like object."""
    nullable = "" if column.nullable else " NOT NULL"
    default = f" DEFAULT {column.default}" if column.default is not None else ""
    return f'"{column.name}" {column.data_type}{nullable}{default}'


_CONSTRAINT_ORDER = {"primary_key": 0, "unique": 1, "check": 2, "foreign_key": 3, "exclusion": 4}


def _constraint_sort_key(constraint: Any) -> tuple[int, str]:
    return (_CONSTRAINT_ORDER.get(constraint.type, 9), constraint.name)


def _rewrite_schema_reference(definition: str, target: str, shadow: str) -> str:
    """Rewrite ``REFERENCES target.table`` → ``REFERENCES shadow.table``.

    Works on the constraint-clause output of ``pg_get_constraintdef()``
    so an intra-schema FK survives the clone. Cross-schema FKs that
    don't mention ``target`` are left untouched (and will surface in
    the diff as removed/changed).
    """
    return re.sub(rf'(?<![A-Za-z0-9_])"?{re.escape(target)}"?\.', f'"{shadow}".', definition)


def _rewrite_index_definition(definition: str, target: str, shadow: str) -> str:
    """Rewrite ``CREATE INDEX ... ON target.table`` to use the shadow schema."""
    return re.sub(rf'(?<![A-Za-z0-9_])"?{re.escape(target)}"?\.', f'"{shadow}".', definition)


# --- prepare / complete / cancel ------------------------------------


async def prepare_migration(
    driver: SqlDriver,
    *,
    name: str,
    target_schema: str,
    candidate_sql: str,
    ttl_minutes: int = 60,
) -> PrepareResult:
    """Stage a candidate migration against a shadow of ``target_schema``.

    Replicates ``target_schema``'s structure into a fresh shadow
    schema, applies ``candidate_sql`` there, then diffs shadow vs
    target via :func:`mcpg.schema_diff.compare_schemas` so the agent
    can review the structural delta before calling
    :func:`complete_migration`.

    Raises:
        MigrationError: When inputs fail validation, the DDL replay
            fails, or the candidate SQL fails when applied to the
            shadow. On replay/apply failure the shadow is dropped so
            the next prepare doesn't trip over its leftovers.
    """
    _check_identifier(target_schema, "target_schema")
    if not name.strip():
        raise MigrationError("name must not be empty")
    if not candidate_sql.strip():
        raise MigrationError("candidate_sql must not be empty")
    if ttl_minutes < 1:
        raise MigrationError("ttl_minutes must be at least 1")

    await _ensure_state_table(driver)
    await _sweep_expired(driver)

    migration_id = _make_migration_id(name)
    shadow_schema = _shadow_name_for(migration_id)
    _check_identifier(shadow_schema, "shadow_schema")
    ttl_expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)

    try:
        await driver.execute_query(f'CREATE SCHEMA "{shadow_schema}"')
        await _replay_target_into_shadow(driver, target_schema, shadow_schema)
        # Apply the candidate against the shadow. SET LOCAL inside a
        # single transactional connection so unqualified identifiers in
        # the candidate SQL resolve against the shadow — without
        # leaking the search_path back into the pool after the call.
        await _execute_in_schema(driver, shadow_schema, candidate_sql)
        diff = await compare_schemas(driver, target_schema, shadow_schema)
    except Exception:
        # The shadow is half-built; drop it so we don't accumulate
        # orphaned schemas across failed prepares.
        try:
            await driver.execute_query(f'DROP SCHEMA IF EXISTS "{shadow_schema}" CASCADE')
        except Exception:
            pass
        raise

    # Persist the staged row. INSERT through a parametrised statement so
    # the migration id / sql / schema names cannot inject.
    await driver.execute_query(
        "INSERT INTO mcpg_migrations.staged "
        "(id, target_schema, shadow_schema, candidate_sql, status, ttl_expires_at) "
        "VALUES (%s, %s, %s, %s, 'prepared', %s)",
        [migration_id, target_schema, shadow_schema, candidate_sql, ttl_expires_at],
    )

    return PrepareResult(
        id=migration_id,
        target_schema=target_schema,
        shadow_schema=shadow_schema,
        ttl_expires_at=ttl_expires_at,
        diff=diff,
    )


async def complete_migration(driver: SqlDriver, migration_id: str) -> CompleteResult:
    """Apply a prepared migration's candidate SQL to the target schema.

    Refuses if the migration is not in ``prepared`` status or its TTL
    has expired. The candidate SQL runs against the target with
    ``SET search_path`` pointed at the target schema so unqualified
    identifiers resolve where the agent expects.

    Raises:
        MigrationError: When ``migration_id`` doesn't exist, is not in
            ``prepared`` status, has expired, or the candidate SQL
            fails on the target.
    """
    await _ensure_state_table(driver)
    record = await _load_record(driver, migration_id)
    if record is None:
        raise MigrationError(f"migration {migration_id!r} not found")
    if record.status != "prepared":
        raise MigrationError(f"migration {migration_id!r} is not in 'prepared' status (got {record.status!r})")
    if record.ttl_expires_at <= datetime.now(UTC):
        raise MigrationError(f"migration {migration_id!r} has expired; cancel and prepare a new one")

    await _execute_in_schema(driver, record.target_schema, record.candidate_sql)

    # Drop the shadow now that we've applied to target.
    await driver.execute_query(f'DROP SCHEMA IF EXISTS "{record.shadow_schema}" CASCADE')
    completed_at = datetime.now(UTC)
    await driver.execute_query(
        "UPDATE mcpg_migrations.staged SET status='completed', completed_at=%s WHERE id=%s",
        [completed_at, migration_id],
    )
    return CompleteResult(
        id=migration_id,
        target_schema=record.target_schema,
        completed_at=completed_at,
        statements_run=1,
    )


async def cancel_migration(driver: SqlDriver, migration_id: str) -> CancelResult:
    """Drop a prepared migration's shadow and mark it cancelled.

    Idempotent on the shadow drop. Returns ``shadow_dropped=False``
    when the migration row didn't exist (so the caller can distinguish
    a real cancel from a no-op).
    """
    await _ensure_state_table(driver)
    record = await _load_record(driver, migration_id)
    if record is None:
        return CancelResult(id=migration_id, shadow_dropped=False)
    await driver.execute_query(f'DROP SCHEMA IF EXISTS "{record.shadow_schema}" CASCADE')
    await driver.execute_query(
        "UPDATE mcpg_migrations.staged SET status='cancelled' WHERE id=%s AND status='prepared'",
        [migration_id],
    )
    return CancelResult(id=migration_id, shadow_dropped=True)


async def list_pending_migrations(driver: SqlDriver) -> list[MigrationRecord]:
    """List migrations in ``prepared`` status, newest first.

    Sweeps expired entries first so the result reflects what the
    caller can actually complete.
    """
    await _ensure_state_table(driver)
    await _sweep_expired(driver)
    rows = await driver.execute_query(
        "SELECT id, prepared_at, target_schema, shadow_schema, candidate_sql, status, "
        "ttl_expires_at, completed_at "
        "FROM mcpg_migrations.staged WHERE status='prepared' ORDER BY prepared_at DESC",
        force_readonly=True,
    )
    return [_row_to_record(row.cells) for row in rows or []]


# --- internals ------------------------------------------------------


# Statements that PostgreSQL refuses to run inside a transaction block.
# We wrap candidate SQL in a transaction (for SET LOCAL search_path), so
# we have to refuse these up-front with a clear message rather than let
# PG raise an opaque error mid-migration.
_NON_TRANSACTIONAL_SQL = re.compile(
    r"\b(?:"
    r"CREATE\s+INDEX\s+CONCURRENTLY"
    r"|DROP\s+INDEX\s+CONCURRENTLY"
    r"|REINDEX\s+\w+\s+CONCURRENTLY"
    r"|VACUUM"
    r"|ALTER\s+SYSTEM"
    r"|CREATE\s+DATABASE"
    r"|DROP\s+DATABASE"
    r"|ALTER\s+DATABASE\s+\S+\s+SET\s+TABLESPACE"
    r")\b",
    re.IGNORECASE,
)


async def _execute_in_schema(driver: SqlDriver, schema: str, sql: str) -> None:
    """Run ``sql`` with ``SET LOCAL search_path = schema, public`` on one connection.

    The vendored ``SqlDriver`` gives a fresh pool connection per
    ``execute_query`` call, so a standalone ``SET search_path`` would
    only affect that one call and be invisible to the next. This
    helper reaches through to the underlying pool, opens one
    connection, and runs both the ``SET LOCAL`` and the candidate SQL
    inside a single transaction so the search_path lives exactly as
    long as the candidate SQL — no leakage back into the pool.

    Multi-statement candidate SQL is supported via
    ``cursor.execute`` (psycopg drives ``cursor.nextset()`` to walk
    the result sets).

    Raises:
        MigrationError: When ``sql`` contains a statement that PG
            refuses to run inside a transaction block (CREATE INDEX
            CONCURRENTLY, VACUUM, ALTER SYSTEM, ...). The staged-
            migration workflow always runs candidates transactionally
            so the user gets atomic apply-or-rollback; these
            statements need a different tool.
    """
    if _NON_TRANSACTIONAL_SQL.search(sql):
        raise MigrationError(
            "candidate_sql uses a statement that cannot run inside a transaction "
            "block (e.g. CREATE INDEX CONCURRENTLY, VACUUM, ALTER SYSTEM). The "
            "staged-migration workflow always runs candidates transactionally; "
            "run these statements directly via run_ddl outside this tool."
        )
    if not getattr(driver, "is_pool", False):
        # Direct-connection mode is a test/edge path; run SET + SQL on it.
        async with driver.conn.cursor() as cur:
            await cur.execute(f'SET LOCAL search_path TO "{schema}", public')
            await cur.execute(sql)
        return
    pool_obj = await driver.conn.pool_connect()
    async with pool_obj.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f'SET LOCAL search_path TO "{schema}", public')
            await cur.execute(sql)


async def _load_record(driver: SqlDriver, migration_id: str) -> MigrationRecord | None:
    rows = await driver.execute_query(
        "SELECT id, prepared_at, target_schema, shadow_schema, candidate_sql, status, "
        "ttl_expires_at, completed_at FROM mcpg_migrations.staged WHERE id=%s",
        [migration_id],
        force_readonly=True,
    )
    if not rows:
        return None
    return _row_to_record(rows[0].cells)


def _row_to_record(cells: dict[str, Any]) -> MigrationRecord:
    return MigrationRecord(
        id=cells["id"],
        prepared_at=cells["prepared_at"],
        target_schema=cells["target_schema"],
        shadow_schema=cells["shadow_schema"],
        candidate_sql=cells["candidate_sql"],
        status=cells["status"],
        ttl_expires_at=cells["ttl_expires_at"],
        completed_at=cells["completed_at"],
    )


# --- validate_migration (Phase 9.2) --------------------------------------


# Default sample size — small enough to keep the validation cheap on a
# large table, large enough that constraint violations on common data
# shapes have a chance to surface. Tunable per call.
DEFAULT_VALIDATION_SAMPLE_ROWS = 100


def _validation_shadow_name() -> str:
    """Generate a transient shadow name for a validation run.

    Distinct from ``_shadow_name_for`` so a concurrent prepare and a
    validation can't collide on the same shadow.
    """
    suffix = uuid.uuid4().hex[:12]
    return f"mcpg_validate_{suffix}"


async def _count_rows(driver: SqlDriver, schema: str, table: str) -> int:
    """Read the live row count of ``schema.table``. Schema + table are
    pre-validated by the caller (no untrusted identifier reaches this
    function), so the inline interpolation is safe."""
    rows = await driver.execute_query(
        f'SELECT COUNT(*) AS n FROM "{schema}"."{table}"',
        force_readonly=True,
    )
    if not rows:
        return 0
    return int(rows[0].cells["n"])


async def validate_migration(
    driver: SqlDriver,
    *,
    target_schema: str,
    candidate_sql: str,
    sample_rows_per_table: int = DEFAULT_VALIDATION_SAMPLE_ROWS,
) -> ValidationResult:
    """Apply ``candidate_sql`` to a transient sample of ``target_schema``.

    Unlike :func:`prepare_migration` (which clones structure only and
    persists the shadow under a TTL), this validates the candidate
    against actual rows: a transient shadow is created, up to
    ``sample_rows_per_table`` rows are copied from each base table in
    the target, the candidate SQL is applied, and per-table row counts
    are captured. The shadow is **always** dropped before returning —
    success or failure — so the function leaves no persistent state.

    Use this to catch the classes of failures a structural diff misses:

    * adding ``NOT NULL`` to a column with existing NULLs,
    * tightening a CHECK constraint that some live rows already violate,
    * type narrowings (text → integer) that fail on real values,
    * triggers whose body errors against actual data.

    Result reports per-table ``rows_before`` and ``rows_after`` so a
    DELETE-shaped candidate's effect is visible. When the candidate
    fails, ``error`` carries the database error message and
    ``candidate_applied`` is ``False``.
    """
    _check_identifier(target_schema, "target_schema")
    if not candidate_sql.strip():
        raise MigrationError("candidate_sql must not be empty")
    if sample_rows_per_table < 0:
        raise MigrationError("sample_rows_per_table must be >= 0")

    shadow_schema = _validation_shadow_name()
    _check_identifier(shadow_schema, "shadow_schema")

    table_stats: list[TableSampleStat] = []
    candidate_applied = False
    error: str | None = None

    try:
        await driver.execute_query(f'CREATE SCHEMA "{shadow_schema}"')
        await _replay_target_into_shadow(driver, target_schema, shadow_schema)

        # Sample rows. We iterate the base tables in dependency-light
        # order (the structure replay already created them) but skip
        # FK-cascade ordering — the candidate SQL is what we're testing,
        # so insertion-order mismatches that violate FKs are themselves
        # a finding the agent should see.
        tables = await list_tables(driver, target_schema)
        plain_tables = [t for t in tables if t.type == "BASE TABLE" and not t.is_partition]
        for table in plain_tables:
            _check_identifier(table.name, "table")
            if sample_rows_per_table == 0:
                continue
            # INSERT INTO shadow.table SELECT * FROM target.table LIMIT N.
            # We catch FK-violation errors per-table so one bad table
            # doesn't abort the whole validation — the agent gets a
            # complete picture instead.
            try:
                await driver.execute_query(
                    f'INSERT INTO "{shadow_schema}"."{table.name}" '
                    f'SELECT * FROM "{target_schema}"."{table.name}" LIMIT %s',
                    params=[sample_rows_per_table],
                )
            except Exception:
                # Sampling failure (FK violation, etc.) is recorded as
                # zero rows for that table; the validation continues.
                pass

        # Snapshot counts before the candidate runs.
        before_counts: dict[str, int] = {}
        for table in plain_tables:
            before_counts[table.name] = await _count_rows(driver, shadow_schema, table.name)

        # Apply the candidate. _execute_in_schema sets search_path so
        # unqualified identifiers resolve against the shadow.
        try:
            await _execute_in_schema(driver, shadow_schema, candidate_sql)
            candidate_applied = True
        except Exception as exc:
            error = str(exc)

        # Snapshot after, regardless of candidate outcome — the agent
        # may want to see counts even on failure (partial application).
        for table in plain_tables:
            try:
                after = await _count_rows(driver, shadow_schema, table.name)
            except Exception:
                # Table may have been dropped by the candidate.
                after = 0
            table_stats.append(
                TableSampleStat(
                    table=table.name,
                    rows_before=before_counts.get(table.name, 0),
                    rows_after=after,
                )
            )
    finally:
        try:
            await driver.execute_query(f'DROP SCHEMA IF EXISTS "{shadow_schema}" CASCADE')
        except Exception:
            pass

    return ValidationResult(
        target_schema=target_schema,
        sample_rows_per_table=sample_rows_per_table,
        tables_sampled=len(table_stats),
        table_stats=table_stats,
        candidate_applied=candidate_applied,
        error=error,
    )


async def _sweep_expired(driver: SqlDriver) -> None:
    """Drop shadows of any prepared migration whose TTL has passed.

    Runs as part of every ``prepare`` and ``list`` call so an idle
    server eventually reaps abandoned shadows without needing a
    background sweeper task. Failures on individual drops are
    swallowed so one orphan can't block the others.
    """
    rows = await driver.execute_query(
        "SELECT id, shadow_schema FROM mcpg_migrations.staged WHERE status='prepared' AND ttl_expires_at <= now()",
        force_readonly=True,
    )
    for row in rows or []:
        shadow = row.cells["shadow_schema"]
        try:
            await driver.execute_query(f'DROP SCHEMA IF EXISTS "{shadow}" CASCADE')
        except Exception:
            continue
        await driver.execute_query(
            "UPDATE mcpg_migrations.staged SET status='expired' WHERE id=%s",
            [row.cells["id"]],
        )


async def validate_migration_schema(
    driver: SqlDriver,
    *,
    target_schema: str,
    reference_schema: str,
    candidate_sql: str,
) -> MigrationSchemaValidationResult:
    """Clone target_schema structure, apply candidate_sql, and diff against reference_schema.

    A transient shadow schema is created, target_schema structure is replayed,
    candidate_sql is applied, and the resulting shadow is compared against
    reference_schema using compare_schemas. The shadow is always dropped before
    returning.

    If SQL application fails, applied is False and error holds the database exception
    message; the shadow is still dropped safely.
    """
    _check_identifier(target_schema, "target_schema")
    _check_identifier(reference_schema, "reference_schema")
    if not candidate_sql.strip():
        raise MigrationError("candidate_sql must not be empty")

    shadow_schema = _validation_shadow_name()
    _check_identifier(shadow_schema, "shadow_schema")

    applied = False
    error: str | None = None
    diff: SchemaDiff | None = None

    try:
        await driver.execute_query(f'CREATE SCHEMA "{shadow_schema}"')
        await _replay_target_into_shadow(driver, target_schema, shadow_schema)
        try:
            await _execute_in_schema(driver, shadow_schema, candidate_sql)
            applied = True
        except Exception as exc:
            error = str(exc)

        if applied:
            diff = await compare_schemas(driver, reference_schema, shadow_schema)
    finally:
        try:
            await driver.execute_query(f'DROP SCHEMA IF EXISTS "{shadow_schema}" CASCADE')
        except Exception:
            pass

    return MigrationSchemaValidationResult(
        target_schema=target_schema,
        reference_schema=reference_schema,
        applied=applied,
        error=error,
        diff=diff,
    )
