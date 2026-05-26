"""Schema advisors — codified lint rules over the PG catalog.

``run_advisors`` runs a set of catalog-driven checks and returns a
typed report of findings. Each rule is a small function that yields
:class:`Finding` instances; rules are pure SQL (no fix-up, no DDL) and
the tool is exposed under the READ capability so an agent in any mode
can request advice.

First-cut rules:

* ``missing_primary_key`` — base tables without a PRIMARY KEY constraint.
* ``unindexed_foreign_key`` — FK constraints whose leading column lacks
  an index whose first column matches; can produce slow joins and
  ``DELETE CASCADE`` storms.
* ``duplicate_indexes`` — two indexes on the same access method with
  identical column keys; one is redundant.
* ``nullable_timestamp_without_tz`` — nullable ``timestamp`` (without
  time zone) columns; a frequent source of TZ-coercion bugs.

Rules are deliberately conservative — the goal is "would a careful
reviewer flag this?" rather than "is this provably wrong?".
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# Stable rule identifiers — agents may filter by these.
RULE_MISSING_PRIMARY_KEY = "missing_primary_key"
RULE_UNINDEXED_FOREIGN_KEY = "unindexed_foreign_key"
RULE_DUPLICATE_INDEXES = "duplicate_indexes"
RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ = "nullable_timestamp_without_tz"

_RULES = (
    RULE_MISSING_PRIMARY_KEY,
    RULE_UNINDEXED_FOREIGN_KEY,
    RULE_DUPLICATE_INDEXES,
    RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ,
)


@dataclass(frozen=True, slots=True)
class Finding:
    """A single advisor finding.

    ``object`` is the qualified name of the thing the rule is about —
    ``"schema.table"`` for table-level rules, ``"schema.table.column"``
    for column-level rules, and ``"schema.index_a vs schema.index_b"``
    for the duplicate-indexes rule.
    """

    rule: str
    severity: str
    object: str
    message: str


@dataclass(frozen=True, slots=True)
class AdvisorReport:
    """The aggregated result of running every advisor against a schema."""

    schema: str
    rules_run: list[str]
    findings: list[Finding]


async def _missing_primary_keys(driver: SqlDriver, schema: str) -> list[Finding]:
    rows = await driver.execute_query(
        "SELECT c.relname AS table_name "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM pg_constraint con "
        "  WHERE con.conrelid = c.oid AND con.contype = 'p'"
        ") ORDER BY c.relname",
        params=[schema],
        force_readonly=True,
    )
    return [
        Finding(
            rule=RULE_MISSING_PRIMARY_KEY,
            severity="warning",
            object=f"{schema}.{row.cells['table_name']}",
            message="table has no PRIMARY KEY constraint",
        )
        for row in rows or []
    ]


async def _unindexed_foreign_keys(driver: SqlDriver, schema: str) -> list[Finding]:
    rows = await driver.execute_query(
        # Leading-column heuristic: a FK can use any index whose first
        # column matches the FK's first column; reporting on that is
        # close enough to "is this FK indexed?" for the common cases.
        "SELECT con.conname AS fk_name, c.relname AS table_name, att.attname AS first_column "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1] "
        "WHERE n.nspname = %s AND con.contype = 'f' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM pg_index idx "
        "  WHERE idx.indrelid = con.conrelid AND idx.indkey[0] = con.conkey[1]"
        ") ORDER BY c.relname, con.conname",
        params=[schema],
        force_readonly=True,
    )
    return [
        Finding(
            rule=RULE_UNINDEXED_FOREIGN_KEY,
            severity="warning",
            object=f"{schema}.{row.cells['table_name']}.{row.cells['first_column']}",
            message=(
                f"foreign key {row.cells['fk_name']!r} has no index whose leading column "
                f"is {row.cells['first_column']!r}; joins and cascading deletes will seq-scan"
            ),
        )
        for row in rows or []
    ]


async def _duplicate_indexes(driver: SqlDriver, schema: str) -> list[Finding]:
    rows = await driver.execute_query(
        "SELECT i1.relname AS index_a, i2.relname AS index_b, c.relname AS table_name "
        "FROM pg_index ix1 "
        "JOIN pg_class i1 ON i1.oid = ix1.indexrelid "
        "JOIN pg_class c ON c.oid = ix1.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        # Pair each index with every other index on the same table that
        # is functionally identical — same column keys, same operator
        # classes, same sort options, same uniqueness, same partial
        # predicate, same expression set, and not a primary-key
        # backing index. Without these, a UNIQUE / partial / expression
        # index would be falsely flagged as a duplicate of a plain
        # index over the same columns, and an agent acting on the
        # report could drop a constraint-enforcing index by mistake.
        "JOIN pg_index ix2 ON ix2.indrelid = ix1.indrelid "
        "  AND ix2.indexrelid > ix1.indexrelid "
        "  AND ix2.indkey = ix1.indkey "
        "  AND ix2.indclass = ix1.indclass "
        "  AND ix2.indoption = ix1.indoption "
        "  AND ix2.indisunique = ix1.indisunique "
        "  AND ix2.indisprimary = ix1.indisprimary "
        "  AND ix2.indpred::text IS NOT DISTINCT FROM ix1.indpred::text "
        "  AND ix2.indexprs::text IS NOT DISTINCT FROM ix1.indexprs::text "
        "JOIN pg_class i2 ON i2.oid = ix2.indexrelid "
        "WHERE n.nspname = %s AND i1.relam = i2.relam "
        "ORDER BY c.relname, i1.relname, i2.relname",
        params=[schema],
        force_readonly=True,
    )
    return [
        Finding(
            rule=RULE_DUPLICATE_INDEXES,
            severity="warning",
            object=f"{schema}.{row.cells['index_a']} vs {schema}.{row.cells['index_b']}",
            message=(
                f"indexes {row.cells['index_a']!r} and {row.cells['index_b']!r} on "
                f"{row.cells['table_name']!r} cover identical columns with the same access method"
            ),
        )
        for row in rows or []
    ]


async def _nullable_timestamps_without_tz(driver: SqlDriver, schema: str) -> list[Finding]:
    rows = await driver.execute_query(
        "SELECT c.relname AS table_name, att.attname AS column_name "
        "FROM pg_attribute att "
        "JOIN pg_class c ON c.oid = att.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = att.atttypid "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') "
        "AND att.attnum > 0 AND NOT att.attisdropped "
        # 'timestamp' is the internal name for 'timestamp without time
        # zone'; the TZ-aware variant is 'timestamptz'.
        "AND t.typname = 'timestamp' AND NOT att.attnotnull "
        "ORDER BY c.relname, att.attnum",
        params=[schema],
        force_readonly=True,
    )
    return [
        Finding(
            rule=RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ,
            severity="info",
            object=f"{schema}.{row.cells['table_name']}.{row.cells['column_name']}",
            message=(
                "nullable timestamp column without time zone — prefer 'timestamptz NOT NULL' "
                "to avoid TZ-coercion surprises"
            ),
        )
        for row in rows or []
    ]


async def run_advisors(driver: SqlDriver, schema: str) -> AdvisorReport:
    """Run every advisor rule against ``schema`` and aggregate the findings."""
    findings: list[Finding] = []
    findings.extend(await _missing_primary_keys(driver, schema))
    findings.extend(await _unindexed_foreign_keys(driver, schema))
    findings.extend(await _duplicate_indexes(driver, schema))
    findings.extend(await _nullable_timestamps_without_tz(driver, schema))
    return AdvisorReport(schema=schema, rules_run=list(_RULES), findings=findings)


# --- unused-objects finder (Phase 8.2) -----------------------------------


@dataclass(frozen=True, slots=True)
class UnusedTable:
    """A table that has had zero scans since pg_stat was last reset.

    Zero scans is a strong signal — even an idle table normally
    receives an occasional scan from `ANALYZE` or a one-off query.
    But it is a SIGNAL, not a proof: the table may have been
    created recently or stats may have been reset. Tools surface
    the seq_scan + idx_scan + ins+upd+del counts so the agent can
    decide for itself.
    """

    schema: str
    table: str
    seq_scans: int
    index_scans: int
    rows_modified: int
    estimated_row_count: int


@dataclass(frozen=True, slots=True)
class UnusedIndex:
    """A user-defined index that has been scanned zero times.

    Excludes indexes backing PRIMARY KEY / UNIQUE constraints — PG
    needs those for integrity enforcement regardless of scan counts.
    """

    schema: str
    table: str
    index: str
    size_bytes: int
    definition: str


@dataclass(frozen=True, slots=True)
class UnusedObjectsReport:
    """The result of :func:`find_unused_objects`.

    ``tables`` and ``indexes`` are sorted by name (deterministic) so
    repeated runs produce identical output and the diff is reviewable.
    """

    schema: str
    tables: list[UnusedTable]
    indexes: list[UnusedIndex]


async def find_unused_objects(driver: SqlDriver, schema: str) -> UnusedObjectsReport:
    """Find tables and indexes with zero scans since stats were reset.

    Tables: zero combined sequential + index scans AND zero writes
    (the row never moved). A genuinely cold table.

    Indexes: zero index scans. Indexes backing PRIMARY KEY or UNIQUE
    constraints are excluded — PG needs those for enforcement even if
    no query reads through them.

    Both lists report alongside enough context (size, definition,
    write count) for the agent to decide whether the object is safe
    to drop. **This is a SIGNAL, not a verdict** — recent stats
    resets, or tables only touched during deploys, can produce
    false positives.
    """
    table_rows = await driver.execute_query(
        "SELECT s.relname AS table_name, "
        "       COALESCE(s.seq_scan, 0) AS seq_scans, "
        "       COALESCE(s.idx_scan, 0) AS index_scans, "
        "       (COALESCE(s.n_tup_ins, 0) + COALESCE(s.n_tup_upd, 0) + COALESCE(s.n_tup_del, 0)) AS rows_modified, "
        "       COALESCE(c.reltuples, 0)::bigint AS estimated_row_count "
        "FROM pg_stat_user_tables s "
        "JOIN pg_class c ON c.oid = s.relid "
        "WHERE s.schemaname = %s "
        "AND COALESCE(s.seq_scan, 0) = 0 "
        "AND COALESCE(s.idx_scan, 0) = 0 "
        "AND (COALESCE(s.n_tup_ins, 0) + COALESCE(s.n_tup_upd, 0) + COALESCE(s.n_tup_del, 0)) = 0 "
        "ORDER BY s.relname",
        params=[schema],
        force_readonly=True,
    )
    unused_tables = [
        UnusedTable(
            schema=schema,
            table=str(row.cells["table_name"]),
            seq_scans=int(row.cells["seq_scans"]),
            index_scans=int(row.cells["index_scans"]),
            rows_modified=int(row.cells["rows_modified"]),
            estimated_row_count=int(row.cells["estimated_row_count"]),
        )
        for row in table_rows or []
    ]

    index_rows = await driver.execute_query(
        "SELECT s.relname AS table_name, "
        "       s.indexrelname AS index_name, "
        "       pg_relation_size(s.indexrelid) AS size_bytes, "
        "       pg_get_indexdef(s.indexrelid) AS definition "
        "FROM pg_stat_user_indexes s "
        "JOIN pg_index i ON i.indexrelid = s.indexrelid "
        "WHERE s.schemaname = %s "
        "AND COALESCE(s.idx_scan, 0) = 0 "
        "AND NOT i.indisprimary "
        "AND NOT i.indisunique "
        "ORDER BY s.relname, s.indexrelname",
        params=[schema],
        force_readonly=True,
    )
    unused_indexes = [
        UnusedIndex(
            schema=schema,
            table=str(row.cells["table_name"]),
            index=str(row.cells["index_name"]),
            size_bytes=int(row.cells["size_bytes"]),
            definition=str(row.cells["definition"]),
        )
        for row in index_rows or []
    ]

    return UnusedObjectsReport(schema=schema, tables=unused_tables, indexes=unused_indexes)
