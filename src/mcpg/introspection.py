"""Schema-introspection queries against the PostgreSQL catalog.

Each function runs a single read-only catalog query through a vendored
``SqlDriver`` and maps the rows to a typed result. Queries are parameterised;
no value is interpolated into SQL text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SqlDriver

# Schemas that belong to PostgreSQL itself rather than the user.
_SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})

# pg_constraint.contype codes -> readable constraint type.
_CONSTRAINT_TYPES = {
    "p": "primary_key",
    "f": "foreign_key",
    "u": "unique",
    "c": "check",
    "x": "exclusion",
}

# pg_proc.prokind codes -> readable routine kind.
_ROUTINE_KINDS = {"f": "function", "p": "procedure", "a": "aggregate", "w": "window"}

# pg_partitioned_table.partstrat codes -> readable partitioning strategy.
_PARTITION_STRATEGIES = {"r": "range", "l": "list", "h": "hash"}

# pg_class.relkind codes -> the table type reported by list_tables.
_TABLE_TYPES = {"r": "BASE TABLE", "p": "BASE TABLE", "v": "VIEW", "f": "FOREIGN"}


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    """A database schema."""

    name: str


@dataclass(frozen=True, slots=True)
class TableInfo:
    """A table or view within a schema.

    ``partitioned`` is ``True`` for a partitioned table (the parent);
    ``is_partition`` is ``True`` when the table is itself a partition of one.
    """

    name: str
    type: str
    partitioned: bool
    is_partition: bool


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """A column of a table.

    ``vector_dimension`` is set for ``pgvector`` ``vector(N)`` columns and is
    ``None`` for every other column type.
    """

    name: str
    data_type: str
    nullable: bool
    default: str | None
    vector_dimension: int | None


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """An index on a table.

    ``method`` is the access method — a built-in one (``btree``, ``gin``,
    ``gist``, ``brin``, ``hash``, ``spgist``) or an extension's (e.g.
    ``hnsw`` / ``ivfflat`` from ``pgvector``). ``partitioned`` is ``True``
    for a partitioned index — the template propagated to each partition.
    """

    name: str
    method: str
    definition: str
    partitioned: bool


@dataclass(frozen=True, slots=True)
class ViewInfo:
    """A view or materialized view within a schema."""

    name: str
    materialized: bool
    definition: str


@dataclass(frozen=True, slots=True)
class FunctionInfo:
    """A function or procedure within a schema.

    ``kind`` is ``function``, ``procedure``, ``aggregate``, ``window``, or
    ``other``. ``returns`` is ``None`` for procedures.
    """

    name: str
    kind: str
    arguments: str
    returns: str | None
    language: str


@dataclass(frozen=True, slots=True)
class TriggerInfo:
    """A user-defined trigger on a table."""

    name: str
    function: str
    definition: str


@dataclass(frozen=True, slots=True)
class ConstraintInfo:
    """A constraint on a table.

    ``type`` is ``primary_key``, ``foreign_key``, ``unique``, ``check``,
    ``exclusion``, or ``other``.
    """

    name: str
    type: str
    definition: str


@dataclass(frozen=True, slots=True)
class ForeignKeyInfo:
    """A foreign-key constraint resolved to its referenced columns.

    ``from_columns`` and ``to_columns`` are aligned by ordinal position —
    ``from_columns[i]`` references ``to_columns[i]`` of
    ``to_schema.to_table``.
    """

    name: str
    from_table: str
    from_columns: list[str]
    to_schema: str
    to_table: str
    to_columns: list[str]


@dataclass(frozen=True, slots=True)
class PartitionInfo:
    """A partition of a partitioned table.

    ``bounds`` is the partition's bound expression — e.g.
    ``FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')``.
    """

    name: str
    bounds: str


@dataclass(frozen=True, slots=True)
class PartitionSet:
    """How a table is partitioned, with its partitions.

    ``partitioned`` is ``False`` for an ordinary table, in which case
    ``strategy`` is ``None`` and ``partitions`` is empty.
    """

    partitioned: bool
    strategy: str | None
    partitions: list[PartitionInfo]


@dataclass(frozen=True, slots=True)
class PolicyInfo:
    """A Row-Level-Security policy on a table.

    ``permissive`` is ``True`` for a permissive policy, ``False`` for a
    restrictive one. ``using_expression`` and ``check_expression`` are the
    policy's ``USING`` and ``WITH CHECK`` predicates, or ``None``.
    """

    name: str
    command: str
    permissive: bool
    roles: list[str]
    using_expression: str | None
    check_expression: str | None


@dataclass(frozen=True, slots=True)
class PolicySet:
    """The Row-Level-Security configuration of a table.

    ``rls_enabled`` reflects whether row security is switched on for the
    table — policies can exist while it is off, in which case they are
    inert.
    """

    rls_enabled: bool
    policies: list[PolicyInfo]


@dataclass(frozen=True, slots=True)
class RoleInfo:
    """A database role and its attributes.

    ``connection_limit`` is ``-1`` when the role has no connection cap.
    ``member_of`` lists the roles this role is a direct member of.
    """

    name: str
    superuser: bool
    create_role: bool
    create_db: bool
    can_login: bool
    replication: bool
    bypass_rls: bool
    connection_limit: int
    member_of: list[str]


@dataclass(frozen=True, slots=True)
class GrantInfo:
    """A privilege granted on a table.

    ``grantable`` is ``True`` when the grantee may pass the privilege on
    (``WITH GRANT OPTION``).

    ``acl`` is the raw PostgreSQL aclitem string (``user=privs/grantor``)
    that ``pg_get_acl()`` returns on PG 19+; it's ``None`` on
    PG ≤ 18 (where the function doesn't exist and the field stays
    absent from the wire). Useful for matching what ``\\dp`` shows
    and for parsing privilege flags (``r``=SELECT, ``w``=UPDATE,
    ``a``=INSERT, ``d``=DELETE, ``D``=TRUNCATE, ``x``=REFERENCES,
    ``t``=TRIGGER) without reverse-engineering the
    information_schema row.
    """

    grantee: str
    privilege: str
    grantable: bool
    grantor: str
    acl: str | None = None


@dataclass(frozen=True, slots=True)
class SequenceInfo:
    """A sequence within a schema.

    ``last_value`` is ``None`` when the sequence has not yet been used or is
    not readable by the connected role.
    """

    name: str
    data_type: str
    start_value: int
    min_value: int
    max_value: int
    increment: int
    cycle: bool
    last_value: int | None


@dataclass(frozen=True, slots=True)
class EnumInfo:
    """An enum type within a schema and its labels in sort order."""

    name: str
    values: list[str]


@dataclass(frozen=True, slots=True)
class DomainInfo:
    """A domain type within a schema.

    ``constraints`` are the rendered ``CHECK`` constraint definitions
    attached to the domain, in catalog order.
    """

    name: str
    base_type: str
    nullable: bool
    default: str | None
    constraints: list[str]


@dataclass(frozen=True, slots=True)
class CompositeAttribute:
    """A column of a composite type."""

    name: str
    data_type: str


@dataclass(frozen=True, slots=True)
class CompositeTypeInfo:
    """A standalone composite type within a schema (excludes table row-types)."""

    name: str
    attributes: list[CompositeAttribute]


@dataclass(frozen=True, slots=True)
class ForeignDataWrapperInfo:
    """A foreign-data wrapper installed in the database.

    ``handler`` and ``validator`` are the qualified function names, or
    ``None`` when the FDW does not define one.
    """

    name: str
    handler: str | None
    validator: str | None
    options: dict[str, str]


@dataclass(frozen=True, slots=True)
class ForeignServerInfo:
    """A foreign server defined for an FDW."""

    name: str
    wrapper: str
    type: str | None
    version: str | None
    options: dict[str, str]


@dataclass(frozen=True, slots=True)
class ForeignTableInfo:
    """A foreign table within a schema."""

    name: str
    server: str
    options: dict[str, str]


@dataclass(frozen=True, slots=True)
class UserMappingInfo:
    """A role-to-foreign-server mapping.

    ``user`` is ``"public"`` for the catch-all mapping.
    """

    user: str
    server: str
    options: dict[str, str]


@dataclass(frozen=True, slots=True)
class PublicationInfo:
    """A logical-replication publication.

    ``tables`` lists ``"schema.table"`` qualified names included in the
    publication; empty when ``all_tables`` is true.
    """

    name: str
    owner: str
    all_tables: bool
    publishes_insert: bool
    publishes_update: bool
    publishes_delete: bool
    publishes_truncate: bool
    tables: list[str]


@dataclass(frozen=True, slots=True)
class SubscriptionInfo:
    """A logical-replication subscription.

    ``publications`` lists the publication names this subscription consumes.
    ``connection`` is the libpq connection string; password fields are
    not redacted by PostgreSQL and the caller should treat the field as
    sensitive.
    """

    name: str
    owner: str
    enabled: bool
    connection: str
    publications: list[str]


@dataclass(frozen=True, slots=True)
class ExtensionInfo:
    """An installed PostgreSQL extension."""

    name: str
    version: str


@dataclass(frozen=True, slots=True)
class AvailableExtension:
    """An extension available to the database, installed or not."""

    name: str
    default_version: str
    installed_version: str | None
    installed: bool


def _is_system_schema(name: str) -> bool:
    return name in _SYSTEM_SCHEMAS or name.startswith("pg_")


def _parse_options(raw: list[str | None] | None) -> dict[str, str]:
    """Parse a PostgreSQL ``text[]`` of ``"key=value"`` entries into a dict.

    Tolerant of catalog quirks: ``NULL`` array elements are skipped, and
    entries without an ``=`` separator are ignored. When a key appears more
    than once the last occurrence wins.
    """
    options: dict[str, str] = {}
    for item in raw or []:
        if not isinstance(item, str):
            continue
        key, sep, value = item.partition("=")
        if sep:
            options[key] = value
    return options


async def list_schemas(driver: SqlDriver, *, include_system: bool = False) -> list[SchemaInfo]:
    """List schemas, excluding PostgreSQL's own schemas unless asked."""
    rows = await driver.execute_query(
        "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name",
        force_readonly=True,
    )
    schemas = [SchemaInfo(name=row.cells["schema_name"]) for row in rows or []]
    if include_system:
        return schemas
    return [schema for schema in schemas if not _is_system_schema(schema.name)]


