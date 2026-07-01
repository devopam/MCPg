"""Relational → Apache AGE graph projection generator (emit-don't-execute).

``generate_graph_projection`` inspects a relational schema (tables, primary
keys, foreign keys) and emits the openCypher ``CREATE`` / ``MERGE`` statements
that project it into an Apache AGE property graph — rows become vertices,
foreign keys become edges. It is the graph analogue of
:func:`mcpg.test_data.generate_test_data` and
:func:`mcpg.warehousepg.recommend_redistribute`: a READ-ONLY generator that
NEVER executes what it produces. The operator reviews the returned statements
and runs them (in order — nodes before edges) in a safer access mode.

Two modes, selected by ``row_limit``:

* ``row_limit=0`` (default) — a **schema-level plan**: one template
  ``CREATE`` per node label and one template ``MERGE`` per edge type, with
  ``$property`` placeholders. No table rows are read; only the catalog is
  touched.
* ``row_limit>0`` — also emits **concrete per-row** statements (up to
  ``row_limit`` rows per table), with real values escaped for Cypher
  (single quotes doubled, NULL properties omitted, numbers/bools bare).

AGE presence is probed for an advisory ``available`` flag, but the generator
reads only the relational catalog, so it still emits statements when AGE is
not installed.

Security posture:

* Every schema / table / graph name is identifier-validated against
  ``[A-Za-z_][A-Za-z0-9_]*`` before it reaches a generated string; anything
  else is rejected. This is the injection guard — labels and edge types go
  into the Cypher text verbatim after validation.
* Row values are single-quote escaped (``'`` → ``''``) for the Cypher string
  literal. The generated statements are for review, never executed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import ColumnInfo, describe_table, list_foreign_keys

_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Cap on rows sampled per table in row mode, to bound response size.
HARD_ROW_CAP = 1000

# Base-table list for a schema (ordinary tables only, ``relkind='r'``).
_LIST_BASE_TABLES_SQL = (
    "SELECT c.relname AS name "
    "FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'r' AND n.nspname = %s "
    "ORDER BY c.relname"
)

# Primary-key columns of a table, in key order. Covers composite PKs.
_PRIMARY_KEY_SQL = (
    "SELECT a.attname AS column_name "
    "FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
    "WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary = true "
    "ORDER BY array_position(i.indkey, a.attnum)"
)

# AGE presence probe — advisory only.
_AGE_PROBE_SQL = "SELECT 1 FROM ag_catalog.ag_graph LIMIT 1"

# Cypher value rendering: text-like types get quoted, numerics are bare,
# booleans map to true/false. Matched against the base of ColumnInfo.data_type.
_NUMERIC_TYPES = frozenset(
    {
        "integer",
        "int",
        "int2",
        "int4",
        "int8",
        "smallint",
        "bigint",
        "numeric",
        "decimal",
        "real",
        "double precision",
        "float4",
        "float8",
        "money",
    }
)
_BOOLEAN_TYPES = frozenset({"boolean", "bool"})


class GraphProjectionError(Exception):
    """Raised when a graph-projection request is rejected."""


@dataclass(frozen=True)
class NodeLabel:
    """A vertex label projected from a relational table.

    ``label`` is the AGE vertex label (the table name). ``key_columns`` are
    the table's primary-key columns (empty when the table has no PK — such a
    table still gets node ``CREATE`` statements but cannot be reliably
    ``MATCH``ed, so its edges are skipped). ``property_columns`` are all
    columns carried as vertex properties.
    """

    label: str
    source_table: str
    key_columns: list[str]
    property_columns: list[str]


@dataclass(frozen=True)
class EdgeType:
    """An edge type projected from a foreign key.

    The edge points from the referencing (child) table's vertex to the
    referenced (parent) table's vertex. ``from_key`` / ``to_key`` are the
    aligned FK column lists used to bind the endpoints.
    """

    edge_type: str
    from_label: str
    to_label: str
    from_key: list[str]
    to_key: list[str]
    fk_name: str


@dataclass(frozen=True)
class GraphProjection:
    """Result of :func:`generate_graph_projection`.

    ``available`` is the advisory AGE-installed flag (statements are emitted
    regardless). ``cypher_statements`` are the generated
    ``SELECT * FROM cypher(...)`` wrappers — for review, NEVER executed by
    this tool. ``warnings`` flags keyless tables, the AGE materialisation
    caveat, and ordering guidance.
    """

    available: bool
    schema: str
    graph_name: str
    row_limit: int
    node_labels: list[NodeLabel]
    edge_types: list[EdgeType]
    cypher_statements: list[str]
    warnings: list[str]
    detail: str


def _check_identifier(value: str, kind: str) -> None:
    if not _IDENTIFIER.match(value):
        raise GraphProjectionError(f"invalid {kind} {value!r}; must match [A-Za-z_][A-Za-z0-9_]*")


def _wrap(graph_name: str, cypher: str) -> str:
    """Wrap a Cypher body in the AGE ``cypher(...)`` SQL call."""
    return f"SELECT * FROM cypher('{graph_name}', $$ {cypher} $$);"


def _render_value(value: object, column: ColumnInfo) -> str:
    """Render a Python value as a Cypher literal for ``column``'s type.

    Text / date / uuid / json → single-quoted with ``'`` doubled. Numerics →
    bare. Booleans → ``true`` / ``false``. Callers omit NULLs before calling.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    base = column.data_type.lower().split("(", 1)[0].strip()
    if base in _BOOLEAN_TYPES:
        return "true" if value else "false"
    if base in _NUMERIC_TYPES and isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


