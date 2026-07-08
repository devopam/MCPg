"""Schema → jOOQ (Java) configuration exporter.

`jOOQ <https://www.jooq.org/>`_ generates type-safe Java code from a
live database using its code generator. The canonical input is an XML
or programmatic *configuration* file pointing at the database and
declaring what to generate. This exporter writes that configuration
from a PG catalog so a Java agent can drop the file straight into a
build.

Unlike the other Batch G exporters, jOOQ does NOT consume a DSL of
type-mapped declarations — it reads the live database itself at
generation time. So the artefact this module emits is the
``jooq-codegen`` ``<configuration>`` XML pointing at the schema +
listing the tables the user wants generated. The user runs
``mvn jooq-codegen:generate`` (or the equivalent Gradle task)
themselves, and jOOQ does the heavy lifting from the catalog.

This makes the exporter much smaller than the others — most of the
mapping logic lives inside jOOQ. We provide:

- The configuration scaffold (jdbc URL placeholder, dialect, target
  package, output directory).
- An explicit ``<includes>`` regex covering exactly the base tables
  in the schema, so generation is deterministic even if the schema
  later grows new objects we don't want generated.
- An ``<excludes>`` line listing the audit/migration helper tables
  MCPg creates so they don't leak into the generated code.
- Optional ``<forcedTypes>`` for JSON/JSONB columns so they map to
  ``org.jooq.JSON`` / ``org.jooq.JSONB`` Java types out of the box.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

from mcpg.introspection import (
    ColumnInfo,
    describe_table,
    list_tables,
)
from mcpg.sql import SqlDriver

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# MCPg's own bookkeeping tables — never useful to generate jOOQ code for.
_EXCLUDED_TABLE_PATTERN = "mcpg_audit\\..*|mcpg_migrations\\..*"


class JooqExportError(Exception):
    """Raised when a jOOQ export call is rejected or fails."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise JooqExportError(f"invalid {kind} name: {name!r}")


def _strip_type_params(data_type: str) -> str:
    return re.sub(r"\(.*\)", "", data_type).strip()


def _is_json_column(column: ColumnInfo) -> tuple[bool, str | None]:
    """Return ``(is_json, java_type)`` for the column.

    ``json`` → ``org.jooq.JSON``, ``jsonb`` → ``org.jooq.JSONB``.
    Everything else is ``(False, None)``.
    """
    base = _strip_type_params(column.data_type).lower()
    if base == "jsonb":
        return True, "org.jooq.JSONB"
    if base == "json":
        return True, "org.jooq.JSON"
    return False, None


def _render_forced_type(table: str, column: str, java_type: str, schema: str) -> str:
    """Build one ``<forcedType>`` block tying a column to a Java type.

    The ``expression`` is a regex anchored on the fully-qualified
    column path so the rule doesn't accidentally match other columns.
    """
    # Anchor the expression so it only matches THIS column, not a
    # similarly-named column in another table.
    return (
        "            <forcedType>\n"
        f"                <userType>{escape(java_type)}</userType>\n"
        "                <converter>org.jooq.Converter.ofNullable("
        "java.lang.String.class, "
        f"{escape(java_type)}.class, "
        f"{escape(java_type)}::valueOf, java.lang.Object::toString)</converter>\n"
        f"                <expression>{escape(schema)}\\.{escape(table)}\\.{escape(column)}</expression>\n"
        "                <types>.*</types>\n"
        "            </forcedType>"
    )


async def generate_jooq_config(
    driver: SqlDriver,
    schema: str,
    *,
    target_package: str = "com.example.jooq",
    target_directory: str = "src/main/java",
) -> str:
    """Emit a ``jooq-codegen`` ``<configuration>`` XML for ``schema``.

    The XML names every base table in the schema explicitly via an
    ``<includes>`` regex, excludes MCPg's bookkeeping tables, and
    emits a ``<forcedType>`` for every ``json`` / ``jsonb`` column so
    the generated DAOs use ``org.jooq.JSON`` / ``org.jooq.JSONB``
    instead of ``Object``.

    Args:
        driver: SQL driver bound to the live database.
        schema: PG schema to generate from. Must be a plain identifier.
        target_package: Java package the generated code lands in.
        target_directory: Source root the generated package lives under.

    Raises:
        JooqExportError: When the schema name, or any table / column
            name within it, requires PostgreSQL delimited-identifier
            quoting.
    """
    _check_identifier(schema, "schema")
    tables = [t for t in await list_tables(driver, schema) if t.type == "BASE TABLE" and not t.is_partition]
    for t in tables:
        _check_identifier(t.name, "table")

    # Build the includes regex out of explicit table names — anchored
    # so a future ``widget2`` table won't accidentally get generated.
    if tables:
        # Each name is already a plain identifier (validated above), so
        # no regex-meta-character escaping is needed here.
        includes_expr = "|".join(f"{schema}\\.{t.name}" for t in tables)
    else:
        includes_expr = ""  # nothing to generate

    # Collect forced-type entries for JSON / JSONB columns across every table.
    forced_types: list[str] = []
    for table in tables:
        columns = await describe_table(driver, schema, table.name)
        for col in columns:
            is_json, java_type = _is_json_column(col)
            if is_json and java_type is not None:
                _check_identifier(col.name, "column")
                forced_types.append(_render_forced_type(table.name, col.name, java_type, schema))

    forced_types_block = ""
    if forced_types:
        forced_types_block = "        <forcedTypes>\n" + "\n".join(forced_types) + "\n        </forcedTypes>\n"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<configuration xmlns="http://www.jooq.org/xsd/jooq-codegen-3.19.0.xsd">\n'
        "    <!--\n"
        f"      Auto-generated by MCPg for schema {escape(schema)!r}.\n"
        "      Set the jdbc URL / user / password to point at YOUR\n"
        "      target database — these are placeholders.\n"
        "    -->\n"
        "    <jdbc>\n"
        "        <driver>org.postgresql.Driver</driver>\n"
        "        <url>jdbc:postgresql://localhost:5432/mydb</url>\n"
        "        <user>postgres</user>\n"
        "        <password></password>\n"
        "    </jdbc>\n"
        "    <generator>\n"
        "        <database>\n"
        "            <name>org.jooq.meta.postgres.PostgresDatabase</name>\n"
        f"            <inputSchema>{escape(schema)}</inputSchema>\n"
        f"            <includes>{escape(includes_expr)}</includes>\n"
        f"            <excludes>{escape(_EXCLUDED_TABLE_PATTERN)}</excludes>\n"
        "        </database>\n"
        f"{forced_types_block}"
        "        <target>\n"
        f"            <packageName>{escape(target_package)}</packageName>\n"
        f"            <directory>{escape(target_directory)}</directory>\n"
        "        </target>\n"
        "    </generator>\n"
        "</configuration>\n"
    )