async def list_tables(driver: SqlDriver, schema: str) -> list[TableInfo]:
    """List the tables and views in a schema, flagging partitioning."""
    rows = await driver.execute_query(
        "SELECT c.relname AS name, c.relkind AS relkind, "
        "c.relispartition AS is_partition "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'f') "
        "ORDER BY c.relname",
        params=[schema],
        force_readonly=True,
    )
    return [
        TableInfo(
            name=row.cells["name"],
            type=_TABLE_TYPES.get(row.cells["relkind"], "other"),
            partitioned=row.cells["relkind"] == "p",
            is_partition=row.cells["is_partition"],
        )
        for row in rows or []
    ]


async def describe_table(driver: SqlDriver, schema: str, table: str) -> list[ColumnInfo]:
    """Describe the columns of a table, in ordinal order.

    Reads the catalog directly so the display type comes from ``format_type``
    and ``pgvector`` ``vector(N)`` columns report their dimension.
    """
    rows = await driver.execute_query(
        "SELECT a.attname AS column_name, "
        "format_type(a.atttypid, a.atttypmod) AS data_type, "
        "NOT a.attnotnull AS nullable, "
        "pg_get_expr(d.adbin, d.adrelid) AS column_default, "
        "t.typname AS type_name, a.atttypmod AS type_mod "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        params=[schema, table],
        force_readonly=True,
    )
    return [
        ColumnInfo(
            name=row.cells["column_name"],
            data_type=row.cells["data_type"],
            nullable=row.cells["nullable"],
            default=row.cells["column_default"],
            vector_dimension=(
                row.cells["type_mod"] if row.cells["type_name"] == "vector" and row.cells["type_mod"] > 0 else None
            ),
        )
        for row in rows or []
    ]


