"""Schema → SQLAlchemy 2.0 declarative models exporter.

Reads the PostgreSQL catalog via :mod:`mcpg.introspection` and emits a
Python file with SQLAlchemy 2.0-style ``DeclarativeBase`` models
(``Mapped[T]`` + ``mapped_column``). Mirrors the structure of
:mod:`mcpg.prisma` and :mod:`mcpg.drizzle` so the three exporters can
be reasoned about together.

Scope is deliberately narrow per Batch G: catalog → DSL only. No
``__init__`` for the package, no migration scripts, no SQLAlchemy →
DDL parsing. The agent uses the emitted file as the source of truth
for its Python project.
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
_SERIAL_DEFAULT_RE = re.compile(r"nextval\(['\"]([^'\"]+)['\"]")


class SqlAlchemyExportError(Exception):
    """Raised when a SQLAlchemy export call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise SqlAlchemyExportError(f"invalid {kind} name: {name!r}")


def _pascal_case(snake_name: str) -> str:
    """Convert ``snake_case`` to ``PascalCase`` for the model class name."""
    parts = [p for p in snake_name.split("_") if p]
    return "".join(part[:1].upper() + part[1:] for part in parts) or snake_name.capitalize()


def _strip_type_params(data_type: str) -> str:
    return re.sub(r"\(.*\)", "", data_type).strip()


# Maps a PG type → (SQLAlchemy type class, Python annotation, import source).
# import source is one of: "core" (sqlalchemy), "pg" (sqlalchemy.dialects.postgresql),
# "typing" (typing stdlib).
_TYPE_MAP: dict[str, tuple[str, str, str]] = {
    "smallint": ("SmallInteger", "int", "core"),
    "integer": ("Integer", "int", "core"),
    "bigint": ("BigInteger", "int", "core"),
    "real": ("Float", "float", "core"),
    "double precision": ("Float", "float", "core"),
    "numeric": ("Numeric", "Decimal", "core"),
    "decimal": ("Numeric", "Decimal", "core"),
    "boolean": ("Boolean", "bool", "core"),
    "text": ("Text", "str", "core"),
    "character varying": ("String", "str", "core"),
    "varchar": ("String", "str", "core"),
    "character": ("String", "str", "core"),
    "char": ("String", "str", "core"),
    "uuid": ("Uuid", "UUID", "core"),
    "json": ("JSON", "dict", "core"),
    "jsonb": ("JSONB", "dict", "pg"),
    "date": ("Date", "date", "core"),
    "time without time zone": ("Time", "time", "core"),
    "time with time zone": ("Time", "time", "core"),
    "timestamp without time zone": ("DateTime", "datetime", "core"),
    "timestamp with time zone": ("DateTime", "datetime", "core"),
    "bytea": ("LargeBinary", "bytes", "core"),
    "interval": ("Interval", "timedelta", "core"),
}


def _python_type_for(annotation: str, *, nullable: bool) -> str:
    if nullable:
        return f"Optional[{annotation}]"
    return annotation


def _sa_type_for(column: ColumnInfo, enum_names: set[str]) -> tuple[str, str, str]:
    """Return ``(sa_type_call, python_annotation, import_source)`` for a column.

    Enum columns route to ``Enum(EnumName)`` so the generated Python
    enum class is the source of truth.
    """
    raw = column.data_type
    base = _strip_type_params(raw).lower()
    qualified = base.split(".")[-1] if "." in base else base
    if base in enum_names or raw in enum_names or qualified in enum_names:
        enum_class = _pascal_case(qualified)
        return f"Enum({enum_class})", enum_class, "core"
    mapped = _TYPE_MAP.get(base)
    if mapped is None:
        # Unknown type — fall back to String so the file imports and
        # the agent can patch the column type.
        return "String", "str", "core"
    sa_class, py_type, source = mapped
    # varchar(N) / char(N) — carry the length into the type call.
    if sa_class == "String":
        match = re.search(r"\((\d+)\)", raw)
        if match:
            return f"String({match.group(1)})", py_type, source
    if sa_class == "Numeric":
        match = re.search(r"\((\d+),\s*(\d+)\)", raw)
        if match:
            return f"Numeric({match.group(1)}, {match.group(2)})", py_type, source
    if sa_class == "DateTime" and "with time zone" in raw:
        return "DateTime(timezone=True)", py_type, source
    return sa_class, py_type, source


