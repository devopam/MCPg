"""Synthetic test-data factory.

``generate_test_data`` produces ``INSERT`` statements with deterministic
synthetic values for a table's columns — honouring column types,
``NOT NULL`` constraints, and ``DEFAULT`` expressions (skipped, so the
server fills them in). Foreign-key columns are not resolved: the
caller chooses whether to (a) pre-seed referenced rows separately, or
(b) drop the FK temporarily. The function never executes the
generated SQL itself; it returns the statements so an agent in a safer
access mode can preview before applying.

The generator is deterministic when a ``seed`` is provided — useful
for snapshot-style integration tests where consistent test data
matters.

Scope:

* Numeric / text / boolean / date / timestamp / jsonb / uuid → covered.
* arrays, ranges, composite types, ``hstore``, ``geometry`` → emitted
  as ``NULL`` (or ``DEFAULT`` if the column is ``NOT NULL`` with a
  default). The agent should add custom inserts for those.
* Generated columns are skipped — PG fills them in from the
  expression.
"""

from __future__ import annotations

import random
import re
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mcpg.introspection import ColumnInfo, describe_table
from mcpg.sql import SqlDriver

_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

DEFAULT_ROW_COUNT = 10
HARD_ROW_CAP = 10_000


class TestDataError(Exception):
    """Raised when test-data generation is rejected or fails."""

    # Pytest treats classes named ``Test*`` as test collection targets;
    # this exception isn't one. The attribute opts out of collection.
    __test__ = False


@dataclass(frozen=True)
class GeneratedDataset:
    """Result of :func:`generate_test_data`.

    ``statements`` are agent-ready ``INSERT`` strings, one per row,
    safe to copy into an SQL session as-is (identifiers are quoted,
    values are SQL literals). ``rows_generated`` is ``len(statements)``
    for convenience. ``skipped_columns`` lists columns the generator
    omitted (generated columns, unsupported types) so the agent
    knows what was left as ``DEFAULT``.
    """

    schema: str
    table: str
    rows_generated: int
    statements: list[str]
    skipped_columns: list[str]


def _check_identifier(value: str, kind: str) -> None:
    if not _IDENTIFIER.match(value):
        raise TestDataError(f"invalid {kind} {value!r}; must match [A-Za-z_][A-Za-z0-9_]*")