async def list_indexes(driver: SqlDriver, schema: str, table: str) -> list[IndexInfo]:
    """List the indexes defined on a table, with their access method."""
    rows = await driver.execute_query(
        "SELECT i.relname AS name, am.amname AS method, i.relkind AS relkind, "
        "pg_get_indexdef(i.oid) AS definition "
        "FROM pg_class t "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN pg_index ix ON ix.indrelid = t.oid "
        "JOIN pg_class i ON i.oid = ix.indexrelid "
        "JOIN pg_am am ON am.oid = i.relam "
        "WHERE n.nspname = %s AND t.relname = %s ORDER BY i.relname",
        params=[schema, table],
        force_readonly=True,
    )
    return [
        IndexInfo(
            name=row.cells["name"],
            method=row.cells["method"],
            definition=row.cells["definition"],
            partitioned=row.cells["relkind"] == "I",
        )
        for row in rows or []
    ]


async def list_views(driver: SqlDriver, schema: str) -> list[ViewInfo]:
    """List the views and materialized views in a schema, with definitions."""
    rows = await driver.execute_query(
        "SELECT c.relname AS name, (c.relkind = 'm') AS materialized, "
        "pg_get_viewdef(c.oid, true) AS definition "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('v', 'm') ORDER BY c.relname",
        params=[schema],
        force_readonly=True,
    )
    return [
        ViewInfo(
            name=row.cells["name"],
            materialized=row.cells["materialized"],
            definition=row.cells["definition"],
        )
        for row in rows or []
    ]


async def list_functions(driver: SqlDriver, schema: str) -> list[FunctionInfo]:
    """List the functions and procedures defined in a schema."""
    rows = await driver.execute_query(
        "SELECT p.proname AS name, p.prokind AS kind_code, "
        "pg_get_function_arguments(p.oid) AS arguments, "
        "pg_get_function_result(p.oid) AS returns, l.lanname AS language "
        "FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_language l ON l.oid = p.prolang "
        "WHERE n.nspname = %s ORDER BY p.proname, p.oid",
        params=[schema],
        force_readonly=True,
    )
    return [
        FunctionInfo(
            name=row.cells["name"],
            kind=_ROUTINE_KINDS.get(row.cells["kind_code"], "other"),
            arguments=row.cells["arguments"],
            returns=row.cells["returns"],
            language=row.cells["language"],
        )
        for row in rows or []
    ]


async def list_triggers(driver: SqlDriver, schema: str, table: str) -> list[TriggerInfo]:
    """List the user-defined triggers on a table.

    Internal triggers (such as those enforcing foreign keys) are excluded.
    """
    rows = await driver.execute_query(
        "SELECT t.tgname AS name, p.proname AS function, "
        "pg_get_triggerdef(t.oid) AS definition "
        "FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE n.nspname = %s AND c.relname = %s AND NOT t.tgisinternal "
        "ORDER BY t.tgname",
        params=[schema, table],
        force_readonly=True,
    )
    return [
        TriggerInfo(
            name=row.cells["name"],
            function=row.cells["function"],
            definition=row.cells["definition"],
        )
        for row in rows or []
    ]


