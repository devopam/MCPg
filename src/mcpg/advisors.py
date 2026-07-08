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

import re
from dataclasses import dataclass
from typing import Any

from mcpg.query import QueryError, analyze_query_plan
from mcpg.sql import SqlDriver

# Stable rule identifiers — agents may filter by these.
RULE_MISSING_PRIMARY_KEY = "missing_primary_key"
RULE_UNINDEXED_FOREIGN_KEY = "unindexed_foreign_key"
RULE_DUPLICATE_INDEXES = "duplicate_indexes"
RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ = "nullable_timestamp_without_tz"
RULE_RECOMMEND_GRAPH_INDICES = "recommend_graph_indices"
RULE_REDUNDANT_INDEXES = "redundant_indexes"

_RULES = (
    RULE_MISSING_PRIMARY_KEY,
    RULE_UNINDEXED_FOREIGN_KEY,
    RULE_DUPLICATE_INDEXES,
    RULE_NULLABLE_TIMESTAMP_WITHOUT_TZ,
    RULE_RECOMMEND_GRAPH_INDICES,
    RULE_REDUNDANT_INDEXES,
)


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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


async def _recommend_graph_indices(driver: SqlDriver, schema: str) -> list[Finding]:
    try:
        rows = await driver.execute_query(
            "SELECT c.relname AS table_name "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN ag_catalog.ag_label l ON l.name = c.relname "
            "  AND l.graph = (SELECT graphid FROM ag_catalog.ag_graph WHERE namespace = n.nspname) "
            "WHERE n.nspname = %s AND l.kind = 'v' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM pg_index idx "
            "  JOIN pg_class i ON i.oid = idx.indexrelid "
            "  WHERE idx.indrelid = c.oid AND pg_get_indexdef(idx.indexrelid) LIKE '%properties%'"
            ") ORDER BY c.relname",
            params=[schema],
            force_readonly=True,
        )
    except Exception:
        # Gracefully degrade if Apache AGE is not installed or ag_graph is missing
        return []

    return [
        Finding(
            rule=RULE_RECOMMEND_GRAPH_INDICES,
            severity="warning",
            object=f"{schema}.{row.cells['table_name']}",
            message=(
                "graph label table has no index on the 'properties' column; "
                "queries filtering on properties will seq-scan"
            ),
        )
        for row in rows or []
    ]


async def _redundant_indexes(driver: SqlDriver, schema: str) -> list[Finding]:
    """Identify B-Tree indexes whose columns are a leading prefix of another index.

    Operators can drop prefix-redundant indexes to reclaim disk space and reduce
    write-amplification overhead without affecting query-planning efficacy.
    """
    rows = await driver.execute_query(
        "SELECT "
        "  c.relname AS table_name, "
        "  i.relname AS index_name, "
        "  ix.indisunique AS is_unique, "
        "  ix.indisprimary AS is_primary, "
        "  ix.indkey::text AS indkey, "
        "  pg_relation_size(i.oid) AS index_size, "
        "  ix.indpred::text AS indpred, "
        "  ix.indexprs::text AS indexprs "
        "FROM pg_index ix "
        "JOIN pg_class i ON i.oid = ix.indexrelid "
        "JOIN pg_class c ON c.oid = ix.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_am am ON am.oid = i.relam "
        "WHERE n.nspname = %s AND am.amname = 'btree' "
        "ORDER BY c.relname, ix.indkey::text",
        params=[schema],
        force_readonly=True,
    )
    if not rows:
        return []

    # Group by table
    by_table: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        t_name = row.cells["table_name"]
        by_table.setdefault(t_name, []).append(
            {
                "name": row.cells["index_name"],
                "is_unique": row.cells["is_unique"],
                "is_primary": row.cells["is_primary"],
                "indkey_str": row.cells["indkey"] or "",
                "size": row.cells["index_size"] or 0,
                "indpred": row.cells.get("indpred"),
                "indexprs": row.cells.get("indexprs"),
            }
        )

    findings: list[Finding] = []

    for t_name, indexes in by_table.items():
        # Parse column vectors
        parsed_indexes = []
        for idx in indexes:
            cols = [int(x) for x in idx["indkey_str"].split()]
            parsed_indexes.append({**idx, "cols": cols})

        # Compare each pair of indexes
        for idx_a in parsed_indexes:
            # Only recommend dropping if it is not primary or unique
            if idx_a["is_primary"] or idx_a["is_unique"]:
                continue

            cols_a = idx_a["cols"]
            if not cols_a:
                continue

            for idx_b in parsed_indexes:
                if idx_a["name"] == idx_b["name"]:
                    continue

                cols_b = idx_b["cols"]
                # If B starts with A and has more columns, then A is redundant!
                if len(cols_b) > len(cols_a) and cols_b[: len(cols_a)] == cols_a:
                    # Ensure partial index predicates match, or B is a global index covering A
                    if idx_b["indpred"] and idx_b["indpred"] != idx_a["indpred"]:
                        continue
                    # Ensure expressions match if any are present
                    if (0 in cols_a) and idx_a["indexprs"] != idx_b["indexprs"]:
                        continue

                    findings.append(
                        Finding(
                            rule=RULE_REDUNDANT_INDEXES,
                            severity="warning",
                            object=f"{schema}.{idx_a['name']} covered by {schema}.{idx_b['name']}",
                            message=(
                                f"index {idx_a['name']!r} on table {t_name!r} is a prefix subset of "
                                f"index {idx_b['name']!r} (redundant, size: {idx_a['size']} bytes)"
                            ),
                        )
                    )
                    break  # Only report once per redundant index

    return findings


