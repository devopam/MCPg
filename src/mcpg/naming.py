"""Naming-convention linter — catch inconsistencies in table / column / index names.

Pure catalog read. The linter doesn't enforce a specific convention
(snake_case is the Postgres community default, but many shops use
CamelCase or PascalCase) — instead it detects the *majority* style in
a schema and flags outliers. That way an `events` table and a
`Customers` table living in the same schema get noticed.

Three rules:

* ``table_naming_inconsistent`` — a table whose case style differs
  from the schema majority.
* ``column_naming_inconsistent`` — a column whose case style differs
  from the table's majority.
* ``index_unexpected_prefix`` — an index whose name doesn't start
  with any of ``idx_`` / ``ix_`` / ``pk_`` / ``uq_`` / ``fk_``
  (configurable via ``allowed_prefixes``).

Each finding includes the offending object and the detected
majority style so the agent has the context to decide whether to
rename or to surface the discrepancy elsewhere.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

DEFAULT_INDEX_PREFIXES: tuple[str, ...] = ("idx_", "ix_", "pk_", "uq_", "fk_", "gin_", "gist_", "brin_", "hnsw_")


@dataclass(frozen=True)
class NamingFinding:
    """One discrepancy detected by the linter.

    ``style`` is the offender's detected style; ``majority_style`` is
    the style most of its peers use. For ``index_unexpected_prefix``,
    ``majority_style`` is empty.
    """

    rule: str
    object: str
    style: str
    majority_style: str
    message: str


@dataclass(frozen=True)
class NamingReport:
    """Result of :func:`lint_naming_conventions`.

    ``schema_majority_style`` is the case style most tables in the
    schema use — useful context for an agent deciding whether the
    flagged outliers are noise or worth renaming.
    """

    schema: str
    schema_majority_style: str
    findings: list[NamingFinding]


_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
_CAMEL = re.compile(r"^[a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*$")
_PASCAL = re.compile(r"^[A-Z][a-zA-Z0-9]*$")
_UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")


def classify_style(name: str) -> str:
    """Classify ``name`` into one of: snake_case / camelCase / PascalCase / SCREAMING_SNAKE / other."""
    if _SNAKE.match(name):
        return "snake_case"
    if _CAMEL.match(name):
        return "camelCase"
    if _PASCAL.match(name):
        return "PascalCase"
    if _UPPER.match(name):
        return "SCREAMING_SNAKE"
    return "other"


def _majority(names: Iterable[str]) -> str:
    counts = Counter(classify_style(name) for name in names)
    if not counts:
        return "snake_case"  # neutral default for an empty schema
    return counts.most_common(1)[0][0]


async def lint_naming_conventions(
    driver: SqlDriver,
    schema: str,
    *,
    allowed_index_prefixes: tuple[str, ...] = DEFAULT_INDEX_PREFIXES,
) -> NamingReport:
    """Lint table / column / index naming in ``schema``.

    Args:
        allowed_index_prefixes: Index names not starting with any of
            these prefixes are flagged as ``index_unexpected_prefix``.
            Lowercased prefix comparison — case-insensitive on the
            index name. Default covers the common conventions plus
            access-method-derived ones (``gin_``, ``hnsw_``).
    """
    table_rows = await driver.execute_query(
        "SELECT c.relname AS table_name "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') "
        "ORDER BY c.relname",
        params=[schema],
        force_readonly=True,
    )
    table_names = [str(r.cells["table_name"]) for r in table_rows or []]
    table_majority = _majority(table_names)

    findings: list[NamingFinding] = []
    for table in table_names:
        style = classify_style(table)
        if style != table_majority:
            findings.append(
                NamingFinding(
                    rule="table_naming_inconsistent",
                    object=f"{schema}.{table}",
                    style=style,
                    majority_style=table_majority,
                    message=(f"table {table!r} uses {style} but the schema majority is {table_majority}"),
                )
            )

    # Column-level rule: for each table, detect majority among its own
    # columns; flag outliers within that table.
    column_rows = await driver.execute_query(
        "SELECT c.relname AS table_name, a.attname AS column_name "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s "
        "AND c.relkind IN ('r', 'p') "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY c.relname, a.attnum",
        params=[schema],
        force_readonly=True,
    )
    cols_by_table: dict[str, list[str]] = {}
    for row in column_rows or []:
        cols_by_table.setdefault(str(row.cells["table_name"]), []).append(str(row.cells["column_name"]))
    for table, columns in cols_by_table.items():
        if len(columns) < 2:
            # A one-column table can't be inconsistent with itself.
            continue
        table_col_majority = _majority(columns)
        for column in columns:
            style = classify_style(column)
            if style != table_col_majority:
                findings.append(
                    NamingFinding(
                        rule="column_naming_inconsistent",
                        object=f"{schema}.{table}.{column}",
                        style=style,
                        majority_style=table_col_majority,
                        message=(
                            f"column {column!r} on {table!r} uses {style} but the table majority is "
                            f"{table_col_majority}"
                        ),
                    )
                )

    # Index-prefix rule: pure prefix check, not majority-based.
    index_rows = await driver.execute_query(
        "SELECT c.relname AS table_name, i.relname AS index_name "
        "FROM pg_class i "
        "JOIN pg_index ix ON ix.indexrelid = i.oid "
        "JOIN pg_class c ON c.oid = ix.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s "
        "AND NOT ix.indisprimary "
        "ORDER BY c.relname, i.relname",
        params=[schema],
        force_readonly=True,
    )
    prefixes_lower = tuple(p.lower() for p in allowed_index_prefixes)
    for row in index_rows or []:
        index = str(row.cells["index_name"])
        table = str(row.cells["table_name"])
        if not any(index.lower().startswith(p) for p in prefixes_lower):
            findings.append(
                NamingFinding(
                    rule="index_unexpected_prefix",
                    object=f"{schema}.{index}",
                    style="other",
                    majority_style="",
                    message=(
                        f"index {index!r} on {table!r} does not start with any of {list(allowed_index_prefixes)!r}"
                    ),
                )
            )

    return NamingReport(schema=schema, schema_majority_style=table_majority, findings=findings)