async def list_constraints(driver: SqlDriver, schema: str, table: str) -> list[ConstraintInfo]:
    """List the constraints on a table — keys, unique, check, exclusion."""
    rows = await driver.execute_query(
        "SELECT con.conname AS name, con.contype AS type_code, "
        "pg_get_constraintdef(con.oid) AS definition "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s ORDER BY con.conname",
        params=[schema, table],
        force_readonly=True,
    )
    return [
        ConstraintInfo(
            name=row.cells["name"],
            type=_CONSTRAINT_TYPES.get(row.cells["type_code"], "other"),
            definition=row.cells["definition"],
        )
        for row in rows or []
    ]


async def list_foreign_keys(driver: SqlDriver, schema: str) -> list[ForeignKeyInfo]:
    """List every foreign key in a schema, resolved to columns and referenced table.

    ``from_columns`` and ``to_columns`` are aligned by ordinal position.
    """
    rows = await driver.execute_query(
        "SELECT c.conname AS name, cl.relname AS from_table, "
        "  nf.nspname AS to_schema, clf.relname AS to_table, "
        "  ("
        "    SELECT array_agg(att.attname ORDER BY u.ord) "
        "    FROM unnest(c.conkey) WITH ORDINALITY u(num, ord) "
        "    JOIN pg_attribute att ON att.attrelid = c.conrelid AND att.attnum = u.num"
        "  ) AS from_columns, "
        "  ("
        "    SELECT array_agg(att.attname ORDER BY u.ord) "
        "    FROM unnest(c.confkey) WITH ORDINALITY u(num, ord) "
        "    JOIN pg_attribute att ON att.attrelid = c.confrelid AND att.attnum = u.num"
        "  ) AS to_columns "
        "FROM pg_constraint c "
        "JOIN pg_class cl ON cl.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = cl.relnamespace "
        "JOIN pg_class clf ON clf.oid = c.confrelid "
        "JOIN pg_namespace nf ON nf.oid = clf.relnamespace "
        "WHERE c.contype = 'f' AND n.nspname = %s "
        "ORDER BY cl.relname, c.conname",
        params=[schema],
        force_readonly=True,
    )
    return [
        ForeignKeyInfo(
            name=row.cells["name"],
            from_table=row.cells["from_table"],
            from_columns=list(row.cells["from_columns"]),
            to_schema=row.cells["to_schema"],
            to_table=row.cells["to_table"],
            to_columns=list(row.cells["to_columns"]),
        )
        for row in rows or []
    ]


async def list_partitions(driver: SqlDriver, schema: str, table: str) -> PartitionSet:
    """Describe how a table is partitioned and list its partitions.

    Returns ``partitioned=False`` when the table is not a partitioned table.
    """
    rows = await driver.execute_query(
        "SELECT pt.partstrat AS strategy_code, "
        "child.relname AS partition_name, "
        "pg_get_expr(child.relpartbound, child.oid) AS bounds "
        "FROM pg_class parent "
        "JOIN pg_namespace n ON n.oid = parent.relnamespace "
        "JOIN pg_partitioned_table pt ON pt.partrelid = parent.oid "
        "LEFT JOIN pg_inherits i ON i.inhparent = parent.oid "
        "LEFT JOIN pg_class child ON child.oid = i.inhrelid "
        "WHERE n.nspname = %s AND parent.relname = %s "
        "ORDER BY child.relname",
        params=[schema, table],
        force_readonly=True,
    )
    rows = rows or []
    if not rows:
        return PartitionSet(partitioned=False, strategy=None, partitions=[])
    strategy = _PARTITION_STRATEGIES.get(rows[0].cells["strategy_code"])
    partitions = [
        PartitionInfo(name=row.cells["partition_name"], bounds=row.cells["bounds"])
        for row in rows
        if row.cells["partition_name"] is not None
    ]
    return PartitionSet(partitioned=True, strategy=strategy, partitions=partitions)


async def list_policies(driver: SqlDriver, schema: str, table: str) -> PolicySet:
    """List the Row-Level-Security policies on a table.

    Also reports whether row security is enabled on the table; policies are
    inert while it is off.
    """
    rows = await driver.execute_query(
        "SELECT c.relrowsecurity AS rls_enabled, "
        "p.policyname AS name, p.cmd AS command, p.permissive AS permissive, "
        "p.roles AS roles, p.qual AS using_expression, "
        "p.with_check AS check_expression "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_policies p ON p.schemaname = n.nspname AND p.tablename = c.relname "
        "WHERE n.nspname = %s AND c.relname = %s "
        "ORDER BY p.policyname",
        params=[schema, table],
        force_readonly=True,
    )
    rows = rows or []
    if not rows:
        return PolicySet(rls_enabled=False, policies=[])
    policies = [
        PolicyInfo(
            name=row.cells["name"],
            command=row.cells["command"],
            permissive=row.cells["permissive"] == "PERMISSIVE",
            roles=list(row.cells["roles"]),
            using_expression=row.cells["using_expression"],
            check_expression=row.cells["check_expression"],
        )
        for row in rows
        if row.cells["name"] is not None
    ]
    return PolicySet(rls_enabled=rows[0].cells["rls_enabled"], policies=policies)


