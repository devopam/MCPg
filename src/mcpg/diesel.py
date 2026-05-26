"""Schema → Diesel ORM (Rust) exporter.

Reads the PostgreSQL catalog via :mod:`mcpg.introspection` and emits a
``schema.rs`` an agent can drop into a Rust project using
`Diesel <https://diesel.rs/>`_. Mirrors the structure of the other Batch G
exporters (Prisma / Drizzle / SQLAlchemy 2.0 / sqlc) — same coverage
boundary: base tables, columns, primary keys, foreign keys, and enum
types. Views, foreign tables, partitions, triggers, functions, and
composite types are out of scope.

Diesel's idiom is one ``table!`` block per table plus ``joinable!`` and
``allow_tables_to_appear_in_same_query!`` declarations for joins, so the
output reads like ``diesel print-schema``. Enum types are emitted as a
sibling `pg_enum` module — Diesel does not have a first-class enum type;
agents typically declare a wrapper that maps to a ``Text`` column or a
custom SQL type. We pick the wrapper-over-Text route because it works
out of the box without ``diesel_derive_enum``.
"""

from __future__ import annotations

import re

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import (
    ColumnInfo,
    describe_table,
    list_constraints,
    list_enums,
    list_foreign_keys,
    list_tables,
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class DieselExportError(Exception):
    """Raised when a Diesel export call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise DieselExportError(f"invalid {kind} name: {name!r}")


def _strip_type_params(data_type: str) -> str:
    return re.sub(r"\(.*\)", "", data_type).strip()


# Map a PG type to a Diesel sql_types name. The values here match the
# names Diesel re-exports from ``diesel::pg::sql_types`` / ``diesel::sql_types``.
_TYPE_MAP: dict[str, str] = {
    "smallint": "SmallInt",
    "integer": "Integer",
    "bigint": "BigInt",
    "real": "Float",
    "double precision": "Double",
    "numeric": "Numeric",
    "decimal": "Numeric",
    "boolean": "Bool",
    "text": "Text",
    "character varying": "Varchar",
    "varchar": "Varchar",
    "character": "Bpchar",
    "char": "Bpchar",
    "uuid": "Uuid",
    "json": "Json",
    "jsonb": "Jsonb",
    "date": "Date",
    "time without time zone": "Time",
    "time with time zone": "Timetz",
    "timestamp without time zone": "Timestamp",
    "timestamp with time zone": "Timestamptz",
    "bytea": "Bytea",
    "interval": "Interval",
    "inet": "Inet",
    "cidr": "Cidr",
    "macaddr": "Macaddr",
}


def _diesel_sql_type(column: ColumnInfo, enum_names: set[str]) -> str:
    """Return the Diesel SQL type expression for a column.

    Enum columns map to ``Text`` so the agent can use a String-backed
    wrapper enum without pulling in ``diesel_derive_enum``. ``Nullable<T>``
    wraps when the column is nullable.
    """
    raw = column.data_type
    base = _strip_type_params(raw).lower()
    qualified = base.split(".")[-1] if "." in base else base
    inner: str
    if base in enum_names or raw in enum_names or qualified in enum_names:
        # Backed-by-Text wrapper enum; see module docstring for why.
        inner = "Text"
    else:
        inner = _TYPE_MAP.get(base, "Text")
    if column.nullable:
        return f"Nullable<{inner}>"
    return inner


_PK_COLS_RE = re.compile(r"PRIMARY KEY\s*\(([^)]+)\)", re.IGNORECASE)


def _parse_pk_columns(definition: str) -> list[str]:
    match = _PK_COLS_RE.search(definition)
    if not match:
        return []
    return [col.strip().strip('"') for col in match.group(1).split(",")]


def _render_table_block(
    table_name: str,
    columns: list[ColumnInfo],
    pk_columns: list[str],
    enum_names: set[str],
) -> str:
    """Build the ``table! { ... }`` block for a table.

    Diesel's macro syntax:

    .. code-block:: rust

       table! {
           widget (id) {
               id -> Integer,
               name -> Text,
               quantity -> Nullable<Integer>,
           }
       }
    """
    pk_clause = " (" + ", ".join(pk_columns) + ")" if pk_columns else ""
    lines: list[str] = []
    lines.append("table! {")
    lines.append(f"    {table_name}{pk_clause} {{")
    for column in columns:
        sql_type = _diesel_sql_type(column, enum_names)
        lines.append(f"        {column.name} -> {sql_type},")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


def _render_joinable(from_table: str, to_table: str, fk_column: str) -> str:
    """Emit a ``joinable!`` macro tying two tables on the FK column."""
    return f"joinable!({from_table} -> {to_table} ({fk_column}));"


def _render_allow_join(table_names: list[str]) -> str:
    """Emit the ``allow_tables_to_appear_in_same_query!`` macro for joins."""
    joined = ", ".join(sorted(table_names))
    return f"allow_tables_to_appear_in_same_query!({joined});"


def _render_enum_module(enum_names: list[tuple[str, list[str]]]) -> str:
    """Emit a ``pg_enum`` module with a wrapper for each enum type.

    Each enum becomes a public ``enum`` that derives the traits Diesel
    needs to round-trip the value through a Text column. The agent
    can extend with their preferred conversion (``ToSql`` / ``FromSql``)
    once they decide between the Text-backed wrapper and a true PG
    enum type.
    """
    if not enum_names:
        return ""
    body_lines = [
        "// Wrapper enums for the PG enum types in this schema. They map",
        "// to a Text column out of the box; replace with diesel_derive_enum",
        "// + an explicit SqlType if you want native PG enum mapping.",
        "pub mod pg_enum {",
    ]
    for name, values in enum_names:
        body_lines.append("    #[derive(Debug, Clone, PartialEq, Eq, Hash, diesel::AsExpression, diesel::FromSqlRow)]")
        body_lines.append("    #[diesel(sql_type = diesel::sql_types::Text)]")
        body_lines.append(f"    pub enum {_pascal(name)} {{")
        for value in values:
            body_lines.append(f"        {_pascal(value)},")
        body_lines.append("    }")
    body_lines.append("}")
    return "\n".join(body_lines)


def _pascal(name: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", name)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or name


async def generate_diesel_schema(driver: SqlDriver, schema: str) -> str:
    """Emit a Diesel ORM ``schema.rs`` for ``schema``.

    Returns a Rust source string. The output uses ``table!`` blocks,
    ``joinable!`` for single-column intra-schema FKs, and an
    ``allow_tables_to_appear_in_same_query!`` line so multi-table joins
    type-check without per-pair ``allow!`` macros.

    Raises:
        DieselExportError: When the schema name or any table/column
            name requires PostgreSQL delimited-identifier quoting.
    """
    _check_identifier(schema, "schema")
    tables = [t for t in await list_tables(driver, schema) if t.type == "BASE TABLE" and not t.is_partition]
    for t in tables:
        _check_identifier(t.name, "table")

    enums = await list_enums(driver, schema)
    enum_names = {e.name for e in enums}

    fks_all = await list_foreign_keys(driver, schema)
    entity_names = {t.name for t in tables}

    blocks: list[str] = []

    # 1. Enum wrapper module (if any enums exist).
    enum_module = _render_enum_module([(e.name, list(e.values)) for e in sorted(enums, key=lambda e: e.name)])
    if enum_module:
        blocks.append(enum_module)

    # 2. One table! macro per table.
    for table in tables:
        columns = await describe_table(driver, schema, table.name)
        for col in columns:
            _check_identifier(col.name, "column")
        constraints = await list_constraints(driver, schema, table.name)
        pk_columns: list[str] = []
        for con in constraints:
            if con.type == "primary_key":
                pk_columns = _parse_pk_columns(con.definition)
                break
        blocks.append(_render_table_block(table.name, columns, pk_columns, enum_names))

    # 3. joinable! for every single-column intra-schema FK.
    joinable_lines: list[str] = []
    for fk in fks_all:
        if fk.to_table not in entity_names:
            continue  # cross-schema FK — Diesel's joinable! can't span schemas cleanly
        if len(fk.from_columns) != 1:
            continue  # composite FKs are a documented v1 gap
        joinable_lines.append(_render_joinable(fk.from_table, fk.to_table, fk.from_columns[0]))
    if joinable_lines:
        blocks.append("\n".join(joinable_lines))

    # 4. allow_tables_to_appear_in_same_query! for the full table set.
    if len(tables) >= 2:
        blocks.append(_render_allow_join([t.name for t in tables]))

    return "\n\n".join(blocks) + "\n"
