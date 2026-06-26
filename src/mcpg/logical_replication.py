"""Logical replication management writes.

Companion to the existing read surface (``list_publications`` and
``list_subscriptions`` in :mod:`mcpg.introspection`) — this module
exposes the four DDL-gated write tools that close the loop on
logical-replication ops:

* ``create_publication(name, all_tables=False, tables=(...))``
* ``drop_publication(name, if_exists=False, cascade=False)``
* ``create_subscription(name, connection_string, publications,
  enabled=True, copy_data=True, create_slot=True, slot_name=None,
  synchronous_commit=None)``
* ``drop_subscription(name, if_exists=False)``

All four require ``MCPG_ACCESS_MODE=unrestricted`` and
``MCPG_ALLOW_DDL=true`` — they're plain DDL underneath. They live
here rather than in :mod:`mcpg.write` because the publication /
subscription DDL has its own grammar (FOR ALL TABLES, WITH (slot_name
= …), etc.) that doesn't compose with the generic ``run_ddl`` path.

Security
========

* Publication / subscription names are validated against an unquoted
  PostgreSQL identifier regex (``[A-Za-z_][A-Za-z0-9_]*``); anything
  else is rejected up-front so an identifier can never reach SQL with
  unsafe characters.
* Schema- and table-qualified names in the ``tables`` list are split
  on ``.``, each piece validated separately, then quoted via the same
  ``_pg_quote_ident`` helper the rest of the codebase uses.
* The connection string parameter on ``create_subscription`` is
  passed through with single-quote doubling — Postgres doesn't have a
  ``format('%L')`` analogue for arbitrary text inside DDL, so we
  implement the same escape it would produce.

Convention
==========

Each tool returns a frozen ``*Result`` dataclass with:

  - ``name`` — the publication / subscription touched
  - ``executed_sql`` — the rendered SQL that ran (auditable)
  - ``detail`` — a human-readable summary

Errors raise ``LogicalReplicationError`` rather than the generic
``RuntimeError`` so callers can branch on the failure type cleanly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcpg.database import Database

# Unquoted PostgreSQL identifier: starts with letter / underscore,
# then letters / digits / underscores. Same shape as
# `mcpg.pg_search._IDENTIFIER`; duplicated here to keep modules
# independent rather than importing across feature boundaries.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class LogicalReplicationError(Exception):
    """Raised when a logical-replication write is rejected or fails."""


def _validate_identifier(name: str, kind: str) -> None:
    if not _IDENTIFIER.match(name):
        raise LogicalReplicationError(f"invalid {kind} name: {name!r}")


def _pg_quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier the way ``format('%I')`` would."""
    return '"' + name.replace('"', '""') + '"'


def _pg_quote_literal(value: str) -> str:
    """Quote a PostgreSQL string literal the way ``format('%L')`` would.

    Doubles embedded single quotes; wraps the result in single quotes.
    Used for the libpq ``connection_string`` on
    ``CREATE SUBSCRIPTION`` — which has no parameter-bind slot
    available on the DDL.
    """
    return "'" + value.replace("'", "''") + "'"


def _split_qualified(qname: str) -> tuple[str, str]:
    """Split a schema-qualified ``"schema.table"`` and validate each piece."""
    if "." not in qname:
        raise LogicalReplicationError(f"qualified table name must be 'schema.table': {qname!r}")
    schema, _, table = qname.partition(".")
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    return schema, table


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreatePublicationResult:
    """Outcome of :func:`create_publication`."""

    name: str
    all_tables: bool
    tables: list[str]
    executed_sql: str
    detail: str


@dataclass(frozen=True)
class DropPublicationResult:
    """Outcome of :func:`drop_publication`."""

    name: str
    if_exists: bool
    cascade: bool
    executed_sql: str
    detail: str


_CONN_LITERAL = re.compile(r"CONNECTION '(?:''|[^'])*'")


@dataclass(frozen=True)
class CreateSubscriptionResult:
    """Outcome of :func:`create_subscription`.

    ``connection_string`` is **not** echoed back — it can contain
    embedded credentials. The raw DSN is still available on
    ``executed_sql`` for audit, but ``repr()`` redacts the
    ``CONNECTION '…'`` literal so DSNs don't leak into logs or
    accidental ``print(result)`` output.
    """

    name: str
    publications: list[str]
    enabled: bool
    copy_data: bool
    create_slot: bool
    slot_name: str | None
    executed_sql: str
    detail: str

    def __repr__(self) -> str:
        redacted_sql = _CONN_LITERAL.sub("CONNECTION '<redacted>'", self.executed_sql)
        return (
            f"CreateSubscriptionResult(name={self.name!r}, "
            f"publications={self.publications!r}, enabled={self.enabled!r}, "
            f"copy_data={self.copy_data!r}, create_slot={self.create_slot!r}, "
            f"slot_name={self.slot_name!r}, executed_sql={redacted_sql!r}, "
            f"detail={self.detail!r})"
        )