async def list_roles(driver: SqlDriver, *, include_system: bool = False) -> list[RoleInfo]:
    """List the database roles and their attributes.

    PostgreSQL's own predefined roles (named ``pg_*``) are excluded unless
    ``include_system`` is set.
    """
    rows = await driver.execute_query(
        "SELECT r.rolname AS name, r.rolsuper AS superuser, "
        "r.rolcreaterole AS create_role, r.rolcreatedb AS create_db, "
        "r.rolcanlogin AS can_login, r.rolreplication AS replication, "
        "r.rolbypassrls AS bypass_rls, r.rolconnlimit AS connection_limit, "
        "COALESCE("
        "  array_agg(m.rolname ORDER BY m.rolname) FILTER (WHERE m.rolname IS NOT NULL), "
        "  '{}'"
        ") AS member_of "
        "FROM pg_roles r "
        "LEFT JOIN pg_auth_members am ON am.member = r.oid "
        "LEFT JOIN pg_roles m ON m.oid = am.roleid "
        "GROUP BY r.oid, r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb, "
        "r.rolcanlogin, r.rolreplication, r.rolbypassrls, r.rolconnlimit "
        "ORDER BY r.rolname",
        force_readonly=True,
    )
    roles = [
        RoleInfo(
            name=row.cells["name"],
            superuser=row.cells["superuser"],
            create_role=row.cells["create_role"],
            create_db=row.cells["create_db"],
            can_login=row.cells["can_login"],
            replication=row.cells["replication"],
            bypass_rls=row.cells["bypass_rls"],
            connection_limit=row.cells["connection_limit"],
            member_of=list(row.cells["member_of"]),
        )
        for row in rows or []
    ]
    if include_system:
        return roles
    return [role for role in roles if not role.name.startswith("pg_")]


async def list_grants(driver: SqlDriver, schema: str, table: str) -> list[GrantInfo]:
    """List the privileges granted on a table — who may do what to it.

    Combines the portable information_schema view (works on every PG
    version, one row per (grantee, privilege) pair) with PG 19's
    ``pg_get_acl()`` function, which returns the canonical aclitem
    string ``user=privs/grantor`` for a relation. On PG 19+ every
    returned :class:`GrantInfo` carries the same ``acl`` string
    (one ACL per relation, not per privilege); on PG ≤ 18 the field
    stays ``None``.

    The two probes are independent — a server where ``pg_get_acl``
    fails (PG ≤ 18, or PG 19 with a missing-permission error)
    silently drops the ``acl`` field. The information_schema base
    query never raises on the supported versions; if it does, the
    function returns an empty list.
    """
    rows = await driver.execute_query(
        "SELECT grantee, privilege_type AS privilege, is_grantable, grantor "
        "FROM information_schema.table_privileges "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY grantee, privilege_type",
        params=[schema, table],
        force_readonly=True,
    )

    # PG 19+ surface: pg_get_acl(classid, objid, objsubid). Returns the
    # ACL string for the table — the canonical form `\dp` displays.
    # On PG <= 18 the function doesn't exist and to_regclass /
    # pg_get_acl returns NULL or raises; we swallow both cases so the
    # information_schema rows still surface as before.
    acl_string: str | None = None
    try:
        acl_rows = await driver.execute_query(
            "SELECT pg_get_acl('pg_class'::regclass, c.oid, 0) AS acl "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            params=[schema, table],
            force_readonly=True,
        )
        if acl_rows:
            acl_string = acl_rows[0].cells.get("acl")
    except Exception:
        # PG <= 18 (function doesn't exist), or any other catalog
        # error — leave acl_string None. The information_schema rows
        # still tell the agent who has what privilege.
        acl_string = None

    return [
        GrantInfo(
            grantee=row.cells["grantee"],
            privilege=row.cells["privilege"],
            grantable=row.cells["is_grantable"] == "YES",
            grantor=row.cells["grantor"],
            acl=acl_string,
        )
        for row in rows or []
    ]


async def list_sequences(driver: SqlDriver, schema: str) -> list[SequenceInfo]:
    """List the sequences defined in a schema."""
    rows = await driver.execute_query(
        "SELECT sequencename AS name, data_type, start_value, min_value, "
        "max_value, increment_by AS increment, cycle, last_value "
        "FROM pg_sequences WHERE schemaname = %s ORDER BY sequencename",
        params=[schema],
        force_readonly=True,
    )
    return [
        SequenceInfo(
            name=row.cells["name"],
            data_type=row.cells["data_type"],
            start_value=row.cells["start_value"],
            min_value=row.cells["min_value"],
            max_value=row.cells["max_value"],
            increment=row.cells["increment"],
            cycle=row.cells["cycle"],
            last_value=row.cells["last_value"],
        )
        for row in rows or []
    ]


