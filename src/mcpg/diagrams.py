"""Schema-visualisation helpers.

``generate_schema_diagram`` builds a Mermaid ER diagram for a schema by
combining the existing introspection primitives. The result is a single
string in Mermaid's ``erDiagram`` syntax — agents can paste it into any
Mermaid-aware renderer (GitHub, Mermaid Live, IDEs).
"""

from __future__ import annotations

import re

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import (
    describe_table,
    list_constraints,
    list_foreign_keys,
    list_tables,
)

_PRIMARY_KEY_COLUMNS = re.compile(r"PRIMARY KEY \(([^)]+)\)", re.IGNORECASE)
_NON_IDENT = re.compile(r"[^A-Za-z0-9]+")


def _sanitize(text: str) -> str:
    """Reduce arbitrary text to a Mermaid-friendly identifier-like token.

    Mermaid ER attribute types and entity names must be alphanumeric-ish;
    runs of non-alphanumeric characters are collapsed to a single
    underscore and surrounding underscores stripped.
    """
    cleaned = _NON_IDENT.sub("_", text).strip("_")
    return cleaned or "_"


def _parse_pk_columns(definition: str) -> set[str]:
    """Extract the column names from a ``PRIMARY KEY (...)`` clause."""
    match = _PRIMARY_KEY_COLUMNS.search(definition)
    if not match:
        return set()
    return {column.strip().strip('"') for column in match.group(1).split(",")}


async def generate_schema_diagram(driver: SqlDriver, schema: str, *, include_partitions: bool = False) -> str:
    """Render a Mermaid ER diagram for the tables in ``schema``.

    Views and foreign tables are skipped. Partitions are skipped by
    default — set ``include_partitions=True`` to draw each partition as
    its own entity alongside the partitioned parent.
    """
    tables = [table for table in await list_tables(driver, schema) if table.type == "BASE TABLE"]
    if not include_partitions:
        tables = [table for table in tables if not table.is_partition]
    entity_names = {table.name for table in tables}

    lines = ["erDiagram"]
    for table in tables:
        columns = await describe_table(driver, schema, table.name)
        constraints = await list_constraints(driver, schema, table.name)
        pk_columns: set[str] = set()
        for constraint in constraints:
            if constraint.type == "primary_key":
                pk_columns |= _parse_pk_columns(constraint.definition)

        fk_columns: set[str] = set()
        for fk in await list_foreign_keys(driver, schema):
            if fk.from_table == table.name:
                fk_columns |= set(fk.from_columns)

        lines.append(f"    {_sanitize(table.name)} {{")
        for column in columns:
            attrs: list[str] = []
            if column.name in pk_columns:
                attrs.append("PK")
            if column.name in fk_columns:
                attrs.append("FK")
            suffix = f" {' '.join(attrs)}" if attrs else ""
            lines.append(f"        {_sanitize(column.data_type)} {_sanitize(column.name)}{suffix}")
        lines.append("    }")

    for fk in await list_foreign_keys(driver, schema):
        if fk.from_table not in entity_names or fk.to_table not in entity_names:
            # Cross-schema FK or pointing to a filtered-out table — skip the
            # edge rather than emit a dangling reference.
            continue
        label = ",".join(fk.from_columns)
        lines.append(f'    {_sanitize(fk.to_table)} ||--o{{ {_sanitize(fk.from_table)} : "{label}"')

    return "\n".join(lines) + "\n"
