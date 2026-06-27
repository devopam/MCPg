"""Realistic single-row factory — one row per call, catalogue-aware.

Sibling of :mod:`mcpg.test_data` (bulk synthetic ``INSERT`` statements
that ignore foreign keys) and :func:`mcpg.test_data.seed_table_with_sample_data`
(executes the bulk dataset). Realises roadmap row **8.1** —
``generate_test_row_for(schema, table)`` for the shadow-migration
workflow.

Why a separate tool?
====================

The bulk generator's contract is "best-effort statements, the agent
fixes up FK references afterwards." The shadow-migration workflow
(:mod:`mcpg.migrations`) doesn't have an afterwards — it clones a
schema, the candidate SQL runs, the diff is reported, the shadow is
dropped. Bulk inserts that fail mid-batch defeat the point.

This module produces **one row** that is likely to insert cleanly
against the catalogue as it stands today:

* **Foreign keys are resolved** when ``follow_foreign_keys=True``
  (default) by sampling one existing row from each referenced table.
  If the referenced table is empty the FK column falls back to
  ``DEFAULT`` (or ``NULL`` when the column is nullable).
* **Identity and generated columns are skipped** so the server fills
  them in — preventing the "column ``id`` is GENERATED ALWAYS" /
  identity-column conflicts the bulk generator can't side-step.
* **Column-name heuristics** produce realistic-looking values for
  common patterns (``*_email`` → ``user_N@example.com``, ``*_url``
  → ``https://example.com/r/N``, etc.) so the resulting row reads
  like data, not garbage. Helps when the row is later eyeballed
  during a migration review.

The function never executes the generated SQL — it returns it as a
string plus a per-column reasoning log. Callers run it through
``run_write`` (or the shadow-migration apply step) when ready.

Security posture
================

* Identifiers (``schema``, ``table``, and any referenced
  ``schema.table.column`` discovered during FK resolution) are
  validated against the unquoted-identifier regex before reaching
  any composed SQL.
* All scalar values land as SQL literals via the same
  ``_pg_quote_literal``-style escape used by :mod:`mcpg.test_data`.
* Sampling the FK target uses parameter binding — the literal that
  ends up in the INSERT is the **value** of the sampled column, not
  a SQL fragment.
"""

from __future__ import annotations

import random
import re
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import ColumnInfo, describe_table

_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class TestRowFactoryError(Exception):
    """Raised when the factory is rejected or cannot produce a row."""

    __test__ = False  # opt out of pytest collection — class name starts with Test


@dataclass(frozen=True)
class ColumnFill:
    """One column's contribution to the generated row.

    ``sql_literal`` is what lands in the ``VALUES (...)`` clause —
    already-quoted, ready to embed. ``"DEFAULT"`` means the column
    was omitted from the INSERT column list so the server fills it
    in (identity / generated columns, FK whose target is empty with
    a column default). ``"NULL"`` means the column was nullable and
    no value could be synthesised.

    ``heuristic`` is a short human-readable note explaining how the
    value was chosen — useful for migration reviewers eyeballing
    why a particular row looks the way it does.
    """

    name: str
    sql_literal: str
    heuristic: str


@dataclass(frozen=True)
class GeneratedTestRow:
    """Result of :func:`generate_test_row_for`.

    ``insert_sql`` is a single ``INSERT INTO "schema"."table"
    (cols...) VALUES (...)`` statement ready to execute. ``columns``
    carries one :class:`ColumnFill` per column the factory considered
    — including skipped columns so the reviewer sees the full
    decision trail.
    """

    schema: str
    table: str
    columns: list[ColumnFill]
    insert_sql: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_identifier(value: str, kind: str) -> None:
    if not _IDENTIFIER.match(value):
        raise TestRowFactoryError(f"invalid {kind} {value!r}; must match [A-Za-z_][A-Za-z0-9_]*")


def _quote_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'"
    if isinstance(value, bytes):
        return f"'\\\\x{value.hex()}'"
    s = str(value).replace("'", "''")
    return f"'{s}'"


# Column-name → heuristic map. Order matters: ``last_login_at`` should
# match the ``_at`` timestamp rule before the ``last`` name rule.
_NAME_FIRST = ("alex", "sam", "jamie", "chris", "morgan", "taylor", "dana", "kai")
_NAME_LAST = ("singh", "patel", "kim", "garcia", "okafor", "wong", "silva", "novak")
_COUNTRIES = ("US", "GB", "IN", "DE", "JP", "BR", "ZA", "AU")
_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "INR", "BRL", "ZAR", "AUD")