async def list_enums(driver: SqlDriver, schema: str) -> list[EnumInfo]:
    """List the enum types in a schema, with their labels in sort order."""
    rows = await driver.execute_query(
        "SELECT t.typname AS name, "
        "array_agg(e.enumlabel ORDER BY e.enumsortorder) AS values "
        "FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "JOIN pg_enum e ON e.enumtypid = t.oid "
        "WHERE n.nspname = %s "
        "GROUP BY t.typname ORDER BY t.typname",
        params=[schema],
        force_readonly=True,
    )
    return [EnumInfo(name=row.cells["name"], values=list(row.cells["values"])) for row in rows or []]


async def list_domains(driver: SqlDriver, schema: str) -> list[DomainInfo]:
    """List the domain types in a schema, with base type and check constraints."""
    rows = await driver.execute_query(
        "SELECT t.typname AS name, "
        "format_type(t.typbasetype, t.typtypmod) AS base_type, "
        "NOT t.typnotnull AS nullable, "
        "t.typdefault AS default_value, "
        "COALESCE("
        "  array_agg(pg_get_constraintdef(con.oid) ORDER BY con.conname) "
        "    FILTER (WHERE con.oid IS NOT NULL), "
        "  '{}'"
        ") AS constraints "
        "FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "LEFT JOIN pg_constraint con ON con.contypid = t.oid "
        "WHERE n.nspname = %s AND t.typtype = 'd' "
        "GROUP BY t.typname, t.typbasetype, t.typtypmod, t.typnotnull, t.typdefault "
        "ORDER BY t.typname",
        params=[schema],
        force_readonly=True,
    )
    return [
        DomainInfo(
            name=row.cells["name"],
            base_type=row.cells["base_type"],
            nullable=row.cells["nullable"],
            default=row.cells["default_value"],
            constraints=list(row.cells["constraints"]),
        )
        for row in rows or []
    ]


async def list_composite_types(driver: SqlDriver, schema: str) -> list[CompositeTypeInfo]:
    """List the standalone composite types in a schema.

    Implicit row-types of tables and views (which also live in ``pg_type``
    with ``typtype = 'c'``) are excluded.
    """
    rows = await driver.execute_query(
        "SELECT t.typname AS type_name, a.attname AS attr_name, "
        "format_type(a.atttypid, a.atttypmod) AS attr_type, a.attnum AS attr_num "
        "FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "JOIN pg_class c ON c.oid = t.typrelid "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
        "WHERE n.nspname = %s AND t.typtype = 'c' AND c.relkind = 'c' "
        "ORDER BY t.typname, a.attnum",
        params=[schema],
        force_readonly=True,
    )
    grouped: dict[str, list[CompositeAttribute]] = {}
    for row in rows or []:
        grouped.setdefault(row.cells["type_name"], []).append(
            CompositeAttribute(name=row.cells["attr_name"], data_type=row.cells["attr_type"])
        )
    return [CompositeTypeInfo(name=name, attributes=attrs) for name, attrs in grouped.items()]


async def list_foreign_data_wrappers(driver: SqlDriver) -> list[ForeignDataWrapperInfo]:
    """List the foreign-data wrappers installed in the database."""
    rows = await driver.execute_query(
        "SELECT fdwname AS name, "
        "  CASE WHEN fdwhandler = 0 THEN NULL ELSE fdwhandler::regproc::text END AS handler, "
        "  CASE WHEN fdwvalidator = 0 THEN NULL ELSE fdwvalidator::regproc::text END AS validator, "
        "  fdwoptions AS options "
        "FROM pg_foreign_data_wrapper ORDER BY fdwname",
        force_readonly=True,
    )
    return [
        ForeignDataWrapperInfo(
            name=row.cells["name"],
            handler=row.cells["handler"],
            validator=row.cells["validator"],
            options=_parse_options(row.cells["options"]),
        )
        for row in rows or []
    ]


async def list_foreign_servers(driver: SqlDriver) -> list[ForeignServerInfo]:
    """List the foreign servers defined in the database."""
    rows = await driver.execute_query(
        "SELECT s.srvname AS name, fdw.fdwname AS wrapper, "
        "  s.srvtype AS type, s.srvversion AS version, s.srvoptions AS options "
        "FROM pg_foreign_server s "
        "JOIN pg_foreign_data_wrapper fdw ON fdw.oid = s.srvfdw "
        "ORDER BY s.srvname",
        force_readonly=True,
    )
    return [
        ForeignServerInfo(
            name=row.cells["name"],
            wrapper=row.cells["wrapper"],
            type=row.cells["type"],
            version=row.cells["version"],
            options=_parse_options(row.cells["options"]),
        )
        for row in rows or []
    ]


