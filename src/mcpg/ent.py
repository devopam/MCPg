"""Schema → Ent (Go) schema exporter.

`Ent <https://entgo.io/>`_ generates type-safe Go code from a graph of
``Schema`` structs that declare each entity's fields and edges. The
canonical input is one Go file per entity under ``ent/schema/``.

This exporter emits exactly that — one Go source per base table in the
schema, plus a brief generation README — from a live PG catalog. The
agent runs ``go generate ./ent`` afterwards to produce the Go client.

Coverage matches the other Batch G exporters: base tables, columns,
primary keys, foreign keys (intra-schema, single-column), and enums.
Cross-schema FKs and composite FKs are documented v1 gaps.
"""

from __future__ import annotations

import re

from mcpg.introspection import (
    ColumnInfo,
    ForeignKeyInfo,
    describe_table,
    list_enums,
    list_foreign_keys,
    list_tables,
)
from mcpg.sql import SqlDriver

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class EntExportError(Exception):
    """Raised when an Ent export call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise EntExportError(f"invalid {kind} name: {name!r}")


def _pascal(snake: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", snake)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or snake


def _strip_type_params(data_type: str) -> str:
    return re.sub(r"\(.*\)", "", data_type).strip()


# Maps PG type → ent.field.<X>() builder name.
_FIELD_BUILDER: dict[str, str] = {
    "smallint": "Int16",
    "integer": "Int",
    "bigint": "Int64",
    "real": "Float32",
    "double precision": "Float",
    "numeric": "Float",  # Ent has no native decimal; Float is the closest scalar
    "decimal": "Float",
    "boolean": "Bool",
    "text": "Text",
    "character varying": "String",
    "varchar": "String",
    "character": "String",
    "char": "String",
    "uuid": "UUID",
    "json": "JSON",
    "jsonb": "JSON",
    "date": "Time",
    "time without time zone": "Time",
    "time with time zone": "Time",
    "timestamp without time zone": "Time",
    "timestamp with time zone": "Time",
    "bytea": "Bytes",
}


def _ent_builder_for(column: ColumnInfo, enum_names: set[str]) -> tuple[str, str | None]:
    """Return ``(builder_call, extra_import)`` for a column.

    ``builder_call`` is something like ``field.Int("id")`` or
    ``field.UUID("user_id", uuid.UUID{})``. ``extra_import`` is the
    extra Go import the builder needs (currently only ``uuid``
    triggers it), or ``None``.
    """
    raw = column.data_type
    base = _strip_type_params(raw).lower()
    qualified = base.split(".")[-1] if "." in base else base
    if base in enum_names or raw in enum_names or qualified in enum_names:
        # Ent's enum() builder needs a name + Values; the field call
        # syntax is field.Enum("state").Values("active", "inactive").
        # We caller-side fill in the Values from the catalog.
        return f'field.Enum("{column.name}")', None
    builder = _FIELD_BUILDER.get(base, "String")
    if builder == "UUID":
        return f'field.UUID("{column.name}", uuid.UUID{{}})', "github.com/google/uuid"
    if builder == "JSON":
        # Ent's JSON builder needs a type literal; map to interface{} so
        # the agent can refine. ``map[string]interface{}{}`` is the
        # idiom for arbitrary jsonb.
        return f'field.JSON("{column.name}", map[string]interface{{}}{{}})', None
    if builder == "Bytes":
        return f'field.Bytes("{column.name}")', None
    return f'field.{builder}("{column.name}")', None


def _render_field_call(
    column: ColumnInfo,
    enum_names: set[str],
    enum_values_by_name: dict[str, list[str]],
) -> tuple[str, str | None]:
    """Build one ``field.X("col").Modifiers()`` line + any required import.

    Modifiers: ``.Optional()`` for nullable, ``.Default(...)`` for
    defaults that round-trip cleanly (numeric, boolean, string,
    ``now()`` → ``time.Now``), ``.Values(...)`` for enum fields.
    """
    builder, extra_import = _ent_builder_for(column, enum_names)
    parts = [builder]
    base = _strip_type_params(column.data_type).lower()
    qualified = base.split(".")[-1] if "." in base else base
    is_enum = base in enum_names or qualified in enum_names
    if is_enum:
        values = enum_values_by_name.get(qualified, enum_values_by_name.get(base, []))
        if values:
            literals = ", ".join(f'"{v}"' for v in values)
            parts.append(f".Values({literals})")
    if column.nullable:
        parts.append(".Optional()")
    default = _render_default_modifier(column)
    if default:
        parts.append(default)
    return "".join(parts), extra_import


_NEXTVAL_RE = re.compile(r"nextval\(['\"]([^'\"]+)['\"]")


def _render_default_modifier(column: ColumnInfo) -> str | None:
    """Translate the PG default to a Go ``.Default(...)`` modifier.

    ``nextval`` is dropped (Ent's primary-key generation owns it).
    """
    if column.default is None:
        return None
    if _NEXTVAL_RE.search(column.default):
        return None
    raw = column.default.strip()
    cast_stripped = re.sub(r"::[A-Za-z_][A-Za-z0-9_ ]*$", "", raw)
    lowered = cast_stripped.lower()
    if lowered in {"now()", "current_timestamp"}:
        # field.Time(...).Default(time.Now) — needs the time import.
        return ".Default(time.Now)"
    if lowered in {"true", "false"}:
        return f".Default({lowered})"
    if re.fullmatch(r"-?\d+", cast_stripped) or re.fullmatch(r"-?\d+\.\d+", cast_stripped):
        return f".Default({cast_stripped})"
    if cast_stripped.startswith("'") and cast_stripped.endswith("'"):
        body = cast_stripped[1:-1].replace("\\", "\\\\").replace('"', '\\"')
        return f'.Default("{body}")'
    # Unknown — Ent does not have a sql-text default; omit so the agent
    # can fill in by hand rather than silently corrupting the value.
    return None


def _render_edges(
    table_name: str,
    fks_by_source: dict[str, list[ForeignKeyInfo]],
    entity_names: set[str],
) -> list[str]:
    """Build ``edge.To(...)`` lines for outgoing FKs of ``table_name``.

    Ent represents FKs as edges between schemas; the FK column is
    inferred from the ``Field`` modifier. We only emit edges for
    single-column intra-schema FKs.
    """
    edges: list[str] = []
    for fk in fks_by_source.get(table_name, []):
        if fk.to_table not in entity_names:
            continue
        if len(fk.from_columns) != 1:
            continue
        target_pascal = _pascal(fk.to_table)
        from_column = fk.from_columns[0]
        edges.append(f'edge.To("{fk.to_table}", {target_pascal}.Type).Unique().Field("{from_column}"),')
    return edges


def _render_entity_file(
    table_name: str,
    columns: list[ColumnInfo],
    edges: list[str],
    extra_imports: set[str],
    enum_names: set[str],
    enum_values_by_name: dict[str, list[str]],
) -> str:
    """Build one Go source file for a table.

    The file looks like::

        package schema

        import (
            "entgo.io/ent"
            "entgo.io/ent/schema/field"
            "entgo.io/ent/schema/edge"
        )

        type Widget struct {
            ent.Schema
        }

        func (Widget) Fields() []ent.Field {
            return []ent.Field{
                field.Int("id"),
                ...
            }
        }

        func (Widget) Edges() []ent.Edge {
            return []ent.Edge{
                edge.To("owner", Owner.Type).Unique().Field("owner_id"),
            }
        }
    """
    entity = _pascal(table_name)
    field_lines: list[str] = []
    for col in columns:
        line, extra = _render_field_call(col, enum_names, enum_values_by_name)
        if extra is not None:
            extra_imports.add(extra)
        if line.startswith("field.Time(") or ".Default(time.Now)" in line:
            extra_imports.add("time")
        field_lines.append(f"        {line},")

    # Imports — always include ent + field; add edge only when edges exist.
    imports = ['"entgo.io/ent"', '"entgo.io/ent/schema/field"']
    if edges:
        imports.append('"entgo.io/ent/schema/edge"')
    for extra in sorted(extra_imports):
        if extra == "time":
            imports.append('"time"')
        else:
            imports.append(f'"{extra}"')

    body = [
        "package schema",
        "",
        "import (",
        *(f"    {imp}" for imp in imports),
        ")",
        "",
        f"// {entity} holds the schema definition for the {entity} entity.",
        f"type {entity} struct {{",
        "    ent.Schema",
        "}",
        "",
        f"// Fields of the {entity}.",
        f"func ({entity}) Fields() []ent.Field {{",
        "    return []ent.Field{",
        *field_lines,
        "    }",
        "}",
    ]
    if edges:
        body.extend(
            [
                "",
                f"// Edges of the {entity}.",
                f"func ({entity}) Edges() []ent.Edge {{",
                "    return []ent.Edge{",
                *(f"        {e}" for e in edges),
                "    }",
                "}",
            ]
        )
    return "\n".join(body) + "\n"


async def generate_ent_schemas(driver: SqlDriver, schema: str) -> dict[str, str]:
    """Emit one Go source file per base table in ``schema``.

    Returns a dict ``{filename: source}`` so callers can write each
    file or pass it through to the agent for inspection. Filenames are
    snake_case (matching Ent's conventional layout).

    Raises:
        EntExportError: When the schema name, or any table / column
            name within it, requires PostgreSQL delimited-identifier
            quoting.
    """
    _check_identifier(schema, "schema")
    tables = [t for t in await list_tables(driver, schema) if t.type == "BASE TABLE" and not t.is_partition]
    for t in tables:
        _check_identifier(t.name, "table")

    enums = await list_enums(driver, schema)
    enum_names = {e.name for e in enums}
    enum_values_by_name = {e.name: list(e.values) for e in enums}

    fks_all = await list_foreign_keys(driver, schema)
    fks_by_source: dict[str, list[ForeignKeyInfo]] = {}
    for fk in fks_all:
        fks_by_source.setdefault(fk.from_table, []).append(fk)
    entity_names = {t.name for t in tables}

    output: dict[str, str] = {}
    for table in tables:
        columns = await describe_table(driver, schema, table.name)
        for col in columns:
            _check_identifier(col.name, "column")
        extra_imports: set[str] = set()
        edges = _render_edges(table.name, fks_by_source, entity_names)
        source = _render_entity_file(
            table.name,
            columns,
            edges,
            extra_imports,
            enum_names,
            enum_values_by_name,
        )
        output[f"{table.name}.go"] = source

    return output
