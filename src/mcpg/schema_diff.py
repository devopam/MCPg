"""Structural diff between two PostgreSQL schemas.

``compare_schemas`` reads both schemas through the existing introspection
primitives and returns a typed, hierarchical diff: tables added/removed/
changed, and per-changed-table the same trichotomy for columns, indexes,
constraints, and foreign keys. Object identity is by name; renames are
deliberately surfaced as a paired add + remove rather than guessed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields

from mcpg.introspection import (
    ColumnInfo,
    ConstraintInfo,
    ForeignKeyInfo,
    IndexInfo,
    TableInfo,
    describe_table,
    list_constraints,
    list_foreign_keys,
    list_indexes,
    list_tables,
)
from mcpg.sql import SqlDriver


@dataclass(frozen=True)
class ColumnChange:
    """A column that exists in both schemas but with different attributes.

    ``fields_changed`` lists the ``ColumnInfo`` field names that differ
    (e.g. ``["data_type", "nullable"]``).
    """

    name: str
    before: ColumnInfo
    after: ColumnInfo
    fields_changed: list[str]


@dataclass(frozen=True)
class IndexChange:
    """An index that exists in both schemas but with a different definition."""

    name: str
    before: IndexInfo
    after: IndexInfo


@dataclass(frozen=True)
class ConstraintChange:
    """A constraint that exists in both schemas but with a different definition."""

    name: str
    before: ConstraintInfo
    after: ConstraintInfo


@dataclass(frozen=True)
class ForeignKeyChange:
    """An FK that exists in both schemas but references different columns."""

    name: str
    before: ForeignKeyInfo
    after: ForeignKeyInfo


@dataclass(frozen=True)
class TableDiff:
    """The differences for a table present in both schemas."""

    table: str
    columns_added: list[ColumnInfo]
    columns_removed: list[ColumnInfo]
    columns_changed: list[ColumnChange]
    indexes_added: list[IndexInfo]
    indexes_removed: list[IndexInfo]
    indexes_changed: list[IndexChange]
    constraints_added: list[ConstraintInfo]
    constraints_removed: list[ConstraintInfo]
    constraints_changed: list[ConstraintChange]
    foreign_keys_added: list[ForeignKeyInfo]
    foreign_keys_removed: list[ForeignKeyInfo]
    foreign_keys_changed: list[ForeignKeyChange]


@dataclass(frozen=True)
class SchemaDiff:
    """The full structural diff between two schemas.

    Ordering of every list is stable (alphabetical by object name) so that
    repeated runs produce identical output and the diff is reviewable.
    """

    left_schema: str
    right_schema: str
    tables_added: list[TableInfo]
    tables_removed: list[TableInfo]
    tables_changed: list[TableDiff]


def _diff_by_name[T, C](
    left: list[T],
    right: list[T],
    *,
    name_of: Callable[[T], str],
    is_changed: Callable[[T, T], bool],
    make_change: Callable[[T, T], C],
) -> tuple[list[T], list[T], list[C]]:
    """Generic name-keyed added / removed / changed diff over two collections."""
    left_by_name = {name_of(item): item for item in left}
    right_by_name = {name_of(item): item for item in right}
    added = [right_by_name[name] for name in sorted(right_by_name) if name not in left_by_name]
    removed = [left_by_name[name] for name in sorted(left_by_name) if name not in right_by_name]
    changed: list[C] = []
    for name in sorted(left_by_name.keys() & right_by_name.keys()):
        before = left_by_name[name]
        after = right_by_name[name]
        if is_changed(before, after):
            changed.append(make_change(before, after))
    return added, removed, changed


_COLUMN_FIELDS = ("data_type", "nullable", "default", "vector_dimension")


def _column_fields_changed(before: ColumnInfo, after: ColumnInfo) -> list[str]:
    """Return the ColumnInfo field names where ``before`` and ``after`` differ."""
    return [field for field in _COLUMN_FIELDS if getattr(before, field) != getattr(after, field)]


def _table_diff_is_empty(diff: TableDiff) -> bool:
    """``True`` when every list on the diff is empty — nothing actually changed."""
    return all(not getattr(diff, field.name) for field in fields(diff) if isinstance(getattr(diff, field.name), list))


def _normalize_index_def(definition: str, schema: str) -> str:
    import re

    # Match single-quoted string literals (supporting '' and \') OR the schema qualifier
    pattern = rf"'(?:[^'\\]|\\.|'')*'|(?<![A-Za-z0-9_])\"?{re.escape(schema)}\"?\."

    def repl(match: re.Match[str]) -> str:
        val = match.group(0)
        if val.startswith("'"):
            return val
        return ""

    return re.sub(pattern, repl, definition)


def _normalize_fk_schema(to_schema: str, current_schema: str) -> str:
    return "" if to_schema == current_schema else to_schema


async def _compare_table(
    driver: SqlDriver,
    left_schema: str,
    right_schema: str,
    table: str,
    left_fks: list[ForeignKeyInfo],
    right_fks: list[ForeignKeyInfo],
) -> TableDiff:
    left_columns = await describe_table(driver, left_schema, table)
    right_columns = await describe_table(driver, right_schema, table)
    columns_added, columns_removed, columns_changed = _diff_by_name(
        left_columns,
        right_columns,
        name_of=lambda column: column.name,
        is_changed=lambda before, after: _column_fields_changed(before, after) != [],
        make_change=lambda before, after: ColumnChange(
            name=before.name,
            before=before,
            after=after,
            fields_changed=_column_fields_changed(before, after),
        ),
    )

    left_indexes = await list_indexes(driver, left_schema, table)
    right_indexes = await list_indexes(driver, right_schema, table)
    indexes_added, indexes_removed, indexes_changed = _diff_by_name(
        left_indexes,
        right_indexes,
        name_of=lambda index: index.name,
        is_changed=lambda before, after: (
            before.method != after.method
            or _normalize_index_def(before.definition, left_schema)
            != _normalize_index_def(after.definition, right_schema)
            or before.partitioned != after.partitioned
        ),
        make_change=lambda before, after: IndexChange(name=before.name, before=before, after=after),
    )

    left_constraints = await list_constraints(driver, left_schema, table)
    right_constraints = await list_constraints(driver, right_schema, table)
    constraints_added, constraints_removed, constraints_changed = _diff_by_name(
        left_constraints,
        right_constraints,
        name_of=lambda constraint: constraint.name,
        is_changed=lambda before, after: before.type != after.type or before.definition != after.definition,
        make_change=lambda before, after: ConstraintChange(name=before.name, before=before, after=after),
    )

    foreign_keys_added, foreign_keys_removed, foreign_keys_changed = _diff_by_name(
        left_fks,
        right_fks,
        name_of=lambda fk: fk.name,
        is_changed=lambda before, after: (
            before.from_columns != after.from_columns
            or _normalize_fk_schema(before.to_schema, left_schema)
            != _normalize_fk_schema(after.to_schema, right_schema)
            or before.to_table != after.to_table
            or before.to_columns != after.to_columns
        ),
        make_change=lambda before, after: ForeignKeyChange(name=before.name, before=before, after=after),
    )

    return TableDiff(
        table=table,
        columns_added=columns_added,
        columns_removed=columns_removed,
        columns_changed=columns_changed,
        indexes_added=indexes_added,
        indexes_removed=indexes_removed,
        indexes_changed=indexes_changed,
        constraints_added=constraints_added,
        constraints_removed=constraints_removed,
        constraints_changed=constraints_changed,
        foreign_keys_added=foreign_keys_added,
        foreign_keys_removed=foreign_keys_removed,
        foreign_keys_changed=foreign_keys_changed,
    )


async def compare_schemas(driver: SqlDriver, left_schema: str, right_schema: str) -> SchemaDiff:
    """Return the structural diff between two schemas.

    Only base tables are compared — views, foreign tables, custom types,
    functions, triggers, and policies are out of scope for this first cut.
    Partitions are included; an unchanged partition simply produces an
    empty ``TableDiff`` that is filtered out before returning.
    """
    left_tables = {table.name: table for table in await list_tables(driver, left_schema) if table.type == "BASE TABLE"}
    right_tables = {
        table.name: table for table in await list_tables(driver, right_schema) if table.type == "BASE TABLE"
    }

    tables_added = [right_tables[name] for name in sorted(right_tables) if name not in left_tables]
    tables_removed = [left_tables[name] for name in sorted(left_tables) if name not in right_tables]

    left_fks_by_table: dict[str, list[ForeignKeyInfo]] = {}
    for fk in await list_foreign_keys(driver, left_schema):
        left_fks_by_table.setdefault(fk.from_table, []).append(fk)
    right_fks_by_table: dict[str, list[ForeignKeyInfo]] = {}
    for fk in await list_foreign_keys(driver, right_schema):
        right_fks_by_table.setdefault(fk.from_table, []).append(fk)

    tables_changed: list[TableDiff] = []
    for name in sorted(left_tables.keys() & right_tables.keys()):
        diff = await _compare_table(
            driver,
            left_schema,
            right_schema,
            name,
            left_fks_by_table.get(name, []),
            right_fks_by_table.get(name, []),
        )
        if not _table_diff_is_empty(diff):
            tables_changed.append(diff)

    return SchemaDiff(
        left_schema=left_schema,
        right_schema=right_schema,
        tables_added=tables_added,
        tables_removed=tables_removed,
        tables_changed=tables_changed,
    )
