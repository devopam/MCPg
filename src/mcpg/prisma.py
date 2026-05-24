"""PostgreSQL → Prisma schema exporter.

``generate_prisma_schema`` reads a PG schema via the existing
introspection primitives and emits a valid ``.prisma`` schema string
that an agent can drop into a Prisma project — mirroring what
``prisma db pull`` would write, but driven by MCPg instead of the
Prisma CLI.

Coverage (first cut):
* Tables → ``model`` blocks with columns, primary keys, foreign keys,
  composite unique constraints, and single-column ``@unique`` /
  multi-column ``@@index`` from the actual catalog.
* Enums → top-level Prisma ``enum`` blocks; enum-typed columns reference
  the Prisma enum directly.
* Standard column defaults — ``nextval('seq')`` → ``autoincrement()``,
  ``now()`` / ``CURRENT_TIMESTAMP`` → ``now()``,
  ``gen_random_uuid()`` → ``uuid()``, literals → ``@default(...)``.
* Types Prisma doesn't model (composite types, vectors, custom domains,
  …) fall back to ``Unsupported("…")`` exactly like ``prisma db pull``.

Out of scope for v1: views, foreign tables, partitions, triggers,
functions, RLS policies, composite types. Identifiers must match the
plain PG identifier pattern (no quoted/case-sensitive names) — the
output is not re-mapped via ``@@map`` / ``@map`` in this first cut.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import (
    ColumnInfo,
    EnumInfo,
    ForeignKeyInfo,
    IndexInfo,
    describe_table,
    list_constraints,
    list_enums,
    list_foreign_keys,
    list_indexes,
    list_tables,
)

# Generic PG types → Prisma scalar types. Types with parameters (e.g.
# ``character varying(255)``, ``numeric(10,2)``, ``vector(384)``) are
# stripped to the base name before lookup.
_PRISMA_SCALAR_TYPES = {
    "integer": "Int",
    "int4": "Int",
    "smallint": "Int",
    "int2": "Int",
    "bigint": "BigInt",
    "int8": "BigInt",
    "text": "String",
    "character varying": "String",
    "varchar": "String",
    "character": "String",
    "char": "String",
    "bpchar": "String",
    "boolean": "Boolean",
    "bool": "Boolean",
    "real": "Float",
    "float4": "Float",
    "double precision": "Float",
    "float8": "Float",
    "numeric": "Decimal",
    "decimal": "Decimal",
    "date": "DateTime",
    "timestamp": "DateTime",
    "timestamp without time zone": "DateTime",
    "timestamp with time zone": "DateTime",
    "timestamptz": "DateTime",
    "time": "DateTime",
    "time without time zone": "DateTime",
    "time with time zone": "DateTime",
    "json": "Json",
    "jsonb": "Json",
    "uuid": "String",
    "bytea": "Bytes",
}

# Same prefix as mcpg.textsearch / mcpg.vector_tuning — refuse names
# that need PG's delimited-identifier quoting; pass plain ones through.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

_PRIMARY_KEY_COLUMNS = re.compile(r"PRIMARY KEY \(([^)]+)\)", re.IGNORECASE)
_UNIQUE_COLUMNS = re.compile(r"UNIQUE \(([^)]+)\)", re.IGNORECASE)
_LITERAL_DEFAULT = re.compile(r"^'((?:[^']|'')*)'::")


class PrismaError(Exception):
    """Raised when a Prisma schema cannot be emitted."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise PrismaError(f"invalid {kind} name {name!r}; Prisma export requires plain SQL identifiers")


def _parse_pk_columns(definition: str) -> list[str]:
    match = _PRIMARY_KEY_COLUMNS.search(definition)
    if not match:
        return []
    return [column.strip().strip('"') for column in match.group(1).split(",")]


def _parse_unique_columns(definition: str) -> list[str]:
    match = _UNIQUE_COLUMNS.search(definition)
    if not match:
        return []
    return [column.strip().strip('"') for column in match.group(1).split(",")]


def _strip_type_parameters(data_type: str) -> str:
    """Return the base PG type name without ``(...)`` modifiers, array suffix, or schema prefix."""
    base = data_type.split("(", 1)[0].strip()
    if base.endswith("[]"):
        base = base[:-2].strip()
    # User-defined types (enums, domains, composites) come back schema-
    # qualified from format_type when they are not on the search_path —
    # strip the schema prefix so the lookup table and enum-name set both
    # match the bare type name.
    if "." in base:
        base = base.rsplit(".", 1)[1]
    return base.lower()


def _is_array_type(data_type: str) -> bool:
    return data_type.rstrip().endswith("[]")


def _prisma_type(column: ColumnInfo, enum_names: set[str]) -> str:
    """Map a ``ColumnInfo`` to the Prisma type token (without ``?`` modifier)."""
    base = _strip_type_parameters(column.data_type)
    array = _is_array_type(column.data_type)
    if base in enum_names:
        token = base
    elif base in _PRISMA_SCALAR_TYPES:
        token = _PRISMA_SCALAR_TYPES[base]
    else:
        # vector(384), custom domains, unmapped types — preserve the
        # full PG type so prisma generate emits the same Unsupported.
        # Escape embedded double quotes so a pathological type name
        # can't produce a syntactically broken Prisma string literal.
        escaped = column.data_type.replace("\\", "\\\\").replace('"', '\\"')
        return f'Unsupported("{escaped}")'

    if array:
        token = f"{token}[]"
    return token