def _is_serial(column: ColumnInfo) -> bool:
    return column.default is not None and bool(_SERIAL_DEFAULT_RE.search(column.default))


def _render_default(column: ColumnInfo) -> str | None:
    """Map a PG default to a ``server_default=...`` kwarg.

    nextval() (serial) is skipped — SQLAlchemy generates it from the
    Integer/BigInteger column type with autoincrement.
    """
    if column.default is None or _is_serial(column):
        return None
    default = column.default.strip()
    cast_stripped = re.sub(r"::[A-Za-z_][A-Za-z0-9_ ]*$", "", default)
    if cast_stripped.lower() in {"now()", "current_timestamp"}:
        return "server_default=func.now()"
    if cast_stripped.lower() in {"true", "false"}:
        return f'server_default=text("{cast_stripped.lower()}")'
    if re.fullmatch(r"-?\d+", cast_stripped) or re.fullmatch(r"-?\d+\.\d+", cast_stripped):
        return f'server_default=text("{cast_stripped}")'
    if cast_stripped.startswith("'") and cast_stripped.endswith("'"):
        # Keep the PG-quoted literal as a SQL text() server_default —
        # parametrised inserts will use it correctly.
        escaped = cast_stripped.replace("\\", "\\\\").replace('"', '\\"')
        return f'server_default=text("{escaped}")'
    # Unknown — preserve the raw expression as text() so it round-trips.
    escaped = cast_stripped.replace("\\", "\\\\").replace('"', '\\"')
    return f'server_default=text("{escaped}")'


_PK_COLS_RE = re.compile(r"PRIMARY KEY\s*\(([^)]+)\)", re.IGNORECASE)
_UNIQUE_COLS_RE = re.compile(r"UNIQUE\s*\(([^)]+)\)", re.IGNORECASE)


def _parse_columns_in_definition(definition: str, regex: re.Pattern[str]) -> list[str]:
    match = regex.search(definition)
    if not match:
        return []
    return [col.strip().strip('"') for col in match.group(1).split(",")]


def _render_column(
    column: ColumnInfo,
    *,
    schema: str,
    pk_columns: set[str],
    unique_columns: set[str],
    fk_lookup: dict[str, ForeignKeyInfo],
    enum_names: set[str],
) -> tuple[str, set[str], set[str], set[str]]:
    """Return ``(line, core_imports, pg_imports, typing_imports)``.

    Caller aggregates the import sets so the final file's import block
    references only what's actually used.
    """
    sa_type_call, py_annotation, source = _sa_type_for(column, enum_names)
    core_imports: set[str] = set()
    pg_imports: set[str] = set()
    typing_imports: set[str] = set()
    if source == "core":
        # The sa_type_call may include a parenthesised arg ("String(120)") or
        # an enum ref ("Enum(Status)") — split off the bare class name for the
        # import set. Enum needs the Enum import; the Status class itself is
        # generated in the same file so no extra import for it.
        core_imports.add(sa_type_call.split("(", 1)[0])
    else:  # pg
        pg_imports.add(sa_type_call.split("(", 1)[0])

    args: list[str] = [sa_type_call]
    # Foreign keys: a single-column FK uses ForeignKey("schema.table.col")
    # inline; composite FKs are deferred to __table_args__.
    fk = fk_lookup.get(column.name)
    if fk is not None and len(fk.from_columns) == 1:
        target = f"{schema}.{fk.to_table}.{fk.to_columns[0]}"
        args.append(f'ForeignKey("{target}")')
        core_imports.add("ForeignKey")
    kwargs: list[str] = []
    if column.name in pk_columns:
        kwargs.append("primary_key=True")
    if column.name in unique_columns:
        kwargs.append("unique=True")
    if not column.nullable:
        kwargs.append("nullable=False")
    default_kwarg = _render_default(column)
    if default_kwarg is not None:
        kwargs.append(default_kwarg)
        if "func.now" in default_kwarg:
            core_imports.add("func")
        elif "text(" in default_kwarg:
            core_imports.add("text")
    call = ", ".join(args + kwargs)
    # Python annotation — Optional[T] when nullable.
    annotation = _python_type_for(py_annotation, nullable=column.nullable)
    if column.nullable:
        typing_imports.add("Optional")
    if py_annotation in {"datetime", "date", "time", "timedelta"}:
        typing_imports.add(py_annotation)  # actually a datetime import — handled in caller
    if py_annotation == "UUID":
        typing_imports.add("UUID")
    if py_annotation == "Decimal":
        typing_imports.add("Decimal")

    return (
        f"    {column.name}: Mapped[{annotation}] = mapped_column({call})",
        core_imports,
        pg_imports,
        typing_imports,
    )


