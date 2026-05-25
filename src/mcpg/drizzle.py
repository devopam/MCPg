"""Schema → Drizzle ORM (TypeScript) exporter.

Reads the PostgreSQL catalog via :mod:`mcpg.introspection` and emits a
valid ``drizzle-orm/pg-core`` schema file an agent can drop into a
TS/JS project. Mirrors the structure of :mod:`mcpg.prisma` so the two
exporters can be reasoned about together — same coverage rules apply
(base tables only, plain identifiers, intra-schema FKs).

Scope is deliberately narrow per Batch G: catalog → DSL only. We do
not parse Drizzle schemas back to DDL and we don't shell out to
``drizzle-kit``. The agent uses the emitted file as the source of
truth for its TypeScript project.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import (
    ColumnInfo,
    ForeignKeyInfo,
    IndexInfo,
    describe_table,
    list_constraints,
    list_enums,
    list_foreign_keys,
    list_indexes,
    list_tables,
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class DrizzleError(Exception):
    """Raised when a Drizzle export call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise DrizzleError(f"invalid {kind} name: {name!r}")


_SNAKE_BOUNDARY = re.compile(r"_+([a-z0-9])")


def _camel_case(snake_name: str) -> str:
    """Convert ``snake_case`` to ``camelCase`` for TS property names.

    Drizzle's property names are JS-side identifiers; the column name
    in PG stays in the call argument. So ``owner_id`` becomes
    ``ownerId`` for the property, but the call is
    ``ownerId: integer("owner_id")``.
    """
    if not snake_name:
        return snake_name
    head, *rest = snake_name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest if part)


def _strip_type_params(data_type: str) -> str:
    """Drop ``(...)`` from types like ``varchar(255)`` for mapping table lookups."""
    return re.sub(r"\(.*\)", "", data_type).strip()


_BASE_TYPE_MAP = {
    "smallint": "smallint",
    "integer": "integer",
    "bigint": "bigint",
    "real": "real",
    "double precision": "doublePrecision",
    "numeric": "numeric",
    "decimal": "numeric",
    "boolean": "boolean",
    "text": "text",
    "character varying": "varchar",
    "varchar": "varchar",
    "character": "char",
    "char": "char",
    "uuid": "uuid",
    "json": "json",
    "jsonb": "jsonb",
    "date": "date",
    "time without time zone": "time",
    "time with time zone": "time",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp",
    "bytea": "bytea",
    "inet": "inet",
    "cidr": "cidr",
    "macaddr": "macaddr",
    "interval": "interval",
}


def _drizzle_helper_for(column: ColumnInfo, enum_names: set[str]) -> tuple[str, list[str]]:
    """Return the Drizzle helper name + options literal for ``column``.

    ``enum_names`` lets us route an enum column to its generated
    ``pgEnum(...)`` reference instead of a generic ``text`` field.
    """
    raw = column.data_type
    base = _strip_type_params(raw).lower()
    # Enum columns can arrive qualified (``schema.enum_name``) or
    # unqualified depending on the catalog query. Strip a leading
    # schema prefix for the lookup.
    qualified_base = base.split(".")[-1] if "." in base else base
    if base in enum_names or raw in enum_names or qualified_base in enum_names:
        # Drizzle enums are referenced by the generated const, not by the helper.
        return f"{_camel_case(qualified_base)}Enum", []
    helper = _BASE_TYPE_MAP.get(base, "text")
    options: list[str] = []
    # Capture varchar(N) / char(N) length so the TS type is accurate.
    if helper in {"varchar", "char"}:
        match = re.search(r"\((\d+)\)", column.data_type)
        if match:
            options.append(f"length: {match.group(1)}")
    # Map "with time zone" timestamps so the TS type is Date-with-TZ
    # rather than an opaque string.
    if helper == "timestamp" and "with time zone" in column.data_type:
        options.append("withTimezone: true")
    return helper, options


_SERIAL_DEFAULT_RE = re.compile(r"nextval\(['\"]([^'\"]+)['\"]")


def _is_serial(column: ColumnInfo) -> bool:
    return column.default is not None and bool(_SERIAL_DEFAULT_RE.search(column.default))


_PK_COLS_RE = re.compile(r"PRIMARY KEY\s*\(([^)]+)\)", re.IGNORECASE)
_UNIQUE_COLS_RE = re.compile(r"UNIQUE\s*\(([^)]+)\)", re.IGNORECASE)


def _parse_columns(definition: str, regex: re.Pattern[str]) -> list[str]:
    match = regex.search(definition)
    if not match:
        return []
    return [col.strip().strip('"') for col in match.group(1).split(",")]


