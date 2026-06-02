"""Schema documentation generator.

Builds a detailed Markdown reference for a schema, listing each table's
columns (with types, nullability, keys, defaults, and comments), constraints,
indexes, and views/enums/domains.
"""

from __future__ import annotations

import re
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import (
    describe_table,
    list_constraints,
    list_enums,
    list_foreign_keys,
    list_foreign_tables,
    list_indexes,
    list_partitions,
    list_tables,
    list_views,
)

_PRIMARY_KEY_COLUMNS = re.compile(r"PRIMARY KEY \(([^)]+)\)", re.IGNORECASE)


def _parse_pk_columns(definition: str) -> set[str]:
    """Extract column names from a PRIMARY KEY (...) clause."""
    match = _PRIMARY_KEY_COLUMNS.search(definition)
    if not match:
        return set()
    return {column.strip().strip('"').strip() for column in match.group(1).split(",")}


async def _get_table_comments(driver: SqlDriver, schema: str) -> dict[str, str]:
    """Fetch descriptions (comments) for all tables/views in the schema."""
    rows = await driver.execute_query(
        "SELECT c.relname AS name, "
        "       obj_description(c.oid, 'pg_class') AS description "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'f')",
        params=[schema],
        force_readonly=True,
    )
    return {row.cells["name"]: row.cells["description"] for row in rows or [] if row.cells["description"]}


async def _get_column_comments(driver: SqlDriver, schema: str) -> dict[tuple[str, str], str]:
    """Fetch descriptions (comments) for all columns in the schema."""
    rows = await driver.execute_query(
        "SELECT c.relname AS table_name, "
        "       a.attname AS column_name, "
        "       col_description(c.oid, a.attnum) AS description "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND a.attnum > 0 AND NOT a.attisdropped",
        params=[schema],
        force_readonly=True,
    )
    return {
        (row.cells["table_name"], row.cells["column_name"]): row.cells["description"]
        for row in rows or []
        if row.cells["description"]
    }


def _escape_cell(val: Any) -> str:
    """Escape text for use in a Markdown table cell, collapsing newlines."""
    if val is None:
        return ""
    s = str(val).replace("|", "\\|")
    return s.replace("\n", "<br>")