_PY_KEYWORDS = frozenset(
    {
        "False",
        "None",
        "True",
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
    }
)
_IDENT_SAFE = re.compile(r"[^A-Za-z0-9_]")


def _safe_member_name(label: str) -> str:
    """Coerce a PG enum label into a valid Python identifier.

    Non-identifier chars collapse to underscore; a digit-leading label
    gets an underscore prefix; Python keywords get a trailing underscore.
    The original label remains the enum value — only the Python
    attribute name is sanitised, so user code keeps round-tripping the
    real label to PG.
    """
    member = _IDENT_SAFE.sub("_", label).strip("_") or "value"
    if member[0].isdigit():
        member = "_" + member
    if member in _PY_KEYWORDS:
        member = member + "_"
    return member


def _render_enum_class(name: str, values: list[str]) -> str:
    """Emit a Python ``enum.Enum`` whose values are the PG labels verbatim.

    Uses the class-body form when every label is a valid Python
    identifier (the common case), and falls back to the functional
    ``Status = enum.Enum("Status", {"member": "label"})`` form when
    any label needs sanitising — so labels with hyphens, spaces,
    leading digits, or Python keywords don't make the generated file
    a SyntaxError.
    """
    class_name = _pascal_case(name)
    members = [_safe_member_name(v) for v in values]
    # Detect collisions or any sanitisation that changed the label.
    needs_functional = len(set(members)) != len(members) or any(m != v for m, v in zip(members, values, strict=True))
    if not needs_functional:
        body = "\n".join(f'    {v} = "{v}"' for v in values)
        return f"class {class_name}(enum.Enum):\n{body}\n"
    pairs = ", ".join(f'"{m}": "{v}"' for m, v in zip(members, values, strict=True))
    return f'{class_name} = enum.Enum("{class_name}", {{{pairs}}})\n'


def _render_table_args(schema: str, composite_uniques: list[tuple[str, list[str]]]) -> str | None:
    """Build the ``__table_args__`` for composite uniques + schema."""
    pieces: list[str] = []
    for name, cols in composite_uniques:
        col_literals = ", ".join(f'"{c}"' for c in cols)
        pieces.append(f'UniqueConstraint({col_literals}, name="{name}")')
    schema_kwarg = f'{{"schema": "{schema}"}}'
    if not pieces:
        return f"    __table_args__ = {schema_kwarg}"
    joined = ", ".join(pieces)
    return f"    __table_args__ = ({joined}, {schema_kwarg})"