def _prisma_default(default: str | None) -> str | None:
    if default is None:
        return None
    lowered = default.strip().lower()
    if lowered.startswith("nextval("):
        return "autoincrement()"
    if lowered in {"now()", "current_timestamp"}:
        return "now()"
    if "gen_random_uuid()" in lowered or "uuid_generate_v4()" in lowered:
        return "uuid()"
    if lowered in {"true", "false"}:
        return lowered
    # PG renders bare numeric defaults without a cast — accept ints and
    # decimals plus an optional sign.
    if re.match(r"^-?\d+(\.\d+)?$", default.strip()):
        return default.strip()
    # Quoted text literal: ``'value'::type`` — extract the inner string,
    # un-double single quotes, re-quote with double quotes for Prisma.
    literal = _LITERAL_DEFAULT.match(default)
    if literal:
        return '"' + literal.group(1).replace("''", "'").replace('"', '\\"') + '"'
    return None


def _render_field(
    column: ColumnInfo,
    *,
    pk_columns: set[str],
    unique_columns: set[str],
    enum_names: set[str],
) -> str:
    _check_identifier(column.name, "column")
    type_token = _prisma_type(column, enum_names)
    # Prisma treats SQL arrays as inherently optional: an empty array is
    # equivalent to "no value", so the schema can't carry ``?`` on a list
    # type even when the underlying column is nullable. Don't append it.
    nullable = "?" if column.nullable and not type_token.endswith("[]") else ""
    attrs: list[str] = []
    if column.name in pk_columns and len(pk_columns) == 1:
        attrs.append("@id")
    if column.name in unique_columns:
        attrs.append("@unique")
    default = _prisma_default(column.default)
    if default is not None:
        attrs.append(f"@default({default})")
    suffix = (" " + " ".join(attrs)) if attrs else ""
    return f"  {column.name} {type_token}{nullable}{suffix}"


def _render_relation_field(name: str, target_model: str, fk: ForeignKeyInfo) -> str:
    # ``name`` and ``fk.name`` are both interpolated into Prisma text —
    # validate them against the identifier allowlist so a constraint
    # name with spaces or quotes can't produce broken Prisma.
    _check_identifier(name, "relation field")
    _check_identifier(fk.name, "relation name")
    fields = ", ".join(fk.from_columns)
    references = ", ".join(fk.to_columns)
    return f'  {name} {target_model} @relation("{fk.name}", fields: [{fields}], references: [{references}])'


def _render_back_relation(name: str, source_model: str, fk_name: str) -> str:
    _check_identifier(name, "back-relation field")
    _check_identifier(fk_name, "relation name")
    return f'  {name} {source_model}[] @relation("{fk_name}")'


def _render_composite_pk(pk_columns: list[str]) -> str:
    cols = ", ".join(pk_columns)
    return f"  @@id([{cols}])"


def _render_unique_constraint(cols: list[str]) -> str:
    if len(cols) == 1:
        # single-column UNIQUE is emitted inline on the field as @unique
        return ""
    return f"  @@unique([{', '.join(cols)}])"


def _render_index(index: IndexInfo) -> str:
    # Best-effort parse: pg_get_indexdef -> CREATE [UNIQUE] INDEX ... USING method (col1, col2)
    columns_match = re.search(r"\(([^)]+)\)", index.definition)
    if not columns_match:
        return ""
    raw_columns = [
        column.strip().strip('"').split(" ")[0]  # drop any ASC/DESC suffix
        for column in columns_match.group(1).split(",")
    ]
    # Skip indexes whose expressions Prisma can't model.
    if any(not _IDENTIFIER.match(column) for column in raw_columns):
        return ""
    is_unique = "UNIQUE" in index.definition.upper()
    keyword = "@@unique" if is_unique else "@@index"
    return f"  {keyword}([{', '.join(raw_columns)}])"


def _render_enum(enum: EnumInfo) -> str:
    _check_identifier(enum.name, "enum")
    for value in enum.values:
        _check_identifier(value, "enum value")
    lines = [f"enum {enum.name} {{"]
    lines.extend(f"  {value}" for value in enum.values)
    lines.append("}")
    return "\n".join(lines)


def _datasource_header() -> str:
    return (
        "datasource db {\n"
        '  provider = "postgresql"\n'
        '  url      = env("DATABASE_URL")\n'
        "}\n\n"
        "generator client {\n"
        '  provider = "prisma-client-js"\n'
        "}"
    )


async def _foreign_keys_indexed(driver: SqlDriver, schema: str) -> dict[str, list[ForeignKeyInfo]]:
    result: dict[str, list[ForeignKeyInfo]] = {}
    for fk in await list_foreign_keys(driver, schema):
        result.setdefault(fk.from_table, []).append(fk)
    return result


