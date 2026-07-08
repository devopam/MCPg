"""Schema-visualisation helpers.

``generate_schema_diagram`` builds a Mermaid ER diagram for a schema by
combining the existing introspection primitives. The result is a single
string in Mermaid's ``erDiagram`` syntax — agents can paste it into any
Mermaid-aware renderer (GitHub, Mermaid Live, IDEs).
"""

from __future__ import annotations

import re

from mcpg.introspection import (
    describe_table,
    list_constraints,
    list_foreign_keys,
    list_tables,
)
from mcpg.sql import SqlDriver

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

    foreign_keys = await list_foreign_keys(driver, schema)
    fk_columns_by_table: dict[str, set[str]] = {}
    for fk in foreign_keys:
        fk_columns_by_table.setdefault(fk.from_table, set()).update(fk.from_columns)

    lines = ["erDiagram"]
    for table in tables:
        columns = await describe_table(driver, schema, table.name)
        constraints = await list_constraints(driver, schema, table.name)
        pk_columns: set[str] = set()
        for constraint in constraints:
            if constraint.type == "primary_key":
                pk_columns |= _parse_pk_columns(constraint.definition)

        fk_columns = fk_columns_by_table.get(table.name, set())

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

    for fk in foreign_keys:
        if fk.from_table not in entity_names or fk.to_table not in entity_names:
            # Cross-schema FK or pointing to a filtered-out table — skip the
            # edge rather than emit a dangling reference.
            continue
        label = ",".join(fk.from_columns)
        lines.append(f'    {_sanitize(fk.to_table)} ||--o{{ {_sanitize(fk.from_table)} : "{label}"')

    return "\n".join(lines) + "\n"


# --- FK cascade graph (Phase 8.5) ----------------------------------------

_CASCADE_ACTIONS: dict[str, str] = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}

# Actions worth visualising — these are the ones that propagate a
# write to a related row. NO ACTION / RESTRICT block the parent
# operation but don't cause a cascade.
_INTERESTING_ACTIONS = frozenset({"c", "n", "d"})


async def generate_fk_cascade_graph(driver: SqlDriver, schema: str, *, include_all: bool = False) -> str:
    """Build a Mermaid ``graph LR`` of FK cascade chains in ``schema``.

    Each edge runs from the referencing table to the referenced table,
    labelled with the cascade action(s). By default only FKs with at
    least one CASCADE / SET NULL / SET DEFAULT action are included —
    those are the ones that produce a write blast radius. Pass
    ``include_all=True`` to include NO ACTION / RESTRICT FKs too
    (for a full FK topology view).

    Output is a single Mermaid string. Agents paste it into any
    Mermaid-aware renderer.
    """
    rows = await driver.execute_query(
        "SELECT con.conname AS fk_name, "
        "       c.relname AS from_table, "
        "       fn.nspname AS to_schema, "
        "       fc.relname AS to_table, "
        "       con.confdeltype AS on_delete, "
        "       con.confupdtype AS on_update "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_class fc ON fc.oid = con.confrelid "
        "JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
        "WHERE con.contype = 'f' AND n.nspname = %s "
        "ORDER BY c.relname, con.conname",
        params=[schema],
        force_readonly=True,
    )

    lines: list[str] = ["graph LR"]
    drawn_nodes: set[str] = set()
    drawn_edges = 0
    for row in rows or []:
        on_delete = _CASCADE_ACTIONS.get(str(row.cells["on_delete"]), "?")
        on_update = _CASCADE_ACTIONS.get(str(row.cells["on_update"]), "?")
        if not include_all and (
            str(row.cells["on_delete"]) not in _INTERESTING_ACTIONS
            and str(row.cells["on_update"]) not in _INTERESTING_ACTIONS
        ):
            continue

        from_table = str(row.cells["from_table"])
        to_table = str(row.cells["to_table"])
        to_schema = str(row.cells["to_schema"])
        # Cross-schema FK targets get an explicit prefix so the node is
        # distinct from any same-named table in the source schema.
        to_node = f"{to_schema}__{to_table}" if to_schema != schema else to_table

        for node, label in (
            (_sanitize(from_table), from_table),
            (_sanitize(to_node), f"{to_schema}.{to_table}" if to_schema != schema else to_table),
        ):
            if node not in drawn_nodes:
                lines.append(f'    {node}["{label}"]')
                drawn_nodes.add(node)

        edge_label_parts = []
        if str(row.cells["on_delete"]) in _INTERESTING_ACTIONS or include_all:
            edge_label_parts.append(f"DEL {on_delete}")
        if str(row.cells["on_update"]) in _INTERESTING_ACTIONS or include_all:
            edge_label_parts.append(f"UPD {on_update}")
        edge_label = " / ".join(edge_label_parts) if edge_label_parts else ""
        lines.append(f'    {_sanitize(from_table)} -->|"{edge_label}"| {_sanitize(to_node)}')
        drawn_edges += 1

    if drawn_edges == 0:
        # No cascade FKs found — emit a single-node graph with a note so
        # the agent gets a syntactically valid Mermaid output rather
        # than an empty string.
        lines.append(f'    none["no cascade foreign keys in schema {schema!r}"]')

    return "\n".join(lines) + "\n"
