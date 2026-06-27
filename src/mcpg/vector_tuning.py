"""pgvector index-tuning advisors.

Two read-only tools that help agents make sensible pgvector choices:

* ``tune_vector_index`` recommends ``ivfflat`` or ``hnsw`` parameters
  from the live row count and column dimension, and emits a ready-to-run
  ``CREATE INDEX`` snippet.
* ``vector_recall_at_k`` probes an existing pgvector index by comparing
  its top-k results against a brute-force ground truth for the same
  query vectors, reporting mean recall@k.

Both require the ``vector`` extension; both raise :class:`VectorTuningError`
when it is absent.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed
from mcpg.introspection import describe_table

# pgvector supports these index access methods today; the allowlist
# guards against arbitrary identifier injection in CREATE INDEX text.
_INDEX_TYPES = frozenset({"ivfflat", "hnsw"})

# Per pgvector docs: distance functions bypass the index in the planner,
# giving us a reliable brute-force baseline without SET LOCAL gymnastics.
_DISTANCE_FUNCTIONS = {"l2": "l2_distance", "cosine": "cosine_distance", "inner_product": "inner_product"}
# Their operator counterparts trigger the ANN index when one exists.
_DISTANCE_OPERATORS = {"l2": "<->", "cosine": "<=>", "inner_product": "<#>"}

# Recall sampling triggers 2N+1 queries per call; cap N to prevent
# accidental DoS via a runaway sample_size argument.
_MAX_SAMPLE_SIZE = 100

# Plain unquoted PostgreSQL identifier — letters, digits, underscores,
# starting with a letter or underscore. We refuse anything that requires
# quoting at the catalog level (delimited identifiers, case-sensitive
# names) rather than try to parse them out of an agent's string.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class VectorTuningError(Exception):
    """Raised when a pgvector tuning operation cannot complete."""


def _quoted(name: str, kind: str) -> str:
    """Validate a SQL identifier against the allowlist and return it double-quoted."""
    if not _IDENTIFIER.match(name):
        raise VectorTuningError(f"invalid {kind} name: {name!r}")
    return f'"{name}"'


@dataclass(frozen=True)
class TuningRecommendation:
    """A recommended pgvector index configuration."""

    index_type: str
    parameters: dict[str, int]
    rationale: str
    create_index_sql: str
    row_count: int
    dimension: int


@dataclass(frozen=True)
class RecallReport:
    """Recall@k probed against an existing pgvector index."""

    metric: str
    k: int
    sample_size: int
    mean_recall: float


async def _ensure_installed(driver: SqlDriver) -> None:
    if not await extension_installed(driver, "vector"):
        raise VectorTuningError("vector extension is not installed in this database")


async def _row_count(driver: SqlDriver, schema: str, table: str) -> int:
    # schema/table are matched against the catalog via parameters, so no
    # quoting is needed here — _quoted is applied only where identifiers
    # are interpolated into SQL text.
    rows = await driver.execute_query(
        # Catalog estimate from pg_class.reltuples — accurate enough for
        # tuning heuristics and orders of magnitude faster than COUNT(*).
        "SELECT GREATEST(c.reltuples, 0)::bigint AS estimate "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[schema, table],
        force_readonly=True,
    )
    if not rows:
        raise VectorTuningError(f"table {schema}.{table} not found")
    return int(rows[0].cells["estimate"])


async def _column_dimension(driver: SqlDriver, schema: str, table: str, column: str) -> int:
    columns = await describe_table(driver, schema, table)
    for info in columns:
        if info.name == column:
            if info.vector_dimension is None:
                raise VectorTuningError(f"column {schema}.{table}.{column} is not a pgvector vector(N)")
            return info.vector_dimension
    raise VectorTuningError(f"column {schema}.{table}.{column} not found")


def _recommend_ivfflat(row_count: int) -> tuple[dict[str, int], str]:
    # pgvector docs: start with rows/1000 up to 1M rows, sqrt(rows)
    # above 1M. Clamp at 100 — fewer lists than that gives little
    # benefit over a seqscan.
    if row_count > 1_000_000:
        lists = max(100, int(math.sqrt(row_count)))
        rationale = f"row_count={row_count:,} > 1M → lists = sqrt(rows) ≈ {lists}"
    else:
        lists = max(100, row_count // 1000)
        rationale = f"row_count={row_count:,} → lists = rows/1000, floored at 100 = {lists}"
    return {"lists": lists}, rationale


def _recommend_hnsw(row_count: int) -> tuple[dict[str, int], str]:
    # m controls graph degree (memory + recall trade-off); ef_construction
    # controls build-time quality. Both ramp with size.
    m = 16 if row_count <= 1_000_000 else 24
    ef_construction = 64 if row_count <= 100_000 else 128
    m_note = "baseline" if m == 16 else "denser graph for >1M rows"
    ef_note = "default" if ef_construction == 64 else "wider candidate pool for >100k rows"
    rationale = f"row_count={row_count:,} → m={m} ({m_note}), ef_construction={ef_construction} ({ef_note})"
    return {"m": m, "ef_construction": ef_construction}, rationale


def _format_create_index(
    index_type: str, schema: str, table: str, column: str, ops: str, parameters: dict[str, int]
) -> str:
    params_text = ", ".join(f"{name} = {value}" for name, value in parameters.items())
    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    return f"CREATE INDEX ON {relation} USING {index_type} ({col} {ops}) WITH ({params_text});"


_DEFAULT_OPS_FOR_METRIC = {"l2": "vector_l2_ops", "cosine": "vector_cosine_ops", "inner_product": "vector_ip_ops"}


async def tune_vector_index(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    *,
    index_type: str = "hnsw",
    metric: str = "l2",
) -> TuningRecommendation:
    """Recommend an ``ivfflat`` or ``hnsw`` configuration for a vector column.

    Reads the live row count (from ``pg_class.reltuples``) and column
    dimension, applies the standard pgvector heuristics, and returns the
    parameters plus a ready-to-run ``CREATE INDEX`` statement.

    Raises:
        VectorTuningError: pgvector is not installed, ``index_type`` or
            ``metric`` is not in the allowlist, or the column is not a
            ``vector(N)`` column.
    """
    await _ensure_installed(driver)
    if index_type not in _INDEX_TYPES:
        raise VectorTuningError(f"unsupported index_type {index_type!r}; expected one of {sorted(_INDEX_TYPES)}")
    if metric not in _DEFAULT_OPS_FOR_METRIC:
        raise VectorTuningError(f"unknown metric {metric!r}; expected l2, cosine, or inner_product")

    row_count = await _row_count(driver, schema, table)
    dimension = await _column_dimension(driver, schema, table, column)

    if index_type == "ivfflat":
        parameters, rationale = _recommend_ivfflat(row_count)
    else:
        parameters, rationale = _recommend_hnsw(row_count)

    ops = _DEFAULT_OPS_FOR_METRIC[metric]
    sql = _format_create_index(index_type, schema, table, column, ops, parameters)
    return TuningRecommendation(
        index_type=index_type,
        parameters=parameters,
        rationale=rationale,
        create_index_sql=sql,
        row_count=row_count,
        dimension=dimension,
    )


async def vector_recall_at_k(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    id_column: str,
    *,
    k: int = 10,
    sample_size: int = 20,
    metric: str = "l2",
) -> RecallReport:
    """Measure recall@k of an existing pgvector index against brute-force truth.

    Picks ``sample_size`` rows in id order, treats each row's vector as
    a query, runs the top-k search through the index (operator form,
    which the planner routes to the ANN index) and via the brute-force
    function form (``l2_distance`` / ``cosine_distance`` / ``inner_product``
    — pgvector documents these as non-indexed alternatives), and reports
    the mean overlap.

    Raises:
        VectorTuningError: pgvector is not installed, ``metric`` is
            unknown, or the table / id column does not exist.
    """
    await _ensure_installed(driver)
    if metric not in _DISTANCE_OPERATORS:
        raise VectorTuningError(f"unknown metric {metric!r}; expected l2, cosine, or inner_product")
    if k <= 0 or sample_size <= 0:
        raise VectorTuningError("k and sample_size must be positive")
    if sample_size > _MAX_SAMPLE_SIZE:
        # Each probed row triggers two extra catalog queries; cap to
        # keep an over-eager caller from accidentally DoSing the DB.
        raise VectorTuningError(f"sample_size cannot exceed {_MAX_SAMPLE_SIZE}")

    operator = _DISTANCE_OPERATORS[metric]
    function = _DISTANCE_FUNCTIONS[metric]
    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    id_col = _quoted(id_column, "id_column")

    sample_rows = await driver.execute_query(
        f"SELECT {id_col} AS id, {col}::text AS vec FROM {relation} WHERE {col} IS NOT NULL ORDER BY {id_col} LIMIT %s",
        params=[sample_size],
        force_readonly=True,
    )
    samples = sample_rows or []
    if not samples:
        return RecallReport(metric=metric, k=k, sample_size=0, mean_recall=0.0)

    recalls: list[float] = []
    for sample in samples:
        query_vec = sample.cells["vec"]
        ann_rows = await driver.execute_query(
            f"SELECT {id_col} AS id FROM {relation} ORDER BY {col} {operator} %s::vector LIMIT %s",
            params=[query_vec, k],
            force_readonly=True,
        )
        truth_rows = await driver.execute_query(
            f"SELECT {id_col} AS id FROM {relation} ORDER BY {function}({col}, %s::vector) LIMIT %s",
            params=[query_vec, k],
            force_readonly=True,
        )
        ann_ids = {row.cells["id"] for row in ann_rows or []}
        truth_ids = {row.cells["id"] for row in truth_rows or []}
        if truth_ids:
            recalls.append(len(ann_ids & truth_ids) / len(truth_ids))

    mean = sum(recalls) / len(recalls) if recalls else 0.0
    return RecallReport(metric=metric, k=k, sample_size=len(samples), mean_recall=mean)


# --- migrate_vector_to_halfvec --------------------------------------------


# pgvector storage cost per element. vector → float32 (4 bytes),
# halfvec → float16 (2 bytes). Used for the saved-bytes estimate.
_VECTOR_BYTES_PER_ELEMENT = 4
_HALFVEC_BYTES_PER_ELEMENT = 2

# The pgvector → halfvec operator-class rename. Indexes carry these
# in their `pg_get_indexdef` output; the migration plan rewrites
# matches in place to keep the rest of each index definition (table,
# columns, WITH-options, partial WHERE) intact.
_VECTOR_TO_HALFVEC_OPCLASS = {
    "vector_l2_ops": "halfvec_l2_ops",
    "vector_ip_ops": "halfvec_ip_ops",
    "vector_cosine_ops": "halfvec_cosine_ops",
    "vector_l1_ops": "halfvec_l1_ops",
}


@dataclass(frozen=True)
class HalfvecIndexConversion:
    """One index touched by the halfvec migration.

    ``current_definition`` is the verbatim ``pg_get_indexdef`` output
    of the existing index (use it for rollback). ``recreate_sql`` is
    the same DDL with ``vector_*_ops`` rewritten to ``halfvec_*_ops``.
    ``conversion_supported`` is ``False`` when the opclass has no
    halfvec sibling (today the only blocker is custom user opclasses,
    not a concrete pgvector op) — in that case the migration plan
    refuses to drop the index rather than recreate it incorrectly.
    """

    index_name: str
    index_method: str
    current_definition: str
    recreate_sql: str
    conversion_supported: bool


@dataclass(frozen=True)
class HalfvecMigrationPlan:
    """A read-only DDL plan for converting a pgvector column to halfvec.

    ``available`` is ``False`` when the pgvector extension isn't
    installed. ``already_halfvec`` is ``True`` when the column is
    already a ``halfvec(N)`` — in that case ``migration_sql`` is empty
    and the caller has nothing to do.

    ``migration_sql`` is the ordered list of statements that performs
    the conversion: drop every affected index, ``ALTER COLUMN`` to
    ``halfvec(N)`` via a ``USING`` cast, then recreate each index
    with its halfvec opclass. ``rollback_sql`` mirrors it back to
    ``vector(N)`` using each index's original definition text.

    Nothing is executed — the caller is expected to feed
    ``migration_sql`` through the shadow-migration workflow
    (``prepare_migration`` / ``validate_migration_schema``) before
    applying it for real.
    """

    available: bool
    already_halfvec: bool
    column_type: str
    dimension: int
    row_count: int
    estimated_bytes_per_row_before: int
    estimated_bytes_per_row_after: int
    estimated_total_bytes_saved: int
    indexes: list[HalfvecIndexConversion]
    migration_sql: list[str]
    rollback_sql: list[str]
    notes: str


async def _column_pg_type(driver: SqlDriver, schema: str, table: str, column: str) -> tuple[str | None, int | None]:
    """Return ``(typename, dimension)`` for a column, or ``(None, None)``.

    ``dimension`` is the declared ``N`` for ``vector(N)`` / ``halfvec(N)``
    via ``pg_attribute.atttypmod``; ``None`` when the column has no
    declared dimension. We probe the catalog directly rather than
    going through :func:`mcpg.introspection.describe_table` because we
    want to keep walking when the type isn't ``vector`` (so we can
    return the no-op result for ``halfvec`` rather than raising).
    """
    rows = await driver.execute_query(
        "SELECT t.typname AS type_name, a.atttypmod AS type_mod "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s AND a.attnum > 0",
        params=[schema, table, column],
        force_readonly=True,
    )
    if not rows:
        return None, None
    cell = rows[0].cells
    type_name = cell.get("type_name")
    type_mod = cell.get("type_mod")
    dim = int(type_mod) if isinstance(type_mod, int) and type_mod > 0 else None
    return (type_name if isinstance(type_name, str) else None), dim


async def _indexes_on_column(driver: SqlDriver, schema: str, table: str, column: str) -> list[dict[str, str]]:
    """Return ``[{index_name, index_method, index_def}]`` for indexes on a column."""
    rows = await driver.execute_query(
        # Each index lists its key columns in ``indkey``; we unnest to
        # pull a single row per (index, key-position) and filter to
        # the column we care about. ``pg_get_indexdef`` gives the
        # exact CREATE INDEX text the planner used.
        "SELECT i.relname AS index_name, "
        "       am.amname AS index_method, "
        "       pg_get_indexdef(ix.indexrelid) AS index_def "
        "FROM pg_index ix "
        "JOIN pg_class t ON t.oid = ix.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN pg_class i ON i.oid = ix.indexrelid "
        "JOIN pg_am am ON am.oid = i.relam "
        # `pg_index.indkey` is `int2vector`, not a standard `int2[]`,
        # so `= ANY(...)` only resolves with an explicit cast — without
        # it the planner errors with "op any(int2vector) is not unique".
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey::int2[]) "
        "WHERE n.nspname = %s AND t.relname = %s AND a.attname = %s "
        "ORDER BY i.relname",
        params=[schema, table, column],
        force_readonly=True,
    )
    out: list[dict[str, str]] = []
    for row in rows or []:
        out.append(
            {
                "index_name": str(row.cells.get("index_name", "")),
                "index_method": str(row.cells.get("index_method", "")),
                "index_def": str(row.cells.get("index_def", "")),
            }
        )
    return out


def _rewrite_opclass_to_halfvec(index_def: str) -> tuple[str, bool]:
    """Rewrite ``vector_*_ops`` in an index definition to ``halfvec_*_ops``.

    Returns ``(rewritten_definition, conversion_supported)``. When no
    known ``vector_*_ops`` appears (e.g. someone built an HNSW with a
    custom opclass or a plain B-tree on the column), we report
    ``conversion_supported=False`` and leave the definition unchanged.
    """
    rewritten = index_def
    found = False
    for src, dst in _VECTOR_TO_HALFVEC_OPCLASS.items():
        if src in rewritten:
            rewritten = rewritten.replace(src, dst)
            found = True
    return rewritten, found


async def migrate_vector_to_halfvec(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
) -> HalfvecMigrationPlan:
    """Generate a DDL plan to convert a ``vector(N)`` column to ``halfvec(N)``.

    Reads the column's current type and dimension from the catalog,
    finds every index on the column, and emits the ordered DDL
    needed to drop those indexes, ``ALTER COLUMN`` to ``halfvec(N)``
    via a ``USING`` cast, and recreate each index with the halfvec
    sibling of its opclass. Also returns a ``rollback_sql`` plan that
    restores the original column type and recreates the original
    indexes from their captured definitions.

    Nothing is executed — this is a planner. Feed the result through
    the shadow-migration workflow before applying.

    Halfvec halves the per-element storage cost (4 → 2 bytes); for
    embedding columns at d ≥ 768 this typically gives a ~50% table-
    size reduction with negligible recall impact.

    Raises:
        VectorTuningError: pgvector is not installed, the column
            doesn't exist, the column isn't a ``vector`` type
            (``halfvec`` returns a no-op result instead), or an index
            on the column uses an opclass that has no halfvec
            sibling.
    """
    await _ensure_installed(driver)

    _quoted(schema, "schema")
    _quoted(table, "table")
    _quoted(column, "column")

    type_name, dimension = await _column_pg_type(driver, schema, table, column)
    if type_name is None or dimension is None:
        # Either the column doesn't exist or it has no declared
        # dimension. For a pgvector migration both are blockers.
        raise VectorTuningError(f"column {schema}.{table}.{column} is not a pgvector column with a declared dimension")

    if type_name == "halfvec":
        # Already at the target type — return a structured no-op so
        # callers can short-circuit without reading exception text.
        return HalfvecMigrationPlan(
            available=True,
            already_halfvec=True,
            column_type="halfvec",
            dimension=dimension,
            row_count=0,
            estimated_bytes_per_row_before=dimension * _HALFVEC_BYTES_PER_ELEMENT,
            estimated_bytes_per_row_after=dimension * _HALFVEC_BYTES_PER_ELEMENT,
            estimated_total_bytes_saved=0,
            indexes=[],
            migration_sql=[],
            rollback_sql=[],
            notes=f"{schema}.{table}.{column} is already halfvec({dimension}); no migration needed.",
        )

    if type_name != "vector":
        raise VectorTuningError(f"column {schema}.{table}.{column} is type {type_name!r}; expected pgvector vector(N)")

    row_count = await _row_count(driver, schema, table)
    index_rows = await _indexes_on_column(driver, schema, table, column)

    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    quoted_schema = _quoted(schema, "schema")

    conversions: list[HalfvecIndexConversion] = []
    drop_statements: list[str] = []
    recreate_statements: list[str] = []
    rollback_recreate: list[str] = []

    for ix in index_rows:
        name = ix["index_name"]
        method = ix["index_method"]
        current = ix["index_def"]
        rewritten, supported = _rewrite_opclass_to_halfvec(current)

        # We only rewrite ANN indexes — plain B-tree on a vector
        # column is unusual but we don't refuse it; we just refuse to
        # silently change its opclass and leave it to the user.
        if method in {"hnsw", "ivfflat"} and not supported:
            raise VectorTuningError(
                f"index {schema}.{name} uses an opclass with no halfvec sibling; "
                "supply a custom rewrite or drop the index before migrating"
            )

        ix_name_q = _quoted(name, "index")
        drop_statements.append(f"DROP INDEX IF EXISTS {quoted_schema}.{ix_name_q};")
        # If the opclass had no halfvec sibling we don't recreate at
        # all — the index was either non-vector (B-tree, GIN…) or
        # custom, and the user has to decide what to do with it.
        if supported:
            recreate_statements.append(rewritten + (";" if not rewritten.rstrip().endswith(";") else ""))
        # Rollback always restores the original index verbatim.
        rollback_recreate.append(current + (";" if not current.rstrip().endswith(";") else ""))

        conversions.append(
            HalfvecIndexConversion(
                index_name=name,
                index_method=method,
                current_definition=current,
                recreate_sql=rewritten if supported else current,
                conversion_supported=supported,
            )
        )

    alter_stmt = (
        f"ALTER TABLE {relation} ALTER COLUMN {col} TYPE halfvec({dimension}) USING {col}::halfvec({dimension});"
    )
    rollback_alter = (
        f"ALTER TABLE {relation} ALTER COLUMN {col} TYPE vector({dimension}) USING {col}::vector({dimension});"
    )

    # Forward order: drop indexes, alter the column, recreate indexes.
    # We can't ALTER through an index that depends on the column's
    # opclass, so the drops must precede the ALTER.
    migration_sql = [*drop_statements, alter_stmt, *recreate_statements]
    # Rollback walks the same shape in reverse intent: drop the new
    # halfvec indexes (still under their original names since rewrite
    # kept the names intact), restore the column type, then recreate
    # the original vector indexes.
    rollback_sql = [
        *(f"DROP INDEX IF EXISTS {quoted_schema}.{_quoted(ix['index_name'], 'index')};" for ix in index_rows),
        rollback_alter,
        *rollback_recreate,
    ]

    bytes_before = dimension * _VECTOR_BYTES_PER_ELEMENT
    bytes_after = dimension * _HALFVEC_BYTES_PER_ELEMENT
    total_saved = max(row_count, 0) * (bytes_before - bytes_after)

    notes = (
        f"Migrates vector({dimension}) → halfvec({dimension}) on "
        f"{schema}.{table}.{column}. {len(conversions)} index(es) will be "
        f"dropped and recreated with their halfvec opclasses. Estimated "
        f"row-level storage reduction: {bytes_before} → {bytes_after} bytes "
        f"per row (row_count={row_count:,} → ~{total_saved:,} bytes saved on "
        f"the column itself, before index size). Validate via the shadow-migration "
        f"workflow before applying."
    )

    return HalfvecMigrationPlan(
        available=True,
        already_halfvec=False,
        column_type="vector",
        dimension=dimension,
        row_count=row_count,
        estimated_bytes_per_row_before=bytes_before,
        estimated_bytes_per_row_after=bytes_after,
        estimated_total_bytes_saved=total_saved,
        indexes=conversions,
        migration_sql=migration_sql,
        rollback_sql=rollback_sql,
        notes=notes,
    )
