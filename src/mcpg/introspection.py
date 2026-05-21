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
    """A column of a table."""

    name: str
    data_type: str
    nullable: bool
    default: str | None


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """An index on a table."""

    name: str
    definition: str


@dataclass(frozen=True, slots=True)
class ExtensionInfo:
    """An installed PostgreSQL extension."""

    name: str
    version: str


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
    """Describe the columns of a table, in ordinal order."""
    rows = await driver.execute_query(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        params=[schema, table],
        force_readonly=True,
    )
    return [
        ColumnInfo(
            name=row.cells["column_name"],
            data_type=row.cells["data_type"],
            nullable=row.cells["is_nullable"] == "YES",
            default=row.cells["column_default"],
        )
        for row in rows or []
    ]


async def list_indexes(driver: SqlDriver, schema: str, table: str) -> list[IndexInfo]:
    """List the indexes defined on a table."""
    rows = await driver.execute_query(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
        params=[schema, table],
        force_readonly=True,
    )
    return [IndexInfo(name=row.cells["indexname"], definition=row.cells["indexdef"]) for row in rows or []]


async def list_extensions(driver: SqlDriver) -> list[ExtensionInfo]:
    """List the extensions installed in the database."""
    rows = await driver.execute_query(
        "SELECT extname, extversion FROM pg_extension ORDER BY extname",
        force_readonly=True,
    )
    return [ExtensionInfo(name=row.cells["extname"], version=row.cells["extversion"]) for row in rows or []]