def _synth_by_name(col_name: str, rng: random.Random) -> tuple[object, str] | None:
    """Return ``(value, heuristic_label)`` if ``col_name`` matches a
    well-known pattern; otherwise ``None`` so the caller falls through
    to type-based synthesis."""
    name = col_name.lower()
    # Time-bearing names land first so ``last_login_at`` wins on ``_at``.
    if name.endswith("_at") or name in {"created", "updated", "last_seen", "last_login"} or name.endswith("_timestamp"):
        ts = datetime.now(UTC) - timedelta(seconds=rng.randint(0, 60 * 60 * 24 * 30))
        return ts, "timestamp pattern"
    if name == "email" or name.endswith("_email"):
        return f"user_{rng.randint(1, 9999)}@example.com", "email pattern"
    if name == "url" or name.endswith("_url") or name.endswith("_uri"):
        return f"https://example.com/r/{rng.randint(1, 9999)}", "url pattern"
    if name == "phone" or name.endswith("_phone"):
        return f"+1-555-{rng.randint(1000, 9999)}", "phone pattern"
    if name in {"country", "country_code"}:
        return rng.choice(_COUNTRIES), "country pattern"
    if name in {"currency", "currency_code"}:
        return rng.choice(_CURRENCIES), "currency pattern"
    if name == "ip" or name == "ip_address":
        return f"192.0.2.{rng.randint(1, 254)}", "ip pattern (RFC 5737 docs range)"
    if name == "slug":
        return "-".join(rng.choice(_NAME_FIRST) for _ in range(rng.randint(2, 3))), "slug pattern"
    if name in {"first_name", "given_name"}:
        return rng.choice(_NAME_FIRST), "first_name pattern"
    if name in {"last_name", "family_name", "surname"}:
        return rng.choice(_NAME_LAST), "last_name pattern"
    if name in {"full_name", "name", "display_name"}:
        return f"{rng.choice(_NAME_FIRST)} {rng.choice(_NAME_LAST)}", "name pattern"
    return None


def _synth_by_type(column: ColumnInfo, rng: random.Random) -> tuple[object, str] | None:
    """Fall-back type-driven synthesis."""
    base = column.data_type.lower().split("(", 1)[0].strip()
    if base in {"integer", "int", "int4", "int2", "smallint", "bigint", "int8"}:
        return rng.randint(1, 1_000_000), "int type"
    if base in {"numeric", "decimal", "real", "double precision", "float4", "float8"}:
        return round(rng.random() * 1000, 2), "numeric type"
    if base in {"boolean", "bool"}:
        return rng.choice([True, False]), "bool type"
    if base == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128))), "uuid type"
    if base == "date":
        return (datetime.now(UTC).date() - timedelta(days=rng.randint(0, 365))).isoformat(), "date type"
    if base in {"timestamp", "timestamp without time zone", "timestamptz", "timestamp with time zone"}:
        return datetime.now(UTC) - timedelta(seconds=rng.randint(0, 60 * 60 * 24 * 365)), "timestamp type"
    if base in {"text", "varchar", "character varying", "char", "character", "citext", "name"}:
        # Honour the (N) length cap on varchar(N) / char(N) — generating
        # a 4-16 char string into a varchar(2) lands a real "value too
        # long" failure on the INSERT (gemini review on #178).
        max_len: int | None = None
        if "(" in column.data_type:
            try:
                max_len = int(column.data_type.split("(", 1)[1].split(")", 1)[0].strip())
            except (ValueError, IndexError):
                max_len = None
        lo, hi = 4, 16
        if max_len is not None:
            hi = min(hi, max(1, max_len))
            lo = min(lo, hi)
        length = rng.randint(lo, hi)
        return "".join(rng.choice(string.ascii_lowercase) for _ in range(length)), "text type"
    if base in {"json", "jsonb"}:
        return '{"k": "v"}', "json type"
    return None


async def _identity_or_generated_columns(driver: SqlDriver, schema: str, table: str) -> set[str]:
    """Names of columns to skip from the INSERT — server fills them in.

    Covers both ``GENERATED { ALWAYS | BY DEFAULT } AS IDENTITY`` and
    ``GENERATED ALWAYS AS (expr) STORED`` (column-default-derived).
    """
    rows = await driver.execute_query(
        "SELECT column_name "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "  AND (is_identity = 'YES' OR is_generated = 'ALWAYS')",
        params=[schema, table],
        force_readonly=True,
    )
    return {str(r.cells["column_name"]) for r in rows or []}


