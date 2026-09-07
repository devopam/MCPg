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

from mcpg.errors import MCPgError
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
from mcpg.sql import SqlDriver

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


class PrismaError(MCPgError):
    """Raised when a Prisma schema cannot be emitted."""


def _check_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise PrismaError(
            f"invalid {kind} name {name!r}; generate_prisma_schema only accepts plain "
            f"identifiers [A-Za-z_][A-Za-z0-9_]* because the name becomes a source "
            f"identifier in generated Prisma schema. SQL tools (export_table, "
            f"dump_database, …) accept delimited names via quoting — see docs/identifier-policy.md."
        )
