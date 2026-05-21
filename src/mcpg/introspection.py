"""Schema-introspection queries against the PostgreSQL catalog.

Each function runs a single read-only catalog query through a vendored
``SqlDriver`` and maps the rows to a typed result. Queries are parameterised;
no value is interpolated into SQL text.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# Schemas that belong to PostgreSQL itself rather than the user.
_SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    """A database schema."""

    name: str


@dataclass(frozen=True, slots=True)
class TableInfo:
    """A table or view within a schema."""

    name: str
    type: str


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """A column of a table.

    ``vector_dimension`` is set for ``pgvector`` ``vector(N)`` columns and is
    ``None`` for every other column type.
    """

    name: str
    data_type: str
    nullable: bool
    default: str | None
    vector_dimension: int | None


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """An index on a table.

    ``method`` is the access method — a built-in one (``btree``, ``gin``,
    ``gist``, ``brin``, ``hash``, ``spgist``) or an extension's (e.g.
    ``hnsw`` / ``ivfflat`` from ``pgvector``).
    """

    name: str
    method: str
    definition: str


@dataclass(frozen=True, slots=True)
class ExtensionInfo:
    """An installed PostgreSQL extension."""

    name: str
    version: str


@dataclass(frozen=True, slots=True)
class AvailableExtension:
    """An extension available to the database, installed or not."""

    name: str
    default_version: str
    installed_version: str | None
    installed: bool


def _is_system_schema(name: str) -> bool:
    return name in _SYSTEM_SCHEMAS or name.startswith("pg_")


async def list_schemas(driver: SqlDriver, *, include_system: bool = False) -> list[SchemaInfo]:
    """List schemas, excluding PostgreSQL's own schemas unless asked."""
    rows = await driver.execute_query(
        "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name",
        force_readonly=True,
    )
    schemas = [SchemaInfo(name=row.cells["schema_name"]) for row in rows or []]
    if include_system:
        return schemas
    return [schema for schema in schemas if not _is_system_schema(schema.name)]


async def list_tables(driver: SqlDriver, schema: str) -> list[TableInfo]:
    """List the tables and views in a schema."""
    rows = await driver.execute_query(
        "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
        params=[schema],
        force_readonly=True,
    )
    return [TableInfo(name=row.cells["table_name"], type=row.cells["table_type"]) for row in rows or []]


async def describe_table(driver: SqlDriver, schema: str, table: str) -> list[ColumnInfo]:
    """Describe the columns of a table, in ordinal order.

    Reads the catalog directly so the display type comes from ``format_type``
    and ``pgvector`` ``vector(N)`` columns report their dimension.
    """
    rows = await driver.execute_query(
        "SELECT a.attname AS column_name, "
        "format_type(a.atttypid, a.atttypmod) AS data_type, "
        "NOT a.attnotnull AS nullable, "
        "pg_get_expr(d.adbin, d.adrelid) AS column_default, "
        "t.typname AS type_name, a.atttypmod AS type_mod "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        params=[schema, table],
        force_readonly=True,
    )
    return [
        ColumnInfo(
            name=row.cells["column_name"],
            data_type=row.cells["data_type"],
            nullable=row.cells["nullable"],
            default=row.cells["column_default"],
            vector_dimension=(
                row.cells["type_mod"] if row.cells["type_name"] == "vector" and row.cells["type_mod"] > 0 else None
            ),
        )
        for row in rows or []
    ]


async def list_indexes(driver: SqlDriver, schema: str, table: str) -> list[IndexInfo]:
    """List the indexes defined on a table, with their access method."""
    rows = await driver.execute_query(
        "SELECT i.relname AS name, am.amname AS method, pg_get_indexdef(i.oid) AS definition "
        "FROM pg_class t "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN pg_index ix ON ix.indrelid = t.oid "
        "JOIN pg_class i ON i.oid = ix.indexrelid "
        "JOIN pg_am am ON am.oid = i.relam "
        "WHERE n.nspname = %s AND t.relname = %s ORDER BY i.relname",
        params=[schema, table],
        force_readonly=True,
    )
    return [
        IndexInfo(
            name=row.cells["name"],
            method=row.cells["method"],
            definition=row.cells["definition"],
        )
        for row in rows or []
    ]


async def list_extensions(driver: SqlDriver) -> list[ExtensionInfo]:
    """List the extensions installed in the database."""
    rows = await driver.execute_query(
        "SELECT extname, extversion FROM pg_extension ORDER BY extname",
        force_readonly=True,
    )
    return [ExtensionInfo(name=row.cells["extname"], version=row.cells["extversion"]) for row in rows or []]


async def list_available_extensions(driver: SqlDriver) -> list[AvailableExtension]:
    """List every extension available to the database, with install status."""
    rows = await driver.execute_query(
        "SELECT name, default_version, installed_version FROM pg_available_extensions ORDER BY name",
        force_readonly=True,
    )
    return [
        AvailableExtension(
            name=row.cells["name"],
            default_version=row.cells["default_version"],
            installed_version=row.cells["installed_version"],
            installed=row.cells["installed_version"] is not None,
        )
        for row in rows or []
    ]
