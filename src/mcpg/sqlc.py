"""Schema → sqlc-friendly SQL DDL exporter.

`sqlc <https://docs.sqlc.dev/>`_ compiles SQL to type-safe Go (or
Python/Kotlin) by reading a ``schema.sql`` file. This exporter emits
that file from a live PostgreSQL schema via :mod:`mcpg.introspection`
so an agent can hand sqlc a starting schema in one round trip.

Unlike :mod:`mcpg.prisma` / :mod:`mcpg.drizzle` / :mod:`mcpg.sqlalchemy_export`,
the output here is just clean SQL DDL — no foreign-DSL translation.
``pg_dump --schema-only`` would also work, but it requires the
subprocess gate; this in-process version lets a read-only deployment
generate the schema without enabling ``MCPG_ALLOW_SHELL``.

Coverage: base tables (columns, defaults, NOT NULL), primary / unique
/ check constraints, foreign keys (intra-schema), indexes, and enum
types. Views, foreign tables, partitions, triggers, functions, and
sequences (beyond serial defaults) are out of scope for v1 — the same
boundary as the other Batch G exporters.
"""

from __future__ import annotations

import re

from mcpg.introspection import (
    ColumnInfo,
    describe_table,
    list_constraints,
    list_enums,
    list_foreign_keys,
    list_indexes,
    list_tables,
)
from mcpg.sql import SqlDriver

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SqlcExportError(Exception):
    """Raised when an sqlc export call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise SqlcExportError(f"invalid {kind} name: {name!r}")


def _render_column_line(column: ColumnInfo) -> str:
    """Build one ``CREATE TABLE`` column line.

    Type comes straight from the catalog (already includes parens for
    varchar/numeric/etc.); NOT NULL and DEFAULT round-trip from the
    PG-side definition.
    """
    parts = [f'"{column.name}"', column.data_type]
    if not column.nullable:
        parts.append("NOT NULL")
    if column.default is not None:
        parts.append(f"DEFAULT {column.default}")
    return "    " + " ".join(parts)


def _render_enum(enum_name: str, values: list[str]) -> str:
    # PG's standard string-literal escape doubles the apostrophe; without
    # this an enum label like O'Brien produces broken DDL that sqlc and
    # psql both reject.
    literals = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f'CREATE TYPE "{enum_name}" AS ENUM ({literals});'


def _render_table(
    schema: str,
    table_name: str,
    columns: list[ColumnInfo],
) -> str:
    """Emit a ``CREATE TABLE`` statement (columns only, no constraints).

    Constraints are added as separate ``ALTER TABLE ... ADD CONSTRAINT``
    statements after every table is created so cross-table FKs (within
    the same schema) can reference tables defined later in the file.
    """
    column_lines = [_render_column_line(col) for col in columns]
    body = ",\n".join(column_lines)
    return f'CREATE TABLE "{schema}"."{table_name}" (\n{body}\n);'


def _render_constraint(schema: str, table_name: str, name: str, definition: str) -> str:
    return f'ALTER TABLE "{schema}"."{table_name}" ADD CONSTRAINT "{name}" {definition};'


def _render_index(definition: str) -> str:
    """pg_get_indexdef returns a complete CREATE INDEX statement."""
    return definition.rstrip(";") + ";"


_CONSTRAINT_ORDER = {"primary_key": 0, "unique": 1, "check": 2, "foreign_key": 3, "exclusion": 4}


async def generate_sqlc_schema(driver: SqlDriver, schema: str) -> str:
    """Emit a ``schema.sql`` for sqlc covering the base tables of ``schema``.

    The output is ordered:

    1. ``CREATE SCHEMA IF NOT EXISTS "<schema>";``
    2. ``CREATE TYPE`` statements for each enum.
    3. ``CREATE TABLE`` statements (columns only) for each base table.
    4. ``ALTER TABLE ... ADD CONSTRAINT`` for PK / unique / check /
       foreign key, in that order.
    5. ``CREATE INDEX`` statements for every non-constraint index.

    This ordering means the file replays cleanly against an empty
    database — FKs land after all referenced tables exist.

    Raises:
        SqlcExportError: When the schema (or any table/column) name
            requires PostgreSQL delimited-identifier quoting.
    """
    _check_identifier(schema, "schema")

    tables = [t for t in await list_tables(driver, schema) if t.type == "BASE TABLE" and not t.is_partition]
    for t in tables:
        _check_identifier(t.name, "table")

    enums = await list_enums(driver, schema)

    blocks: list[str] = [f'CREATE SCHEMA IF NOT EXISTS "{schema}";']
    if enums:
        for enum in sorted(enums, key=lambda e: e.name):
            blocks.append(_render_enum(enum.name, list(enum.values)))

    # 3. CREATE TABLE statements — columns only.
    table_columns: dict[str, list[ColumnInfo]] = {}
    for table in tables:
        columns = await describe_table(driver, schema, table.name)
        for col in columns:
            _check_identifier(col.name, "column")
        table_columns[table.name] = columns
        blocks.append(_render_table(schema, table.name, columns))

    # 4. ALTER TABLE ADD CONSTRAINT, ordered by constraint type so PK lands
    #    before FK (and unique indexes are created implicitly with PK/unique).
    constraint_blocks: list[str] = []
    constraints_by_table: dict[str, list[tuple[str, str, str]]] = {}
    for table in tables:
        cons = await list_constraints(driver, schema, table.name)
        triples = [(c.type, c.name, c.definition) for c in cons]
        triples.sort(key=lambda triple: (_CONSTRAINT_ORDER.get(triple[0], 9), triple[1]))
        constraints_by_table[table.name] = triples
        for _ctype, name, definition in triples:
            constraint_blocks.append(_render_constraint(schema, table.name, name, definition))

    # FK constraints on intra-schema targets come back via list_constraints
    # already (they're table constraints, not free-floating). The
    # cross-schema FKs are emitted with their literal definition; if the
    # other schema doesn't exist on replay, the agent will see an error
    # when sqlc compiles — easier to diagnose than silently dropping the FK.
    _ = await list_foreign_keys(driver, schema)

    blocks.extend(constraint_blocks)

    # 5. CREATE INDEX statements for indexes not created by PK / unique constraints.
    for table in tables:
        constraint_names = {name for _, name, _ in constraints_by_table[table.name]}
        for idx in await list_indexes(driver, schema, table.name):
            if idx.name in constraint_names:
                continue
            blocks.append(_render_index(idx.definition))

    return "\n\n".join(blocks) + "\n"