async def list_foreign_tables(driver: SqlDriver, schema: str) -> list[ForeignTableInfo]:
    """List the foreign tables in a schema, with their server and options."""
    rows = await driver.execute_query(
        "SELECT c.relname AS name, s.srvname AS server, ft.ftoptions AS options "
        "FROM pg_foreign_table ft "
        "JOIN pg_class c ON c.oid = ft.ftrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_foreign_server s ON s.oid = ft.ftserver "
        "WHERE n.nspname = %s ORDER BY c.relname",
        params=[schema],
        force_readonly=True,
    )
    return [
        ForeignTableInfo(
            name=row.cells["name"],
            server=row.cells["server"],
            options=_parse_options(row.cells["options"]),
        )
        for row in rows or []
    ]


async def list_user_mappings(driver: SqlDriver) -> list[UserMappingInfo]:
    """List the role-to-foreign-server mappings defined in the database.

    The catch-all ``PUBLIC`` mapping appears with ``user = "public"``.
    """
    rows = await driver.execute_query(
        "SELECT COALESCE(usename, 'public') AS user_name, srvname AS server, "
        "  umoptions AS options "
        "FROM pg_user_mappings ORDER BY srvname, user_name",
        force_readonly=True,
    )
    return [
        UserMappingInfo(
            user=row.cells["user_name"],
            server=row.cells["server"],
            options=_parse_options(row.cells["options"]),
        )
        for row in rows or []
    ]


async def list_publications(driver: SqlDriver) -> list[PublicationInfo]:
    """List the logical-replication publications in the database."""
    rows = await driver.execute_query(
        "SELECT p.pubname AS name, r.rolname AS owner, p.puballtables AS all_tables, "
        "  p.pubinsert AS publishes_insert, p.pubupdate AS publishes_update, "
        "  p.pubdelete AS publishes_delete, p.pubtruncate AS publishes_truncate, "
        "  COALESCE("
        "    (SELECT array_agg(pt.schemaname || '.' || pt.tablename ORDER BY pt.schemaname, pt.tablename) "
        "       FROM pg_publication_tables pt WHERE pt.pubname = p.pubname), "
        "    '{}'"
        "  ) AS tables "
        "FROM pg_publication p "
        "JOIN pg_roles r ON r.oid = p.pubowner "
        "ORDER BY p.pubname",
        force_readonly=True,
    )
    return [
        PublicationInfo(
            name=row.cells["name"],
            owner=row.cells["owner"],
            all_tables=row.cells["all_tables"],
            publishes_insert=row.cells["publishes_insert"],
            publishes_update=row.cells["publishes_update"],
            publishes_delete=row.cells["publishes_delete"],
            publishes_truncate=row.cells["publishes_truncate"],
            tables=list(row.cells["tables"]),
        )
        for row in rows or []
    ]


async def list_subscriptions(driver: SqlDriver) -> list[SubscriptionInfo]:
    """List the logical-replication subscriptions in the database.

    Reading ``pg_subscription`` requires superuser privileges; with a
    non-privileged role the result will be empty even when subscriptions
    exist.
    """
    rows = await driver.execute_query(
        "SELECT s.subname AS name, r.rolname AS owner, s.subenabled AS enabled, "
        "  s.subconninfo AS connection, s.subpublications AS publications "
        "FROM pg_subscription s "
        "JOIN pg_roles r ON r.oid = s.subowner "
        "ORDER BY s.subname",
        force_readonly=True,
    )
    return [
        SubscriptionInfo(
            name=row.cells["name"],
            owner=row.cells["owner"],
            enabled=row.cells["enabled"],
            connection=row.cells["connection"],
            publications=list(row.cells["publications"]),
        )
        for row in rows or []
    ]


async def list_extensions(driver: SqlDriver) -> list[ExtensionInfo]:
    """List the extensions installed in the database."""
    rows = await driver.execute_query(
        "SELECT extname, extversion FROM pg_extension ORDER BY extname",
        force_readonly=True,
    )
    return [ExtensionInfo(name=row.cells["extname"], version=row.cells["extversion"]) for row in rows or []]


async def list_available_extensions(driver: SqlDriver) -> list[AvailableExtension]:
    """List every extension available to the database, with install status."""
    rows = await driver.execute_query(
        "SELECT name, default_version, installed_version FROM pg_available_extensions ORDER BY name",
        force_readonly=True,
    )
    return [
        AvailableExtension(
            name=row.cells["name"],
            default_version=row.cells["default_version"],
            installed_version=row.cells["installed_version"],
            installed=row.cells["installed_version"] is not None,
        )
        for row in rows or []
    ]


# --- generated columns (Phase 4.7) ---------------------------------------


@dataclass(frozen=True, slots=True)
class GeneratedColumnInfo:
    """A ``GENERATED ALWAYS AS (...) STORED`` column on a table.

    PostgreSQL today only supports ``STORED`` generated columns — the
    standard's ``VIRTUAL`` form is not implemented — so ``kind`` is
    always ``"stored"``. The field is reported anyway so callers can
    distinguish a hypothetical ``virtual`` value when PG eventually
    adds it without an interface break.
    """

    schema: str
    table: str
    column: str
    data_type: str
    expression: str
    kind: str