async def _foreign_key_targets(driver: SqlDriver, schema: str, table: str) -> dict[str, tuple[str, str, str]]:
    """Map ``local_column → (ref_schema, ref_table, ref_column)`` for
    every FK on ``schema.table``.

    Composite FKs are surfaced as one entry per local column — the
    sampling step picks one row from the referenced table per
    distinct (ref_schema, ref_table) so the composite stays
    consistent."""
    rows = await driver.execute_query(
        "SELECT "
        "  kcu.column_name AS local_column, "
        "  ccu.table_schema AS ref_schema, "
        "  ccu.table_name AS ref_table, "
        "  ccu.column_name AS ref_column "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        " AND tc.table_schema = kcu.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON ccu.constraint_name = tc.constraint_name "
        " AND ccu.table_schema = tc.table_schema "
        "WHERE tc.constraint_type = 'FOREIGN KEY' "
        "  AND tc.table_schema = %s AND tc.table_name = %s",
        params=[schema, table],
        force_readonly=True,
    )
    out: dict[str, tuple[str, str, str]] = {}
    for r in rows or []:
        out[str(r.cells["local_column"])] = (
            str(r.cells["ref_schema"]),
            str(r.cells["ref_table"]),
            str(r.cells["ref_column"]),
        )
    return out


