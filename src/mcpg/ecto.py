"""Schema → Ecto (Elixir) schema exporter.

`Ecto <https://hexdocs.pm/ecto/>`_ is the canonical Elixir database
wrapper / DSL. Modules are defined with ``use Ecto.Schema`` and a
``schema "table_name" do ... end`` block listing each ``field`` and
``belongs_to`` / ``has_many`` association.

This exporter emits one Elixir module per base table in the PG schema,
matching Phoenix conventions (PascalCase module names, snake_case
table/field names, ``belongs_to`` for single-column intra-schema FKs).

Coverage is the same as the other Batch G exporters — base tables,
columns, primary keys, foreign keys (intra-schema, single-column),
enums (mapped to ``:string`` since Ecto's enum support requires the
``EctoEnum`` library — we leave that decision to the user). Cross-
schema FKs and composite FKs are documented v1 gaps.
"""

from __future__ import annotations

import re

from mcpg.introspection import (
    ColumnInfo,
    ForeignKeyInfo,
    describe_table,
    list_constraints,
    list_enums,
    list_foreign_keys,
    list_tables,
)
from mcpg.sql import SqlDriver

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class EctoExportError(Exception):
    """Raised when an Ecto export call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise EctoExportError(f"invalid {kind} name: {name!r}")


def _strip_type_params(data_type: str) -> str:
    return re.sub(r"\(.*\)", "", data_type).strip()


def _pascal(snake: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", snake)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or snake


def _singularize(plural: str) -> str:
    """Best-effort English singularisation for table → module.

    Ecto modules are conventionally singular (``User`` for table
    ``users``). The mapping is intentionally minimal — anything we
    can't confidently singularise stays as-is. Latin-derived singular
    endings (``-us``, ``-is``) are preserved so we don't break
    ``status`` → ``statu``.
    """
    if plural.endswith("ies") and len(plural) > 3:
        return plural[:-3] + "y"
    if plural.endswith("ses") and len(plural) > 3:
        return plural[:-2]
    if plural.endswith("s") and not plural.endswith(("ss", "us", "is", "os")) and len(plural) > 1:
        return plural[:-1]
    return plural


# Maps PG type → Ecto field type atom.
_FIELD_TYPE: dict[str, str] = {
    "smallint": ":integer",
    "integer": ":integer",
    "bigint": ":integer",
    "real": ":float",
    "double precision": ":float",
    "numeric": ":decimal",
    "decimal": ":decimal",
    "boolean": ":boolean",
    "text": ":string",
    "character varying": ":string",
    "varchar": ":string",
    "character": ":string",
    "char": ":string",
    "uuid": "Ecto.UUID",
    "json": ":map",
    "jsonb": ":map",
    "date": ":date",
    "time without time zone": ":time",
    "time with time zone": ":time",
    "timestamp without time zone": ":naive_datetime",
    "timestamp with time zone": ":utc_datetime",
    "bytea": ":binary",
}


def _ecto_field_type(column: ColumnInfo, enum_names: set[str]) -> str:
    """Return the Ecto field-type expression for a column."""
    raw = column.data_type
    base = _strip_type_params(raw).lower()
    qualified = base.split(".")[-1] if "." in base else base
    if base in enum_names or raw in enum_names or qualified in enum_names:
        # Without the EctoEnum library Ecto can't express PG enums
        # natively. Fall back to :string so the schema compiles; the
        # agent can swap in EctoEnum if needed.
        return ":string"
    return _FIELD_TYPE.get(base, ":string")


_PK_COLS_RE = re.compile(r"PRIMARY KEY\s*\(([^)]+)\)", re.IGNORECASE)


def _parse_pk_columns(definition: str) -> list[str]:
    match = _PK_COLS_RE.search(definition)
    if not match:
        return []
    return [col.strip().strip('"') for col in match.group(1).split(",")]


def _is_timestamp_pair(columns: list[ColumnInfo]) -> bool:
    """True when the columns include both ``inserted_at`` and ``updated_at``.

    Ecto's ``timestamps()`` macro handles those two columns for you;
    we skip emitting field declarations for them when both are present.
    """
    names = {c.name for c in columns}
    return "inserted_at" in names and "updated_at" in names


def _render_field_line(column: ColumnInfo, enum_names: set[str]) -> str:
    ecto_type = _ecto_field_type(column, enum_names)
    return f"    field :{column.name}, {ecto_type}"


def _render_belongs_to(fk_column: str, target_table: str, target_module: str) -> str:
    """Emit one ``belongs_to`` line for an FK column.

    Ecto strips the trailing ``_id`` to derive the association name
    (so ``owner_id`` → ``:owner``). We respect that idiom but fall
    back to the column name if the suffix is missing.
    """
    assoc = fk_column[:-3] if fk_column.endswith("_id") else fk_column
    return f"    belongs_to :{assoc}, {target_module}, foreign_key: :{fk_column}"


def _render_module(
    app_module: str,
    table_name: str,
    columns: list[ColumnInfo],
    pk_columns: list[str],
    belongs_to_lines: list[str],
    enum_names: set[str],
) -> str:
    """Build one ``defmodule`` block for a table.

    The module looks like::

        defmodule MyApp.Widget do
          use Ecto.Schema

          @primary_key {:id, :id, autogenerate: true}
          schema "widget" do
            field :name, :string
            belongs_to :owner, MyApp.Owner, foreign_key: :owner_id
            timestamps()
          end
        end
    """
    entity = _pascal(_singularize(table_name))
    has_timestamps = _is_timestamp_pair(columns)
    skip_columns = {"inserted_at", "updated_at"} if has_timestamps else set()

    # Don't double-declare PK columns; @primary_key handles ``id``.
    if len(pk_columns) == 1 and pk_columns[0] == "id":
        skip_columns.add("id")
        primary_key_line = "  @primary_key {:id, :id, autogenerate: true}"
    elif len(pk_columns) == 1:
        # Non-conventional PK — declare it so Ecto knows the type.
        primary_key_line = f"  @primary_key {{:{pk_columns[0]}, :id, autogenerate: true}}"
    elif len(pk_columns) > 1:
        # Composite PK — Ecto requires opting out of the default :id PK
        # and the field declarations carry primary_key: true.
        primary_key_line = "  @primary_key false"
    else:
        primary_key_line = "  @primary_key false"

    # Skip FK columns whose belongs_to has been emitted — Ecto's
    # belongs_to creates the schema field itself, so re-declaring the
    # underlying column would produce a duplicate-field compile error.
    fk_re = re.compile(r"\s*belongs_to\s+:\w+,.*foreign_key:\s*:(\w+)")
    for line in belongs_to_lines:
        match = fk_re.match(line)
        if match:
            skip_columns.add(match.group(1))

    field_lines = [_render_field_line(c, enum_names) for c in columns if c.name not in skip_columns]
    if pk_columns and len(pk_columns) > 1:
        # Composite PK — re-declare each component WITH primary_key: true.
        composite_pk_lines = [f"    field :{name}, :integer, primary_key: true" for name in pk_columns]
        field_lines = composite_pk_lines + field_lines

    body = [
        f"defmodule {app_module}.{entity} do",
        "  use Ecto.Schema",
        "",
        primary_key_line,
        f'  schema "{table_name}" do',
        *field_lines,
        *belongs_to_lines,
    ]
    if has_timestamps:
        body.append("    timestamps()")
    body.extend(
        [
            "  end",
            "end",
        ]
    )
    return "\n".join(body) + "\n"


async def generate_ecto_schemas(
    driver: SqlDriver,
    schema: str,
    *,
    app_module: str = "MyApp",
) -> dict[str, str]:
    """Emit one Elixir module per base table in ``schema``.

    Returns ``{filename: source}`` where ``filename`` is the
    snake_case singular form with ``.ex`` extension — matching
    Phoenix's ``lib/my_app/<singular>.ex`` convention.

    Args:
        driver: SQL driver bound to the live database.
        schema: PG schema to generate from. Must be a plain identifier.
        app_module: The Elixir top-level module name (e.g. ``MyApp``,
            ``Shop``). Modules emitted as ``<app_module>.<Entity>``.

    Raises:
        EctoExportError: When the schema name, or any table / column
            name, requires PostgreSQL delimited-identifier quoting.
    """
    _check_identifier(schema, "schema")
    tables = [t for t in await list_tables(driver, schema) if t.type == "BASE TABLE" and not t.is_partition]
    for t in tables:
        _check_identifier(t.name, "table")

    enums = await list_enums(driver, schema)
    enum_names = {e.name for e in enums}

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
        constraints = await list_constraints(driver, schema, table.name)
        pk_columns: list[str] = []
        for con in constraints:
            if con.type == "primary_key":
                pk_columns = _parse_pk_columns(con.definition)
                break

        belongs_to_lines: list[str] = []
        for fk in fks_by_source.get(table.name, []):
            if fk.to_table not in entity_names:
                continue
            if len(fk.from_columns) != 1:
                continue
            target_module = f"{app_module}.{_pascal(_singularize(fk.to_table))}"
            belongs_to_lines.append(_render_belongs_to(fk.from_columns[0], fk.to_table, target_module))

        source = _render_module(app_module, table.name, columns, pk_columns, belongs_to_lines, enum_names)
        # File name: lib/my_app/<singular>.ex idiom.
        filename = f"{_singularize(table.name)}.ex"
        output[filename] = source

    return output