def _render_default(column: ColumnInfo) -> str | None:
    r"""Map a column's PG default to a Drizzle ``.default(...)`` clause.

    Common scalars round-trip cleanly. Anything we can't recognise is
    emitted as ``.default(sql\`...\`)`` so the user can fix it up — we
    do not silently drop a default the agent relied on.
    """
    if column.default is None:
        return None
    default = column.default.strip()
    # Strip PG's type cast suffix (``'x'::text`` → ``'x'``) for cleaner output.
    cast_stripped = re.sub(r"::[A-Za-z_][A-Za-z0-9_ ]*$", "", default)
    if cast_stripped.lower() == "now()" or cast_stripped.lower() == "current_timestamp":
        return ".defaultNow()"
    if cast_stripped.lower() in {"true", "false"}:
        return f".default({cast_stripped.lower()})"
    if re.fullmatch(r"-?\d+", cast_stripped) or re.fullmatch(r"-?\d+\.\d+", cast_stripped):
        return f".default({cast_stripped})"
    if cast_stripped.startswith("'") and cast_stripped.endswith("'"):
        # PG-quoted literal → TS string literal.
        return f'.default("{cast_stripped[1:-1].replace('"', '\\"')}")'
    # Unrecognised — fall back to raw sql template literal so the agent sees it.
    return f".default(sql`{cast_stripped}`)"


def _render_column(
    column: ColumnInfo,
    *,
    pk_columns: set[str],
    unique_columns: set[str],
    fk_lookup: dict[str, ForeignKeyInfo],
    enum_names: set[str],
) -> str:
    """Build the ``name: helper("name", { ... }).chain()`` line for a column."""
    helper, options = _drizzle_helper_for(column, enum_names)
    if _is_serial(column):
        # serial / bigserial mapped to the matching helper for the
        # base type so the TS side reflects the right precision.
        if helper == "bigint":
            helper = "bigserial"
            options = ['mode: "number"']
        else:
            helper = "serial"
            options = []
    options_literal = ""
    if options:
        options_literal = ", { " + ", ".join(options) + " }"
    chain = ""
    if not column.nullable and not _is_serial(column):
        chain += ".notNull()"
    if column.name in pk_columns and len(pk_columns) == 1:
        chain += ".primaryKey()"
    if column.name in unique_columns:
        chain += ".unique()"
    default_clause = _render_default(column)
    if default_clause and not _is_serial(column):
        chain += default_clause
    if column.name in fk_lookup:
        fk = fk_lookup[column.name]
        target_table = _camel_case(fk.to_table)
        target_column = _camel_case(fk.to_columns[0])
        chain += f".references(() => {target_table}.{target_column})"
    js_name = _camel_case(column.name)
    return f'  {js_name}: {helper}("{column.name}"{options_literal}){chain},'


def _render_enum(enum_name: str, labels: list[str]) -> str:
    label_literals = ", ".join(f'"{lbl}"' for lbl in labels)
    return f'export const {_camel_case(enum_name)}Enum = pgEnum("{enum_name}", [{label_literals}]);'


def _render_composite_pk(table_var: str, pk_columns: list[str]) -> str:
    cols = ", ".join(f"table.{_camel_case(c)}" for c in pk_columns)
    return f"  pk: primaryKey({{ columns: [{cols}] }})"


def _render_composite_unique(table_var: str, name: str, cols: list[str]) -> str:
    parts = ", ".join(f"table.{_camel_case(c)}" for c in cols)
    js_name = _camel_case(name)
    return f'  {js_name}: unique("{name}").on({parts})'


def _render_index(index: IndexInfo) -> str:
    # Drizzle's index helper takes a name + .on(columns); we use the
    # raw SQL fallback for anything we can't parse cleanly so the
    # agent always sees what's in PG.
    return f'  {_camel_case(index.name)}: index("{index.name}").using("{index.method}")'


# Match a top-level call ``helper(`` — NOT ``.helper(`` (chain methods
# like ``.primaryKey()`` don't need an import). The negative lookbehind
# rules out ``.``, identifier chars (so ``foo.bar(`` doesn't match), and
# the assignment ``=``.
_USED_HELPER_PATTERN = re.compile(r"(?<![A-Za-z0-9_.])([a-z][A-Za-z]*)\(")


def _collect_used_helpers(body: str) -> set[str]:
    """Scan emitted body text for the Drizzle helper names actually used.

    Lets the import line stay minimal — we don't pull in helpers that
    no column ended up using (and we don't import chain-method names
    like ``primaryKey`` that look like helpers but aren't).
    """
    return {match.group(1) for match in _USED_HELPER_PATTERN.finditer(body)}


_KNOWN_HELPERS = {
    "smallint",
    "integer",
    "bigint",
    "real",
    "doublePrecision",
    "numeric",
    "boolean",
    "text",
    "varchar",
    "char",
    "uuid",
    "json",
    "jsonb",
    "date",
    "time",
    "timestamp",
    "bytea",
    "inet",
    "cidr",
    "macaddr",
    "interval",
    "serial",
    "bigserial",
    "pgTable",
    "pgEnum",
    "primaryKey",
    "unique",
    "index",
}