async def _primary_key_columns(driver: SqlDriver, schema: str, table: str) -> list[str]:
    """Return a table's primary-key columns in key order (empty when none)."""
    rows = await driver.execute_query(_PRIMARY_KEY_SQL, params=[schema, table], force_readonly=True)
    return [str(row.cells["column_name"]) for row in rows or []]


async def _list_base_tables(driver: SqlDriver, schema: str) -> list[str]:
    """List the ordinary base tables in a schema, alphabetically."""
    rows = await driver.execute_query(_LIST_BASE_TABLES_SQL, params=[schema], force_readonly=True)
    return [str(row.cells["name"]) for row in rows or []]


async def _age_available(driver: SqlDriver) -> bool:
    """Advisory probe: is Apache AGE installed (``ag_catalog.ag_graph``)?"""
    try:
        await driver.execute_query(_AGE_PROBE_SQL, force_readonly=True)
        return True
    except Exception:
        return False


def _node_template(label: str, columns: list[ColumnInfo]) -> str:
    """Template CREATE for a label — properties rendered as ``$prop`` placeholders."""
    props = ", ".join(f"{c.name}: ${c.name}" for c in columns)
    return f"CREATE (:{label} {{{props}}})"


def _edge_template(edge: EdgeType) -> str:
    """Template MATCH…MERGE for an edge — key bindings as placeholders."""
    from_match = ", ".join(f"{fk}: $from_{fk}" for fk in edge.from_key)
    to_match = ", ".join(f"{tk}: $to_{tk}" for tk in edge.to_key)
    return (
        f"MATCH (a:{edge.from_label} {{{from_match}}}), "
        f"(b:{edge.to_label} {{{to_match}}}) "
        f"MERGE (a)-[:{edge.edge_type}]->(b)"
    )


def _node_create(label: str, columns: list[ColumnInfo], row: dict[str, object]) -> str:
    """Concrete CREATE for one row — NULL properties omitted, values escaped."""
    props: list[str] = []
    for col in columns:
        value = row.get(col.name)
        if value is None:
            continue
        props.append(f"{col.name}: {_render_value(value, col)}")
    return f"CREATE (:{label} {{{', '.join(props)}}})"


def _edge_merge(
    edge: EdgeType,
    child_columns: dict[str, ColumnInfo],
    parent_columns: dict[str, ColumnInfo],
    child_row: dict[str, object],
) -> str | None:
    """Concrete MATCH…MERGE binding the child row's FK values.

    Returns ``None`` when any FK column of the child row is NULL — an
    unenforced reference that cannot be matched.
    """
    from_parts: list[str] = []
    to_parts: list[str] = []
    for from_col, to_col in zip(edge.from_key, edge.to_key, strict=True):
        value = child_row.get(from_col)
        if value is None:
            return None
        from_parts.append(f"{from_col}: {_render_value(value, child_columns[from_col])}")
        to_parts.append(f"{to_col}: {_render_value(value, parent_columns[to_col])}")
    return (
        f"MATCH (a:{edge.from_label} {{{', '.join(from_parts)}}}), "
        f"(b:{edge.to_label} {{{', '.join(to_parts)}}}) "
        f"MERGE (a)-[:{edge.edge_type}]->(b)"
    )


async def _sample_rows(driver: SqlDriver, schema: str, table: str, limit: int) -> list[dict[str, object]]:
    """Read up to ``limit`` rows from a table (read-only, quoted identifiers)."""
    # ``limit`` is a validated int and ``schema`` / ``table`` are
    # identifier-validated + double-quoted by the caller.
    sql = f'SELECT * FROM "{schema}"."{table}" LIMIT {int(limit)}'
    rows = await driver.execute_query(sql, force_readonly=True)
    # Python-side cap as belt-and-braces in case the server ignores LIMIT.
    return [dict(row.cells) for row in (rows or [])[:limit]]


def _edge_type_name(fk_name: str, from_table: str, to_table: str) -> str:
    """Derive a valid edge-type name from the FK name, else ``<from>_<to>``."""
    if _IDENTIFIER.match(fk_name):
        return fk_name
    return f"{from_table}_{to_table}"