def _back_relations_by_target(
    fks_by_source: dict[str, list[ForeignKeyInfo]], entity_names: set[str]
) -> dict[str, list[tuple[str, ForeignKeyInfo]]]:
    """Index back-relations as ``target_table → [(source_table, fk), ...]``."""
    result: dict[str, list[tuple[str, ForeignKeyInfo]]] = {}
    for source, fks in fks_by_source.items():
        for fk in fks:
            if fk.to_table not in entity_names:
                continue
            result.setdefault(fk.to_table, []).append((source, fk))
    return result


def _disambiguate_relation_names(pairs: Iterable[tuple[str, ForeignKeyInfo]]) -> dict[str, str]:
    """Build ``fk.name → field_name`` so duplicate sources get unique names.

    Single pass: the first time we see a source we record the bare name;
    subsequent appearances are suffixed with their ordinal index. Order
    is preserved from the input iterable, which makes the chosen names
    stable across runs.
    """
    names: dict[str, str] = {}
    per_source: dict[str, int] = {}
    for source, fk in pairs:
        ordinal = per_source.get(source, 0) + 1
        per_source[source] = ordinal
        names[fk.name] = source if ordinal == 1 else f"{source}_{ordinal}"
    return names


async def generate_prisma_schema(driver: SqlDriver, schema: str) -> str:
    """Emit a Prisma schema string covering the base tables of ``schema``.

    Views, foreign tables, partitions, triggers, functions, policies,
    and composite types are out of scope for v1. Identifiers must be
    plain PG identifiers (``[A-Za-z_][A-Za-z0-9_]*``); a name that
    needs PG's delimited-identifier quoting raises :class:`PrismaError`.
    """
    _check_identifier(schema, "schema")
    tables = [
        table for table in await list_tables(driver, schema) if table.type == "BASE TABLE" and not table.is_partition
    ]
    entity_names = {table.name for table in tables}
    enums = await list_enums(driver, schema)
    enum_names = {enum.name for enum in enums}

    fks_by_source = await _foreign_keys_indexed(driver, schema)
    back_relations = _back_relations_by_target(fks_by_source, entity_names)

    blocks: list[str] = [_datasource_header()]

    for table in tables:
        _check_identifier(table.name, "table")
        columns = await describe_table(driver, schema, table.name)
        constraints = await list_constraints(driver, schema, table.name)
        indexes = await list_indexes(driver, schema, table.name)

        pk_columns: list[str] = []
        single_column_uniques: set[str] = set()
        composite_uniques: list[list[str]] = []
        for constraint in constraints:
            if constraint.type == "primary_key":
                pk_columns = _parse_pk_columns(constraint.definition)
            elif constraint.type == "unique":
                cols = _parse_unique_columns(constraint.definition)
                if len(cols) == 1:
                    single_column_uniques.update(cols)
                elif cols:
                    composite_uniques.append(cols)

        outgoing_fks = fks_by_source.get(table.name, [])

        lines = [f"model {table.name} {{"]
        for column in columns:
            lines.append(
                _render_field(
                    column,
                    pk_columns=set(pk_columns),
                    unique_columns=single_column_uniques,
                    enum_names=enum_names,
                )
            )

        column_names = {column.name for column in columns}

        for fk in outgoing_fks:
            if fk.to_table not in entity_names:
                # Cross-schema FK — Prisma can't model it cleanly; the
                # scalar columns are already emitted.
                continue
            relation_field = fk.name
            if relation_field in column_names:
                # A FK constraint named after an existing column would
                # produce a duplicate-field Prisma model; surface it as
                # a hard error rather than emit broken DSL.
                raise PrismaError(
                    f"foreign-key constraint {fk.name!r} on table {table.name!r} collides with column "
                    f"of the same name; rename the constraint and re-run."
                )
            lines.append(_render_relation_field(relation_field, fk.to_table, fk))

        if table.name in back_relations:
            relation_names = _disambiguate_relation_names(back_relations[table.name])
            for source, fk in back_relations[table.name]:
                if fk.from_table not in entity_names:
                    continue
                lines.append(_render_back_relation(relation_names[fk.name], source, fk.name))

        if len(pk_columns) > 1:
            lines.append(_render_composite_pk(pk_columns))
        for unique_cols in composite_uniques:
            lines.append(_render_unique_constraint(unique_cols))
        emitted: set[tuple[str, ...]] = set()
        # @@id and @@unique already declare their column sets; skip an
        # @@index that duplicates them.
        if pk_columns:
            emitted.add(tuple(pk_columns))
        for unique_cols in composite_uniques:
            emitted.add(tuple(unique_cols))
        for index in indexes:
            rendered = _render_index(index)
            if not rendered:
                continue
            cols_match = re.search(r"\[([^\]]+)\]", rendered)
            if cols_match:
                cols_key = tuple(part.strip() for part in cols_match.group(1).split(","))
                if cols_key in emitted:
                    continue
                emitted.add(cols_key)
            lines.append(rendered)
        lines.append("}")
        blocks.append("\n".join(lines))

    for enum in enums:
        blocks.append(_render_enum(enum))

    return "\n\n".join(blocks) + "\n"