@dataclass(frozen=True)
class DropSubscriptionResult:
    """Outcome of :func:`drop_subscription`."""

    name: str
    if_exists: bool
    executed_sql: str
    detail: str


# ---------------------------------------------------------------------------
# create_publication
# ---------------------------------------------------------------------------


async def create_publication(
    database: Database,
    *,
    name: str,
    all_tables: bool = False,
    tables: tuple[str, ...] = (),
) -> CreatePublicationResult:
    """Create a logical-replication publication.

    Exactly one of ``all_tables=True`` or a non-empty ``tables`` list
    must be supplied — the empty publication (no FOR clause) is legal
    SQL but not useful for an operator-driven workflow and would
    silently do nothing.

    ``tables`` accepts ``"schema.table"`` strings; each is validated +
    quoted before reaching SQL.
    """
    _validate_identifier(name, "publication")
    if all_tables and tables:
        raise LogicalReplicationError("specify either all_tables=True OR tables=..., not both")
    if not all_tables and not tables:
        raise LogicalReplicationError("must specify all_tables=True or a non-empty tables tuple")

    quoted_name = _pg_quote_ident(name)
    if all_tables:
        sql = f"CREATE PUBLICATION {quoted_name} FOR ALL TABLES"
        table_list: list[str] = []
    else:
        quoted_tables: list[str] = []
        for qname in tables:
            schema, table = _split_qualified(qname)
            quoted_tables.append(f"{_pg_quote_ident(schema)}.{_pg_quote_ident(table)}")
        sql = f"CREATE PUBLICATION {quoted_name} FOR TABLE {', '.join(quoted_tables)}"
        table_list = list(tables)

    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        raise LogicalReplicationError(f"create publication failed: {exc}") from exc

    detail = (
        f"Publication {name!r} created"
        + (" for all tables" if all_tables else f" for {len(table_list)} table(s)")
        + "."
    )
    return CreatePublicationResult(
        name=name,
        all_tables=all_tables,
        tables=table_list,
        executed_sql=sql,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# drop_publication
# ---------------------------------------------------------------------------


async def drop_publication(
    database: Database,
    *,
    name: str,
    if_exists: bool = False,
    cascade: bool = False,
) -> DropPublicationResult:
    """Drop a logical-replication publication.

    ``if_exists=True`` suppresses the error if the publication is
    already gone (matches the SQL keyword). ``cascade=True`` lets the
    drop cascade through dependent objects — rarely needed for
    publications since they don't usually have downstream catalog
    dependencies, but included for symmetry with DROP DDL elsewhere.
    """
    _validate_identifier(name, "publication")
    quoted_name = _pg_quote_ident(name)
    parts = ["DROP PUBLICATION"]
    if if_exists:
        parts.append("IF EXISTS")
    parts.append(quoted_name)
    if cascade:
        parts.append("CASCADE")
    sql = " ".join(parts)
    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        raise LogicalReplicationError(f"drop publication failed: {exc}") from exc
    return DropPublicationResult(
        name=name,
        if_exists=if_exists,
        cascade=cascade,
        executed_sql=sql,
        detail=f"Publication {name!r} dropped.",
    )


# ---------------------------------------------------------------------------
# create_subscription
# ---------------------------------------------------------------------------


async def create_subscription(
    database: Database,
    *,
    name: str,
    connection_string: str,
    publications: tuple[str, ...],
    enabled: bool = True,
    copy_data: bool = True,
    create_slot: bool = True,
    slot_name: str | None = None,
    synchronous_commit: str | None = None,
) -> CreateSubscriptionResult:
    """Create a logical-replication subscription.

    Args mirror the SQL surface:

    * ``connection_string`` — the libpq DSN of the publisher cluster.
      Passed through with single-quote doubling (no parameter-bind
      slot is available on the DDL).
    * ``publications`` — at least one publication name (each
      identifier-validated).
    * ``enabled`` — start the subscription immediately. ``False``
      creates it but leaves replication paused.
    * ``copy_data`` — request initial table sync. ``False`` skips the
      initial COPY and only replicates writes that arrive after the
      subscription starts.
    * ``create_slot`` — let the subscriber create its replication
      slot on the publisher. Set ``False`` when reusing a pre-existing
      slot (operator-managed).
    * ``slot_name`` — override the default ``slot_name = <name>``.
      Identifier-validated.
    * ``synchronous_commit`` — optional override for the
      subscription's ``synchronous_commit`` GUC. One of ``'on'``,
      ``'off'``, ``'local'``, ``'remote_write'``, ``'remote_apply'``
      (validated).
    """
    _validate_identifier(name, "subscription")
    if not publications:
        raise LogicalReplicationError("at least one publication is required")
    for pub in publications:
        _validate_identifier(pub, "publication")
    if slot_name is not None:
        _validate_identifier(slot_name, "slot")
    if synchronous_commit is not None and synchronous_commit not in {
        "on",
        "off",
        "local",
        "remote_write",
        "remote_apply",
    }:
        raise LogicalReplicationError(
            f"invalid synchronous_commit: {synchronous_commit!r} (allowed: on, off, local, remote_write, remote_apply)"
        )

    quoted_name = _pg_quote_ident(name)
    quoted_publications = ", ".join(_pg_quote_ident(p) for p in publications)
    quoted_conn = _pg_quote_literal(connection_string)

    options: list[str] = []
    if not enabled:
        options.append("enabled = false")
    if not copy_data:
        options.append("copy_data = false")
    if not create_slot:
        options.append("create_slot = false")
    if slot_name is not None:
        # CREATE SUBSCRIPTION WITH (slot_name = ...) takes a string
        # literal, not a SQL identifier (per PG docs). Quoting it as
        # an identifier (`"my_slot"`) trips a parse error at the
        # subscription-options DefElem walker.
        options.append(f"slot_name = {_pg_quote_literal(slot_name)}")
    if synchronous_commit is not None:
        options.append(f"synchronous_commit = '{synchronous_commit}'")

    sql = f"CREATE SUBSCRIPTION {quoted_name} CONNECTION {quoted_conn} PUBLICATION {quoted_publications}"
    if options:
        sql += " WITH (" + ", ".join(options) + ")"

    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        # psycopg's error message frequently echoes the failing SQL,
        # which on CREATE SUBSCRIPTION includes the DSN — strip the
        # CONNECTION literal before it can leak into logs or back to
        # the caller. Same redaction the result's __repr__ uses.
        redacted = _CONN_LITERAL.sub("CONNECTION '<redacted>'", str(exc))
        raise LogicalReplicationError(f"create subscription failed: {redacted}") from exc

    detail = (
        f"Subscription {name!r} created against {len(publications)} "
        f"publication(s); enabled={enabled}, copy_data={copy_data}."
    )
    return CreateSubscriptionResult(
        name=name,
        publications=list(publications),
        enabled=enabled,
        copy_data=copy_data,
        create_slot=create_slot,
        slot_name=slot_name,
        executed_sql=sql,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# drop_subscription
# ---------------------------------------------------------------------------


async def drop_subscription(
    database: Database,
    *,
    name: str,
    if_exists: bool = False,
) -> DropSubscriptionResult:
    """Drop a logical-replication subscription.

    Postgres requires the subscription to be disabled first (or the
    replication slot dropped on the publisher) — this tool does NOT
    auto-disable; the caller should pair with ``ALTER SUBSCRIPTION
    … DISABLE`` (via ``run_ddl``) when needed. Returning the explicit
    DDL failure surface lets the operator decide whether to disable
    or wait for the slot to drain.
    """
    _validate_identifier(name, "subscription")
    quoted_name = _pg_quote_ident(name)
    parts = ["DROP SUBSCRIPTION"]
    if if_exists:
        parts.append("IF EXISTS")
    parts.append(quoted_name)
    sql = " ".join(parts)
    try:
        await database.run_unmanaged(sql)
    except Exception as exc:
        raise LogicalReplicationError(f"drop subscription failed: {exc}") from exc
    return DropSubscriptionResult(
        name=name,
        if_exists=if_exists,
        executed_sql=sql,
        detail=f"Subscription {name!r} dropped.",
    )


__all__ = [
    "CreatePublicationResult",
    "CreateSubscriptionResult",
    "DropPublicationResult",
    "DropSubscriptionResult",
    "LogicalReplicationError",
    "create_publication",
    "create_subscription",
    "drop_publication",
    "drop_subscription",
]