async def generate_graph_projection(
    driver: SqlDriver,
    schema: str,
    *,
    tables: list[str] | None = None,
    graph_name: str = "g",
    row_limit: int = 0,
) -> GraphProjection:
    """Generate openCypher CREATE/MERGE to project a relational schema into AGE.

    READ-ONLY. Returns a :class:`GraphProjection` — the ``cypher_statements``
    are for review and are NEVER executed here.

    Args:
        schema: The relational schema to project.
        tables: Optional subset of base tables; ``None`` projects every base
            table in ``schema``.
        graph_name: The AGE graph name used in the ``cypher('<graph>', …)``
            wrappers. Identifier-validated.
        row_limit: ``0`` → schema-level template plan (no row reads). ``>0``
            → also emit concrete per-row statements, up to ``row_limit``
            rows per table (capped at :data:`HARD_ROW_CAP`).

    Raises:
        GraphProjectionError: On an invalid schema / table / graph name, a
            negative ``row_limit``, or a requested table that is not a base
            table in ``schema``.
    """
    _check_identifier(schema, "schema")
    _check_identifier(graph_name, "graph_name")
    if row_limit < 0:
        raise GraphProjectionError("row_limit must be >= 0")
    if row_limit > HARD_ROW_CAP:
        raise GraphProjectionError(f"row_limit exceeds hard cap of {HARD_ROW_CAP}")

    base_tables = await _list_base_tables(driver, schema)
    base_set = set(base_tables)

    if tables is None:
        selected = base_tables
    else:
        selected = []
        for table in tables:
            _check_identifier(table, "table")
            if table not in base_set:
                raise GraphProjectionError(f"table {schema}.{table!r} is not a base table in schema {schema!r}")
            selected.append(table)

    selected_set = set(selected)
    available = await _age_available(driver)

    warnings: list[str] = []
    node_labels: list[NodeLabel] = []
    columns_by_table: dict[str, list[ColumnInfo]] = {}
    keyless: set[str] = set()

    for table in selected:
        columns = await describe_table(driver, schema, table)
        columns_by_table[table] = columns
        pk = await _primary_key_columns(driver, schema, table)
        if not pk:
            keyless.add(table)
        node_labels.append(
            NodeLabel(
                label=table,
                source_table=table,
                key_columns=pk,
                property_columns=[c.name for c in columns],
            )
        )

    for table in sorted(keyless):
        warnings.append(
            f"table {table!r} has no primary key; its vertices are created but cannot be "
            f"reliably MATCHed, so edges touching it are skipped"
        )

    # Edge types: FKs where both endpoints are in scope and reference this schema.
    edge_types: list[EdgeType] = []
    fks = await list_foreign_keys(driver, schema)
    for fk in fks:
        if fk.from_table not in selected_set or fk.to_table not in selected_set:
            continue
        if fk.to_schema != schema:
            continue
        if fk.from_table in keyless or fk.to_table in keyless:
            continue
        edge_types.append(
            EdgeType(
                edge_type=_edge_type_name(fk.name, fk.from_table, fk.to_table),
                from_label=fk.from_table,
                to_label=fk.to_table,
                from_key=list(fk.from_columns),
                to_key=list(fk.to_columns),
                fk_name=fk.name,
            )
        )

    # Generate the Cypher statements — nodes first, then edges.
    cypher_statements: list[str] = []

    if row_limit == 0:
        for node in node_labels:
            cypher_statements.append(_wrap(graph_name, _node_template(node.label, columns_by_table[node.label])))
        for edge in edge_types:
            cypher_statements.append(_wrap(graph_name, _edge_template(edge)))
        detail = (
            f"schema-level projection plan for {schema!r} → graph {graph_name!r}: "
            f"{len(node_labels)} node label(s), {len(edge_types)} edge type(s). "
            f"Template statements only; no rows were read."
        )
    else:
        for node in node_labels:
            rows = await _sample_rows(driver, schema, node.label, row_limit)
            for row in rows:
                cypher_statements.append(_wrap(graph_name, _node_create(node.label, columns_by_table[node.label], row)))
        for edge in edge_types:
            child_cols = {c.name: c for c in columns_by_table[edge.from_label]}
            parent_cols = {c.name: c for c in columns_by_table[edge.to_label]}
            rows = await _sample_rows(driver, schema, edge.from_label, row_limit)
            for row in rows:
                statement = _edge_merge(edge, child_cols, parent_cols, row)
                if statement is not None:
                    cypher_statements.append(_wrap(graph_name, statement))
        detail = (
            f"row-level projection of {schema!r} → graph {graph_name!r}: "
            f"{len(node_labels)} node label(s), {len(edge_types)} edge type(s), "
            f"up to {row_limit} row(s) per table. Values are escaped and NULL "
            f"properties omitted; statements are for review, not executed."
        )

    warnings.append(
        "AGE materialises this projection — running the statements LOADS a copy of the data "
        "into the graph (it is not a virtual view over the tables)."
    )
    warnings.append("Run the statements in order: all node CREATE statements before the edge MERGE statements.")
    if not available:
        warnings.append(
            "Apache AGE does not appear to be installed (ag_catalog.ag_graph is unavailable); "
            "install AGE and create the graph before running these statements."
        )

    return GraphProjection(
        available=available,
        schema=schema,
        graph_name=graph_name,
        row_limit=row_limit,
        node_labels=node_labels,
        edge_types=edge_types,
        cypher_statements=cypher_statements,
        warnings=warnings,
        detail=detail,
    )


__all__ = [
    "EdgeType",
    "GraphProjection",
    "GraphProjectionError",
    "NodeLabel",
    "generate_graph_projection",
]