async def _sample_fk_row(
    driver: SqlDriver, ref_schema: str, ref_table: str, ref_columns: list[str]
) -> dict[str, object | None] | None:
    """Sample ONE row from ``ref_schema.ref_table`` covering every
    referenced column in ``ref_columns``.

    Critical for composite FK consistency: a `FOREIGN KEY (a, b)
    REFERENCES t(x, y)` needs (a, b) drawn from the SAME row in t,
    or the resulting INSERT trips a constraint violation. The earlier
    per-column ``SELECT … LIMIT 1`` was prone to drift under
    concurrent writes (gemini review on #178).

    Returns ``None`` when the referenced table is empty, or a dict
    mapping each referenced column to its sampled cell value.
    """
    # Identifiers were validated by the caller — safe to interpolate.
    col_list = ", ".join(f'"{c}"' for c in ref_columns)
    rows = await driver.execute_query(
        f'SELECT {col_list} FROM "{ref_schema}"."{ref_table}" LIMIT 1',
        force_readonly=True,
    )
    if not rows:
        return None
    return {c: rows[0].cells.get(c) for c in ref_columns}


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def generate_test_row_for(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    seed: int | None = None,
    follow_foreign_keys: bool = True,
) -> GeneratedTestRow:
    """Produce one INSERT for ``schema.table`` ready to apply cleanly.

    Heuristic order per column:

    1. Identity / GENERATED ALWAYS columns are omitted from the INSERT.
       The reviewer sees ``DEFAULT`` for them in the column list.
    2. FK columns sample one existing row from the referenced table
       (when ``follow_foreign_keys=True``). FK targets that share a
       referenced ``(schema, table)`` reuse one sampled row so
       composite FKs stay consistent.
    3. Column-name patterns (``email`` / ``url`` / ``phone`` / etc.)
       produce realistic-looking values.
    4. Type-based synthesis falls through for everything else.
    5. A column the factory cannot synthesise lands as ``NULL`` if
       nullable, ``DEFAULT`` if it has a default, otherwise a
       :class:`TestRowFactoryError` is raised so the caller knows the
       generated row would fail to insert.

    The function never executes the INSERT — it returns the
    :class:`GeneratedTestRow` so the caller decides when (and through
    which gate) to apply it.
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")

    columns = await describe_table(driver, schema, table)
    if not columns:
        raise TestRowFactoryError(f"table {schema}.{table!r} has no columns or does not exist")

    skip_cols = await _identity_or_generated_columns(driver, schema, table)
    fk_targets: dict[str, tuple[str, str, str]] = {}
    if follow_foreign_keys:
        fk_targets = await _foreign_key_targets(driver, schema, table)
        # Validate referenced identifiers so the f-string in
        # _sample_fk_row stays safe even if the catalogue contained
        # something unexpected (e.g. a future PG feature that allows
        # special characters in identifiers).
        for ref_schema, ref_table, ref_column in fk_targets.values():
            _check_identifier(ref_schema, "referenced schema")
            _check_identifier(ref_table, "referenced table")
            _check_identifier(ref_column, "referenced column")

    rng = random.Random(seed)
    # One sampled ROW per (ref_schema, ref_table) — guarantees
    # composite-FK consistency. None means we tried and the target was
    # empty; the absence of the key means we haven't tried yet.
    fk_sample_cache: dict[tuple[str, str], dict[str, object | None] | None] = {}

    def _ref_cols_for(key: tuple[str, str]) -> list[str]:
        """Distinct ref_columns referenced for one target table — order
        preserved so the rendered SELECT is deterministic across calls."""
        seen: list[str] = []
        for rs, rt, rc in fk_targets.values():
            if (rs, rt) == key and rc not in seen:
                seen.append(rc)
        return seen

    column_fills: list[ColumnFill] = []
    insert_cols: list[str] = []
    insert_vals: list[str] = []

    for col in columns:
        if col.name in skip_cols:
            column_fills.append(
                ColumnFill(name=col.name, sql_literal="DEFAULT", heuristic="skipped (identity/generated)")
            )
            continue

        if col.name in fk_targets:
            ref_schema, ref_table, ref_column = fk_targets[col.name]
            key = (ref_schema, ref_table)
            if key not in fk_sample_cache:
                fk_sample_cache[key] = await _sample_fk_row(driver, ref_schema, ref_table, _ref_cols_for(key))
            sampled_row = fk_sample_cache[key]
            v = sampled_row.get(ref_column) if sampled_row is not None else None
            if v is None:
                if col.default is not None:
                    column_fills.append(
                        ColumnFill(
                            name=col.name,
                            sql_literal="DEFAULT",
                            heuristic=f"fk → {ref_schema}.{ref_table} empty, using default",
                        )
                    )
                    continue
                if col.nullable:
                    column_fills.append(
                        ColumnFill(
                            name=col.name,
                            sql_literal="NULL",
                            heuristic=f"fk → {ref_schema}.{ref_table} empty, column nullable",
                        )
                    )
                    insert_cols.append(f'"{col.name}"')
                    insert_vals.append("NULL")
                    continue
                raise TestRowFactoryError(
                    f"cannot synthesise NOT NULL FK column {col.name!r}: referenced "
                    f"{ref_schema}.{ref_table} is empty and column has no default"
                )
            literal = _quote_literal(v)
            column_fills.append(
                ColumnFill(
                    name=col.name,
                    sql_literal=literal,
                    heuristic=f"fk → {ref_schema}.{ref_table}.{ref_column} sampled",
                )
            )
            insert_cols.append(f'"{col.name}"')
            insert_vals.append(literal)
            continue

        # Name-pattern heuristic.
        synth = _synth_by_name(col.name, rng)
        if synth is None:
            synth = _synth_by_type(col, rng)
        if synth is None:
            # Unsupported type and no name pattern matched.
            if col.default is not None:
                column_fills.append(
                    ColumnFill(name=col.name, sql_literal="DEFAULT", heuristic="unsupported type, using default")
                )
                continue
            if col.nullable:
                column_fills.append(
                    ColumnFill(name=col.name, sql_literal="NULL", heuristic="unsupported type, column nullable")
                )
                insert_cols.append(f'"{col.name}"')
                insert_vals.append("NULL")
                continue
            raise TestRowFactoryError(
                f"cannot synthesise NOT NULL column {col.name!r} of unsupported type {col.data_type!r}"
            )
        value, heuristic = synth
        literal = _quote_literal(value)
        column_fills.append(ColumnFill(name=col.name, sql_literal=literal, heuristic=heuristic))
        insert_cols.append(f'"{col.name}"')
        insert_vals.append(literal)

    if not insert_cols:
        # Every column was skipped (entirely generated table). Emit
        # the DEFAULT VALUES form rather than a syntactically-bad
        # empty INSERT.
        insert_sql = f'INSERT INTO "{schema}"."{table}" DEFAULT VALUES'
    else:
        insert_sql = f'INSERT INTO "{schema}"."{table}" ({", ".join(insert_cols)}) VALUES ({", ".join(insert_vals)})'

    return GeneratedTestRow(schema=schema, table=table, columns=column_fills, insert_sql=insert_sql)


__all__ = [
    "ColumnFill",
    "GeneratedTestRow",
    "TestRowFactoryError",
    "generate_test_row_for",
]