async def generate_schema_docs(driver: SqlDriver, schema: str, *, include_samples: bool = False) -> str:
    """Build a detailed Markdown reference for all objects in ``schema``.

    Optionally samples the first 10 rows of each base table to extract up to 3
    distinct non-null values for each column when ``include_samples=True``.
    """
    tables_list = await list_tables(driver, schema)
    if not tables_list:
        # If there are no tables/views/enums in the schema, return a clean empty state.
        views_list = await list_views(driver, schema)
        enums_list = await list_enums(driver, schema)
        if not views_list and not enums_list:
            return f"# Schema Reference: {schema}\n\nSchema {schema!r} contains no tables, views, or custom types.\n"

    # Separate tables by kind.
    base_tables = [t for t in tables_list if t.type == "BASE TABLE"]
    views = await list_views(driver, schema)
    foreign_tables = await list_foreign_tables(driver, schema)
    enums = await list_enums(driver, schema)

    table_comments = await _get_table_comments(driver, schema)
    column_comments = await _get_column_comments(driver, schema)

    # Fetch global foreign keys to map column FK markers.
    foreign_keys = await list_foreign_keys(driver, schema)
    fk_columns_by_table: dict[str, set[str]] = {}
    for fk in foreign_keys:
        fk_columns_by_table.setdefault(fk.from_table, set()).update(fk.from_columns)

    lines = [f"# Schema Reference: {schema}\n"]
    overview = (
        f"This schema contains {len(base_tables)} tables, {len(views)} views, "
        f"{len(foreign_tables)} foreign tables, and {len(enums)} custom enums."
    )
    lines.append(overview + "\n")

    if base_tables:
        lines.append("## Tables\n")
        for table in base_tables:
            lines.append(f"### Table: {table.name}")

            # Partition info
            if table.partitioned:
                partition_set = await list_partitions(driver, schema, table.name)
                if partition_set.partitioned:
                    lines.append(f"*Partitioned Table (Strategy: {partition_set.strategy})*\n")
            elif table.is_partition:
                # Find parent table and bounds if possible (list_partitions helps if we queried all)
                lines.append("*Partition Table*\n")
            else:
                lines.append("")

            # Table description
            if table.name in table_comments:
                lines.append(table_comments[table.name] + "\n")

            # Describe columns
            columns = await describe_table(driver, schema, table.name)
            constraints = await list_constraints(driver, schema, table.name)

            # Map PK columns
            pk_columns: set[str] = set()
            for constraint in constraints:
                if constraint.type == "primary_key":
                    pk_columns |= _parse_pk_columns(constraint.definition)

            fk_columns = fk_columns_by_table.get(table.name, set())

            # Fetch sample values if requested
            column_samples: dict[str, str] = {}
            if include_samples:
                try:
                    # Double-quote table and schema names to avoid identifier issues.
                    sample_rows = await driver.execute_query(
                        f'SELECT * FROM "{schema}"."{table.name}" LIMIT 10',
                        force_readonly=True,
                    )
                    if sample_rows:
                        # Collect distinct non-null values for each column
                        col_vals: dict[str, list[Any]] = {}
                        for row in sample_rows:
                            for col_name, val in row.cells.items():
                                if val is not None:
                                    col_vals.setdefault(col_name, [])
                                    if val not in col_vals[col_name]:
                                        col_vals[col_name].append(val)

                        for col_name, vals in col_vals.items():
                            truncated_vals = []
                            for v in vals[:3]:
                                s = str(v)
                                if len(s) > 50:
                                    s = s[:47] + "..."
                                truncated_vals.append(s)
                            column_samples[col_name] = ", ".join(truncated_vals)
                except Exception:
                    # Best effort: ignore failures (e.g. from empty custom types or permission blocks).
                    pass

            # Build columns Markdown table
            headers = ["Column", "Type", "Nullable", "Key", "Default", "Description"]
            alignments = ["---|---|---|---|---|---"]
            if include_samples:
                headers.append("Sample Values")
                alignments.append("---")

            lines.append("| " + " | ".join(headers) + " |")
            lines.append("| " + " | ".join(alignments) + " |")

            for col in columns:
                col_name = col.name
                col_type = col.data_type
                if col.vector_dimension is not None:
                    col_type = f"vector({col.vector_dimension})"

                nullable_str = "Yes" if col.nullable else "No"

                # Key marker
                keys = []
                if col_name in pk_columns:
                    keys.append("PK")
                if col_name in fk_columns:
                    keys.append("FK")
                key_str = ", ".join(keys)

                default_str = col.default if col.default is not None else ""
                desc_str = column_comments.get((table.name, col_name), "")

                row_cells = [
                    _escape_cell(col_name),
                    _escape_cell(col_type),
                    nullable_str,
                    key_str,
                    _escape_cell(default_str),
                    _escape_cell(desc_str),
                ]
                if include_samples:
                    row_cells.append(_escape_cell(column_samples.get(col_name, "")))

                lines.append("| " + " | ".join(row_cells) + " |")

            lines.append("")

            # Table Constraints
            if constraints:
                lines.append("**Constraints:**")
                for con in constraints:
                    con_type_label = con.type.replace("_", " ").title()
                    lines.append(f"- **{con.name}** ({con_type_label}): `{con.definition}`")
                lines.append("")

            # Indexes
            indexes = await list_indexes(driver, schema, table.name)
            if indexes:
                lines.append("**Indexes:**")
                for idx in indexes:
                    lines.append(f"- **{idx.name}** ({idx.method}): `{idx.definition}`")
                lines.append("")

            lines.append("---")
            lines.append("")

    if views:
        lines.append("## Views\n")
        for view in views:
            lines.append(f"### View: {view.name}")
            if view.name in table_comments:
                lines.append(table_comments[view.name] + "\n")

            lines.append("**Query Definition:**")
            lines.append("```sql")
            lines.append(view.definition.strip())
            lines.append("```")
            lines.append("")
            lines.append("---")
            lines.append("")

    if foreign_tables:
        lines.append("## Foreign Tables\n")
        for ft in foreign_tables:
            lines.append(f"### Foreign Table: {ft.name}")
            if ft.name in table_comments:
                lines.append(table_comments[ft.name] + "\n")
            lines.append(f"Server: `{ft.server}`")
            if ft.options:
                opt_str = ", ".join(f"{k}={v}" for k, v in ft.options.items())
                lines.append(f"Options: `{opt_str}`")
            lines.append("")
            lines.append("---")
            lines.append("")

    if enums:
        lines.append("## Custom Enums\n")
        for enum in enums:
            lines.append(f"### Enum: {enum.name}")
            val_str = ", ".join(f"'{v}'" for v in enum.values)
            lines.append(f"Values: {val_str}")
            lines.append("")

    # Ensure clean trailing newline.
    res = "\n".join(lines).strip() + "\n"
    # Remove duplicate consecutive divider lines if they exist at the very end
    if res.endswith("---\n\n---\n"):
        res = res[:-5]
    return res