async def list_generated_columns(driver: SqlDriver, schema: str) -> list[GeneratedColumnInfo]:
    """List every generated column in ``schema``, sorted by table + column.

    Reads ``pg_attribute.attgenerated`` (``'s'`` for stored, ``''``
    for not generated, ``'v'`` reserved for the future virtual form)
    and pulls the expression from ``pg_attrdef`` via
    :func:`pg_get_expr`.
    """
    rows = await driver.execute_query(
        "SELECT c.relname AS table_name, "
        "       a.attname AS column_name, "
        "       format_type(a.atttypid, a.atttypmod) AS data_type, "
        "       pg_get_expr(d.adbin, d.adrelid) AS expression, "
        "       a.attgenerated AS kind "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE n.nspname = %s "
        "AND c.relkind IN ('r', 'p') "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "AND a.attgenerated <> '' "
        "ORDER BY c.relname, a.attnum",
        params=[schema],
        force_readonly=True,
    )
    kind_map = {"s": "stored", "v": "virtual"}
    return [
        GeneratedColumnInfo(
            schema=schema,
            table=str(row.cells["table_name"]),
            column=str(row.cells["column_name"]),
            data_type=str(row.cells["data_type"]),
            expression=str(row.cells["expression"]) if row.cells["expression"] is not None else "",
            kind=kind_map.get(str(row.cells["kind"]), str(row.cells["kind"])),
        )
        for row in rows or []
    ]


async def get_compact_schema(driver: SqlDriver, schema: str) -> str:
    """Return a highly condensed, token-efficient text summary of a schema.

    Collects all tables, columns, primary keys, and foreign keys in the schema
    using exactly 3 queries, then formats them into a compact notation.
    """
    tables = await list_tables(driver, schema)
    if not tables:
        return f"Schema {schema!r} contains no tables."

    # Fetch all columns for all tables in the schema in a single query
    col_rows = await driver.execute_query(
        "SELECT c.relname AS table_name, a.attname AS column_name, "
        "format_type(a.atttypid, a.atttypmod) AS data_type, "
        "NOT a.attnotnull AS nullable, t.typname AS type_name, a.atttypmod AS type_mod "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY c.relname, a.attnum",
        params=[schema],
        force_readonly=True,
    )

    columns_by_table: dict[str, list[dict[str, Any]]] = {}
    for row in col_rows or []:
        t_name = str(row.cells["table_name"])
        columns_by_table.setdefault(t_name, []).append(
            {
                "name": str(row.cells["column_name"]),
                "data_type": str(row.cells["data_type"]),
                "nullable": bool(row.cells["nullable"]),
                "vector_dimension": (
                    int(row.cells["type_mod"])
                    if str(row.cells["type_name"]) == "vector" and int(row.cells["type_mod"]) > 0
                    else None
                ),
            }
        )

    # Fetch all primary keys in the schema in a single query
    pk_rows = await driver.execute_query(
        "SELECT tc.table_name, kcu.column_name "
        "FROM information_schema.table_constraints AS tc "
        "JOIN information_schema.key_column_usage AS kcu "
        "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s "
        "ORDER BY tc.table_name, kcu.ordinal_position",
        params=[schema],
        force_readonly=True,
    )
    pks_by_table: dict[str, list[str]] = {}
    for row in pk_rows or []:
        pks_by_table.setdefault(str(row.cells["table_name"]), []).append(str(row.cells["column_name"]))

    # Fetch all foreign keys in the schema in a single query
    fks = await list_foreign_keys(driver, schema)
    fks_by_table: dict[str, dict[str, str]] = {}
    for fk in fks:
        for idx, from_col in enumerate(fk.from_columns):
            if idx < len(fk.to_columns):
                to_col = fk.to_columns[idx]
                target_ref = (
                    f"{fk.to_schema}.{fk.to_table}.{to_col}" if fk.to_schema != schema else f"{fk.to_table}.{to_col}"
                )
                fks_by_table.setdefault(fk.from_table, {})[from_col] = target_ref

    lines = []
    for table in tables:
        t_name = table.name
        # Skip partition tables if they are children to keep context compact
        if table.is_partition:
            continue

        cols = columns_by_table.get(t_name, [])
        pks = pks_by_table.get(t_name, [])
        table_fks = fks_by_table.get(t_name, {})

        parts = []
        if pks:
            pk_str = ",".join(pks)
            parts.append(f"pk:{pk_str}")

        for col in cols:
            c_name = col["name"]
            c_type = col["data_type"]
            if col["vector_dimension"] is not None:
                c_type = f"vector({col['vector_dimension']})"

            null_suffix = "?" if col["nullable"] else ""

            if c_name in table_fks:
                ref = table_fks[c_name]
                col_str = f"{c_name}:{c_type}{null_suffix}->{ref}"
            else:
                col_str = f"{c_name}:{c_type}{null_suffix}"

            parts.append(col_str)

        line = f"[{t_name}] " + " | ".join(parts)
        lines.append(line)

    return "\n".join(lines)