async def generate_drizzle_schema(driver: SqlDriver, schema: str) -> str:
    """Emit a Drizzle ORM TypeScript schema for ``schema``.

    Returns a string with the import line, every ``pgEnum`` declaration,
    and one ``pgTable`` block per base table. Views, foreign tables,
    partitions, triggers, functions, and composite types are out of
    scope for v1.

    Raises:
        DrizzleError: When the schema name (or any table/column name)
            requires PostgreSQL delimited-identifier quoting.
    """
    _check_identifier(schema, "schema")

    tables = [t for t in await list_tables(driver, schema) if t.type == "BASE TABLE" and not t.is_partition]
    for t in tables:
        _check_identifier(t.name, "table")
    entity_names = {t.name for t in tables}

    enums = await list_enums(driver, schema)
    enum_names = {e.name for e in enums}

    fks_all = await list_foreign_keys(driver, schema)
    fks_by_source: dict[str, list[ForeignKeyInfo]] = {}
    for fk in fks_all:
        fks_by_source.setdefault(fk.from_table, []).append(fk)

    body_blocks: list[str] = []

    if enums:
        for enum in sorted(enums, key=lambda e: e.name):
            body_blocks.append(_render_enum(enum.name, list(enum.values)))
        body_blocks.append("")

    for table in tables:
        columns = await describe_table(driver, schema, table.name)
        constraints = await list_constraints(driver, schema, table.name)
        indexes = await list_indexes(driver, schema, table.name)

        pk_columns: list[str] = []
        single_uniques: set[str] = set()
        composite_uniques: list[tuple[str, list[str]]] = []
        for con in constraints:
            if con.type == "primary_key":
                pk_columns = _parse_columns(con.definition, _PK_COLS_RE)
            elif con.type == "unique":
                cols = _parse_columns(con.definition, _UNIQUE_COLS_RE)
                if len(cols) == 1:
                    single_uniques.update(cols)
                elif cols:
                    composite_uniques.append((con.name, cols))

        outgoing_fks = [fk for fk in fks_by_source.get(table.name, []) if fk.to_table in entity_names]
        # Only single-column FKs map to a column-level .references(); composite
        # FKs are documented as a v1 gap (Drizzle supports them but our column-
        # by-column emit would need a separate table-level helper).
        fk_lookup: dict[str, ForeignKeyInfo] = {}
        for fk in outgoing_fks:
            if len(fk.from_columns) == 1:
                fk_lookup[fk.from_columns[0]] = fk

        column_lines = [
            _render_column(
                col,
                pk_columns=set(pk_columns) if len(pk_columns) == 1 else set(),
                unique_columns=single_uniques,
                fk_lookup=fk_lookup,
                enum_names=enum_names,
            )
            for col in columns
        ]

        extras: list[str] = []
        if len(pk_columns) > 1:
            extras.append(_render_composite_pk(table.name, pk_columns))
        for name, cols in composite_uniques:
            extras.append(_render_composite_unique(table.name, name, cols))
        for idx in indexes:
            # Skip indexes already covered by PK / unique constraints
            # (PG auto-creates an index for each; emitting both would
            # produce duplicate declarations).
            if any(con.name == idx.name for con in constraints):
                continue
            extras.append(_render_index(idx))

        table_var = _camel_case(table.name)
        body = f'export const {table_var} = pgTable("{table.name}", {{\n'
        body += "\n".join(column_lines)
        body += "\n}"
        if extras:
            body += ", (table) => ({\n  " + ",\n  ".join(line.lstrip() for line in extras) + "\n})"
        body += ");"
        body_blocks.append(body)

    body_text = "\n\n".join(body_blocks).rstrip() + "\n"

    # Build the import line from what's actually referenced.
    referenced = _collect_used_helpers(body_text) & _KNOWN_HELPERS
    if "sql`" in body_text:
        referenced.add("sql")
    referenced_sorted = sorted(referenced)
    # `pgTable` is always used (every table emits one).
    if "pgTable" not in referenced_sorted:
        referenced_sorted = ["pgTable", *referenced_sorted]
    import_line = "import { " + ", ".join(referenced_sorted) + ' } from "drizzle-orm/pg-core";\n'
    if "sql" in referenced_sorted:
        # `sql` lives in the root drizzle-orm package, not pg-core.
        import_line = (
            'import { sql } from "drizzle-orm";\n'
            + "import { "
            + ", ".join(h for h in referenced_sorted if h != "sql")
            + ' } from "drizzle-orm/pg-core";\n'
        )

    return import_line + "\n" + body_text


def _iter_helpers_in_use(*sources: Iterable[str]) -> set[str]:
    """Test hook: collect helper names that appear in the supplied source lines."""
    used: set[str] = set()
    for src in sources:
        for line in src:
            used |= _collect_used_helpers(line) & _KNOWN_HELPERS
    return used