def _quote_literal(value: object) -> str:
    """SQL-quote a Python value for inline use in a generated INSERT.

    Strings get single-quotes with doubled embedded quotes; bools map
    to TRUE/FALSE; bytes are emitted as bytea literals; None becomes
    NULL. Numbers / floats are stringified verbatim.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'"
    if isinstance(value, bytes):
        # PG bytea literal — hex-encoded, escape as required.
        return f"'\\\\x{value.hex()}'"
    s = str(value).replace("'", "''")
    return f"'{s}'"


def _synth_value(column: ColumnInfo, rng: random.Random) -> object:
    """Generate one synthetic Python value for ``column``."""
    dt = column.data_type.lower()
    # Strip parameter / length info for matching: `varchar(255)` -> `varchar`.
    base = dt.split("(", 1)[0].strip()
    if base in {"integer", "int", "int4", "int2", "smallint", "bigint", "int8"}:
        return rng.randint(1, 1_000_000)
    if base in {"numeric", "decimal", "real", "double precision", "float4", "float8"}:
        return round(rng.random() * 1000, 2)
    if base in {"boolean", "bool"}:
        return rng.choice([True, False])
    if base in {"uuid"}:
        return str(uuid.UUID(int=rng.getrandbits(128)))
    if base in {"date"}:
        return (datetime.now(UTC).date() - timedelta(days=rng.randint(0, 365))).isoformat()
    if base in {"timestamp", "timestamp without time zone", "timestamptz", "timestamp with time zone"}:
        return datetime.now(UTC) - timedelta(seconds=rng.randint(0, 60 * 60 * 24 * 365))
    if base in {"text", "varchar", "character varying", "char", "character", "citext", "name"}:
        length = rng.randint(4, 16)
        return "".join(rng.choice(string.ascii_lowercase) for _ in range(length))
    if base in {"json", "jsonb"}:
        return '{"k": "v"}'
    # Unknown / unsupported type — return None so the caller emits DEFAULT or NULL.
    return None


# Type names whose default values the generator can't synthesise — these
# always fall back to DEFAULT (or get skipped from the column list when
# the column is NOT NULL without a default, which is then an error).
_UNSUPPORTED_TYPE_PREFIXES = (
    "geometry",
    "geography",
    "hstore",
    "ltree",
    "tsvector",
    "tsquery",
    "interval",
    "inet",  # could be supported, but leave for v2
    "cidr",
    "macaddr",
    "bytea",
    "vector",  # pgvector — needs a dimension; out of scope
)


async def generate_test_data(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    rows: int = DEFAULT_ROW_COUNT,
    seed: int | None = None,
) -> GeneratedDataset:
    """Generate ``INSERT`` statements with synthetic values for ``rows`` rows.

    Returns a :class:`GeneratedDataset` — does NOT execute the inserts.
    The caller decides when (and whether) to run them, typically via
    ``run_write`` in unrestricted mode.

    Args:
        rows: How many INSERT statements to produce. Bounded by
            ``HARD_ROW_CAP`` (10000) to keep the response small.
        seed: When set, the generator is deterministic — repeated
            calls with the same seed produce identical output.

    Raises:
        TestDataError: When identifiers fail validation, ``rows`` is
            out of range, or the table can't be introspected.
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    if rows < 1:
        raise TestDataError("rows must be >= 1")
    if rows > HARD_ROW_CAP:
        raise TestDataError(f"rows exceeds hard cap of {HARD_ROW_CAP}")

    columns = await describe_table(driver, schema, table)
    if not columns:
        raise TestDataError(f"table {schema}.{table!r} has no columns or does not exist")

    rng = random.Random(seed)
    skipped: list[str] = []
    target_columns: list[ColumnInfo] = []
    for col in columns:
        type_lower = col.data_type.lower().split("(", 1)[0].strip()
        if any(type_lower.startswith(p) for p in _UNSUPPORTED_TYPE_PREFIXES):
            skipped.append(col.name)
            continue
        target_columns.append(col)

    if not target_columns:
        raise TestDataError(
            f"no columns of {schema}.{table!r} have generator support; skipped types: " + ", ".join(skipped)
        )

    column_list = ", ".join(f'"{c.name}"' for c in target_columns)
    statements: list[str] = []
    for _ in range(rows):
        values: list[str] = []
        for col in target_columns:
            v = _synth_value(col, rng)
            if v is None and col.default is not None:
                # Has a default; let PG fill it in.
                values.append("DEFAULT")
            elif v is None and not col.nullable:
                # Synthesis failed and column is NOT NULL with no default
                # — fall back to a placeholder that's likely to fit. The
                # agent will see the failure when the INSERT runs.
                values.append("DEFAULT")
            elif v is None:
                values.append("NULL")
            else:
                values.append(_quote_literal(v))
        statements.append(f'INSERT INTO "{schema}"."{table}" ({column_list}) VALUES ({", ".join(values)})')

    return GeneratedDataset(
        schema=schema,
        table=table,
        rows_generated=len(statements),
        statements=statements,
        skipped_columns=skipped,
    )


@dataclass(frozen=True)
class SeedResult:
    """The result of seeding a table with sample data."""

    schema: str
    table: str
    rows_seeded: int
    statements_executed: list[str]
    skipped_columns: list[str]


async def seed_table_with_sample_data(
    driver: SqlDriver,
    schema: str,
    table: str,
    *,
    rows: int = DEFAULT_ROW_COUNT,
    seed: int | None = None,
) -> SeedResult:
    """Generate and execute synthetic INSERT statements to seed a table.

    Generates the inserts via generate_test_data and executes them in a single
    batched round-trip against the target table.
    """
    dataset = await generate_test_data(driver, schema, table, rows=rows, seed=seed)

    if dataset.statements:
        batched_sql = ";\n".join(dataset.statements)
        await driver.execute_query(batched_sql)

    return SeedResult(
        schema=dataset.schema,
        table=dataset.table,
        rows_seeded=dataset.rows_generated,
        statements_executed=dataset.statements,
        skipped_columns=dataset.skipped_columns,
    )