async def generate_sqlalchemy_models(driver: SqlDriver, schema: str) -> str:
    """Emit a SQLAlchemy 2.0 declarative model file for ``schema``.

    Returns a Python source string. The model classes use
    ``DeclarativeBase`` + ``Mapped[T]`` + ``mapped_column``. Composite
    foreign keys are a v1 gap (the column-by-column emit handles only
    single-column FKs); composite unique constraints land in
    ``__table_args__``.

    Raises:
        SqlAlchemyExportError: When the schema name or any table/column
            name needs PostgreSQL delimited-identifier quoting.
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

    core_imports: set[str] = set()
    pg_imports: set[str] = set()
    typing_imports: set[str] = set()
    body_blocks: list[str] = []

    if enums:
        for enum in sorted(enums, key=lambda e: e.name):
            body_blocks.append(_render_enum_class(enum.name, list(enum.values)))

    for table in tables:
        columns = await describe_table(driver, schema, table.name)
        constraints = await list_constraints(driver, schema, table.name)

        pk_columns: list[str] = []
        single_uniques: set[str] = set()
        composite_uniques: list[tuple[str, list[str]]] = []
        for con in constraints:
            if con.type == "primary_key":
                pk_columns = _parse_columns_in_definition(con.definition, _PK_COLS_RE)
            elif con.type == "unique":
                cols = _parse_columns_in_definition(con.definition, _UNIQUE_COLS_RE)
                if len(cols) == 1:
                    single_uniques.update(cols)
                elif cols:
                    composite_uniques.append((con.name, cols))
        if composite_uniques:
            core_imports.add("UniqueConstraint")

        outgoing_fks = [fk for fk in fks_by_source.get(table.name, []) if fk.to_table in entity_names]
        fk_lookup: dict[str, ForeignKeyInfo] = {}
        for fk in outgoing_fks:
            if len(fk.from_columns) == 1:
                fk_lookup[fk.from_columns[0]] = fk

        column_lines: list[str] = []
        for col in columns:
            line, c_imp, p_imp, t_imp = _render_column(
                col,
                schema=schema,
                pk_columns=set(pk_columns),
                unique_columns=single_uniques,
                fk_lookup=fk_lookup,
                enum_names=enum_names,
            )
            column_lines.append(line)
            core_imports |= c_imp
            pg_imports |= p_imp
            typing_imports |= t_imp

        class_name = _pascal_case(table.name)
        block = f"class {class_name}(Base):\n"
        block += f'    __tablename__ = "{table.name}"\n'
        ta = _render_table_args(schema, composite_uniques)
        if ta:
            block += ta + "\n"
        block += "\n".join(column_lines)
        block += "\n"
        body_blocks.append(block)

    # Assemble the import block. Order: stdlib / typing first, then
    # sqlalchemy core, then sqlalchemy.dialects.postgresql.
    datetime_imports = {n for n in typing_imports if n in {"datetime", "date", "time", "timedelta"}}
    other_typing = typing_imports - datetime_imports
    import_lines: list[str] = []
    if datetime_imports:
        import_lines.append("from datetime import " + ", ".join(sorted(datetime_imports)))
    if "UUID" in other_typing:
        import_lines.append("from uuid import UUID")
        other_typing.discard("UUID")
    if "Decimal" in other_typing:
        import_lines.append("from decimal import Decimal")
        other_typing.discard("Decimal")
    if "Optional" in other_typing:
        import_lines.append("from typing import Optional")
        other_typing.discard("Optional")
    if enums:
        import_lines.append("import enum")
    if core_imports:
        import_lines.append("from sqlalchemy import " + ", ".join(sorted(core_imports)))
    import_lines.append("from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column")
    if pg_imports:
        import_lines.append("from sqlalchemy.dialects.postgresql import " + ", ".join(sorted(pg_imports)))

    header = "\n".join(import_lines) + "\n\n\nclass Base(DeclarativeBase):\n    pass\n"
    return header + "\n\n" + "\n\n".join(body_blocks).rstrip() + "\n"