async def run_advisors(driver: SqlDriver, schema: str) -> AdvisorReport:
    """Run every advisor rule against ``schema`` and aggregate the findings."""
    findings: list[Finding] = []
    findings.extend(await _missing_primary_keys(driver, schema))
    findings.extend(await _unindexed_foreign_keys(driver, schema))
    findings.extend(await _duplicate_indexes(driver, schema))
    findings.extend(await _nullable_timestamps_without_tz(driver, schema))
    findings.extend(await _recommend_graph_indices(driver, schema))
    findings.extend(await _redundant_indexes(driver, schema))
    return AdvisorReport(schema=schema, rules_run=list(_RULES), findings=findings)


# --- unused-objects finder (Phase 8.2) -----------------------------------


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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
        "       COALESCE(pg_relation_size(s.indexrelid), 0) AS size_bytes, "
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


# --- sensitive-column heuristic (Phase 6.2) ------------------------------

SENSITIVITY_CREDENTIAL = "credential"
SENSITIVITY_FINANCIAL = "financial"
SENSITIVITY_CONTACT = "contact"
SENSITIVITY_IDENTIFIER = "identifier"
SENSITIVITY_HEALTH = "health"
SENSITIVITY_GOVERNMENT_ID = "government_id"
SENSITIVITY_LOCATION = "location"


# Each token below is wrapped with letter-only lookarounds (NOT \b),
# because Python's \b treats `_` as a word character, so \bpassword\b
# would fail to match `password_hash`. The lookarounds let underscores
# and digits act as token boundaries while still preventing
# `repassword` or `passworded` from matching.
def _word(*tokens: str) -> str:
    # Each token may itself contain alternatives, e.g. "passwor?d".
    body = "|".join(tokens)
    return rf"(?<![A-Za-z])(?:{body})(?![A-Za-z])"


_SENSITIVE_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    # (pattern, category, confidence, reason)
    (_word("password", "passwd", "pwd"), SENSITIVITY_CREDENTIAL, "high", "stores authentication credentials"),
    (
        _word("secret", "api[_-]?key", "access[_-]?key", "private[_-]?key"),
        SENSITIVITY_CREDENTIAL,
        "high",
        "looks like a secret / API key",
    ),
    (
        _word("token", "auth[_-]?token", "bearer", "refresh[_-]?token"),
        SENSITIVITY_CREDENTIAL,
        "high",
        "looks like an auth token",
    ),
    (
        _word("session[_-]?id", "session[_-]?key"),
        SENSITIVITY_CREDENTIAL,
        "medium",
        "session identifier — can hijack a logged-in user",
    ),
    (
        _word("ssn", "social[_-]?security", "tin", "tax[_-]?id"),
        SENSITIVITY_GOVERNMENT_ID,
        "high",
        "government identifier",
    ),
    (
        _word("passport", "drivers?[_-]?license", "national[_-]?id"),
        SENSITIVITY_GOVERNMENT_ID,
        "high",
        "government identifier",
    ),
    (
        _word("credit[_-]?card", "card[_-]?number", "ccnum", "pan"),
        SENSITIVITY_FINANCIAL,
        "high",
        "stores a payment-card number (PCI scope)",
    ),
    (
        _word("cvv", "cvc", "card[_-]?verif[a-z]*"),
        SENSITIVITY_FINANCIAL,
        "high",
        "card verification value (PCI scope, never persist)",
    ),
    (
        _word("bank[_-]?account", "iban", "swift", "routing[_-]?number"),
        SENSITIVITY_FINANCIAL,
        "high",
        "bank-account identifier",
    ),
    (_word("salary", "compensation", "income"), SENSITIVITY_FINANCIAL, "medium", "financial compensation data"),
    (_word("email", "email[_-]?address", "e[_-]?mail"), SENSITIVITY_CONTACT, "medium", "personal email address"),
    (_word("phone", "mobile", "telephone", "cell[_-]?number"), SENSITIVITY_CONTACT, "medium", "personal phone number"),
    (
        _word("address", "street", "zip", "zipcode", "postal[_-]?code", "postcode"),
        SENSITIVITY_LOCATION,
        "medium",
        "postal address fragment",
    ),
    (_word("latitude", "longitude", "geo[_-]?location"), SENSITIVITY_LOCATION, "low", "geographic coordinate"),
    (
        _word("date[_-]?of[_-]?birth", "dob", "birth[_-]?date", "birthday"),
        SENSITIVITY_IDENTIFIER,
        "high",
        "date of birth (PII)",
    ),
    (
        _word("full[_-]?name", "first[_-]?name", "last[_-]?name", "given[_-]?name", "surname"),
        SENSITIVITY_IDENTIFIER,
        "low",
        "personal name",
    ),
    (_word("gender", "ethnicity", "nationality"), SENSITIVITY_IDENTIFIER, "medium", "demographic identifier"),
    (
        _word("diagnos[a-z]*", "medication", "prescription", "icd[_-]?10", "icd[_-]?9"),
        SENSITIVITY_HEALTH,
        "high",
        "health record (HIPAA scope)",
    ),
    (_word("medical", "patient"), SENSITIVITY_HEALTH, "medium", "health-related data"),
)

# Types that are PII-shaped almost regardless of column name.
_SENSITIVE_TYPES: tuple[tuple[str, str, str, str], ...] = (
    # `inet` columns almost always carry IP addresses, which most
    # privacy regimes treat as personal data.
    ("inet", SENSITIVITY_LOCATION, "medium", "INET column — IP addresses are personal data under most privacy regimes"),
    ("cidr", SENSITIVITY_LOCATION, "low", "CIDR column — may contain network ranges that identify a household / org"),
)

_COMPILED_NAME_PATTERNS = tuple(
    (re.compile(p, re.IGNORECASE), cat, conf, reason) for p, cat, conf, reason in _SENSITIVE_PATTERNS
)


@dataclass(frozen=True)
class SensitiveColumn:
    """One column that one or more heuristics flagged as potentially sensitive.

    ``categories`` is a deduplicated list — a column named ``user_email``
    can match the contact heuristic; a column named ``patient_dob``
    matches both health and identifier. ``confidence`` is the highest
    confidence of any matching heuristic (high > medium > low).
    """

    schema: str
    table: str
    column: str
    data_type: str
    categories: list[str]
    confidence: str
    reasons: list[str]


@dataclass(frozen=True)
class SensitiveColumnsReport:
    """Aggregate result of :func:`find_sensitive_columns`.

    Sorted by ``(table, column)`` so repeated runs produce identical
    output and the diff is reviewable.
    """

    schema: str
    columns: list[SensitiveColumn]


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _classify_column(name: str, data_type: str) -> tuple[list[str], str, list[str]] | None:
    """Apply every heuristic to one column.

    Returns ``None`` when nothing matches. Otherwise returns
    ``(unique_categories, highest_confidence, unique_reasons)``.
    """
    categories: list[str] = []
    reasons: list[str] = []
    confidence_rank = 0
    for pattern, category, conf, reason in _COMPILED_NAME_PATTERNS:
        if pattern.search(name):
            if category not in categories:
                categories.append(category)
            if reason not in reasons:
                reasons.append(reason)
            confidence_rank = max(confidence_rank, _CONFIDENCE_RANK[conf])
    for type_match, category, conf, reason in _SENSITIVE_TYPES:
        if data_type.lower() == type_match:
            if category not in categories:
                categories.append(category)
            if reason not in reasons:
                reasons.append(reason)
            confidence_rank = max(confidence_rank, _CONFIDENCE_RANK[conf])
    if not categories:
        return None
    confidence = next(c for c, rank in _CONFIDENCE_RANK.items() if rank == confidence_rank)
    return categories, confidence, reasons


async def find_sensitive_columns(driver: SqlDriver, schema: str) -> SensitiveColumnsReport:
    """Flag columns that look like they hold sensitive data (PII / secrets).

    Uses a name- and type-pattern heuristic — no row sampling, no
    introspection of actual values. Designed as a SIGNAL for review,
    not a verdict: a column called ``email_template_id`` will match
    the email heuristic but isn't itself an email address.

    Categories: ``credential``, ``financial``, ``contact``,
    ``identifier``, ``health``, ``government_id``, ``location``.
    Confidence: ``high`` (very specific name), ``medium`` (common
    pattern), ``low`` (broad pattern). Use the confidence to filter
    output for an initial review pass.
    """
    rows = await driver.execute_query(
        "SELECT c.relname AS table_name, "
        "       a.attname AS column_name, "
        "       format_type(a.atttypid, a.atttypmod) AS data_type "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s "
        "AND c.relkind IN ('r', 'p', 'v', 'm') "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY c.relname, a.attnum",
        params=[schema],
        force_readonly=True,
    )
    columns: list[SensitiveColumn] = []
    for row in rows or []:
        name = str(row.cells["column_name"])
        data_type = str(row.cells["data_type"])
        classified = _classify_column(name, data_type)
        if classified is None:
            continue
        categories, confidence, reasons = classified
        columns.append(
            SensitiveColumn(
                schema=schema,
                table=str(row.cells["table_name"]),
                column=name,
                data_type=data_type,
                categories=categories,
                confidence=confidence,
                reasons=reasons,
            )
        )
    return SensitiveColumnsReport(schema=schema, columns=columns)


@dataclass(frozen=True)
class OptimizationResult:
    """The result of query syntax and execution plan optimization."""

    original_sql: str
    optimized_sql: str
    findings: list[str]
    explain_summary: str
    rationale: str


async def optimize_query(driver: SqlDriver, sql: str) -> OptimizationResult:
    """Analyze a SQL query for anti-patterns and performance issues, returning an optimized version."""
    findings = []
    ex_summary = ""

    # 1. Plan analysis
    try:
        plan = await analyze_query_plan(driver, sql)
        ex_summary = (
            f"Estimated Cost: {plan.total_cost:.2f} | "
            f"Estimated Rows: {plan.estimated_rows} | "
            f"Node Types: {', '.join(plan.node_types)}"
        )
        if plan.sequential_scans:
            seq_scans_str = ", ".join(plan.sequential_scans)
            findings.append(f"Sequential scan detected on table(s): {seq_scans_str}")
    except QueryError as err:
        ex_summary = f"Plan Analysis Failed: {err}"

    # 2. Syntax anti-pattern audits
    # Check SELECT *
    has_select_star = bool(re.search(r"\bSELECT\s+\*(?!\w)", sql, re.IGNORECASE))
    if has_select_star:
        findings.append("Avoid using SELECT * to minimize network transfer and enable index-only scans.")

    # Check missing LIMIT
    is_select = bool(re.search(r"\bSELECT\b", sql, re.IGNORECASE))
    has_limit = bool(re.search(r"\bLIMIT\s+\d+\b", sql, re.IGNORECASE))
    if is_select and not has_limit:
        findings.append("Missing LIMIT clause on large table queries can cause memory and socket exhaustion.")

    # Check IN subquery
    has_in_subquery = bool(re.search(r"\bIN\s*\(\s*SELECT\b", sql, re.IGNORECASE))
    if has_in_subquery:
        findings.append(
            "Use of IN (SELECT ...) subquery instead of JOIN or EXISTS. Subqueries with IN can prevent the "
            "planner from using optimal semi-joins."
        )

    # Check leading wildcard
    has_leading_wildcard = bool(re.search(r"\bLIKE\s*'\%.*?'", sql, re.IGNORECASE))
    if has_leading_wildcard:
        findings.append(
            "Leading wildcard search (LIKE '%term%') prevents standard B-Tree index use. Consider a trigram "
            "GIN index (pg_trgm) or full-text indexing."
        )

    # 3. Construct optimized SQL
    opt_sql = sql
    rationale_parts = []

    if has_select_star:
        # Suggest replacing * with columns
        opt_sql = re.sub(r"\bSELECT\s+\*(?!\w)", "SELECT id, [explicit_columns]", opt_sql, flags=re.IGNORECASE)
        rationale_parts.append(
            "- Replaced SELECT * with explicit column list to reduce data transmission and allow index-only scans."
        )

    if is_select and not has_limit:
        # Suggest adding LIMIT
        stripped_sql = opt_sql.rstrip().rstrip(";")
        opt_sql = f"{stripped_sql} LIMIT 100;"
        rationale_parts.append(
            "- Appended LIMIT 100 to safeguard application memory and prevent querying millions of records."
        )

    if has_in_subquery:
        rationale_parts.append(
            "- Suggest refactoring IN (SELECT ...) subqueries using INNER JOIN or EXISTS for cleaner planner "
            "execution paths."
        )

    if has_leading_wildcard:
        rationale_parts.append(
            "- Suggest using pg_trgm trigram similarity search or PostgreSQL full-text search to leverage indexing "
            "for wildcard searches."
        )

    try:
        if "plan" in locals() and plan.sequential_scans:
            rationale_parts.append(
                "- Consider adding indexes on columns used in WHERE or JOIN clauses for tables with Seq Scan."
            )
    except Exception:
        pass

    rationale = (
        "\n".join(rationale_parts)
        if rationale_parts
        else ("The query has no obvious syntactic anti-patterns. Ensure proper indexes are configured.")
    )

    return OptimizationResult(
        original_sql=sql,
        optimized_sql=opt_sql,
        findings=findings,
        explain_summary=ex_summary,
        rationale=rationale,
    )
