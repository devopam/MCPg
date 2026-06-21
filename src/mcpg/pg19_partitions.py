"""PG 19 partition reorganisation — ``MERGE PARTITIONS`` + ``SPLIT PARTITION``.

PG 19 ships two ``ALTER TABLE`` extensions that finally make partition
boundaries reshapable without dumping and reloading data:

* ``ALTER TABLE parent MERGE PARTITIONS (p1, p2, …) INTO new_partition``
  — consolidates two or more existing partitions into a new partition
  whose bounds span the merged range / list. Replaces the
  hand-rolled "create new, copy, drop old, swap" dance.
* ``ALTER TABLE parent SPLIT PARTITION existing_partition INTO (…)``
  — splits a single partition into two or more with caller-defined
  bounds. Useful for hot-partition splits during traffic growth.

This module provides three tools:

* ``get_pg19_partitions_status`` — version probe; never raises. Reports
  whether MERGE / SPLIT PARTITION are usable on this server.
* ``merge_partitions`` — runs the MERGE PARTITIONS form. All inputs
  are identifiers; the rendered SQL has no caller-supplied expression
  slots.
* ``split_partition`` — runs the SPLIT PARTITION form. Caller supplies
  per-new-partition specs (``name`` identifier + ``for_values_clause``
  expression fragment); we quote the identifier and embed the
  expression verbatim (see *Security posture* below).

Backward compatibility
----------------------
Additive. PG ≤ 18 operators continue using the long-standing
detach / create / attach dance — both write surfaces raise
``Pg19PartitionsError`` on older servers with a guidance string.
``get_pg19_partitions_status`` returns ``available=False`` with the
same pointer.

Integrates with the existing ``mcpg.partman`` lifecycle: the
partman_* tools manage rolling-window partition creation; these
PG 19 tools handle the rarer "reshape an existing range" case
without coupling.

Security posture
----------------
* Schema, table, source partition, and new-partition identifier names
  are validated through ``_quote_identifier`` — embedded double-quotes
  are escaped; NUL bytes / empty strings rejected.
* The ``for_values_clause`` fragment on ``split_partition`` is
  embedded verbatim. SPLIT PARTITION can't accept parameter-bound
  expressions (DDL grammar), so the caller is responsible for
  composing a safe fragment from validated values. The MCP tool sits
  behind the ``Capability.DDL`` + ``allow_ddl`` gate; the same trust
  model as ``run_ddl``. Each helper raises ``Pg19PartitionsError``
  with the rendered SQL in the message on failure, so an operator
  can replay or roll back.
* All writes dispatch through :meth:`Database.run_unmanaged` because
  the partition reorganisation operations cannot run inside a
  transaction block.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver
from mcpg.database import Database

# PG 19 ships both forms. The version-num boundary.
_MIN_PG19_PARTITIONS_VERSION = 190000


class Pg19PartitionsError(Exception):
    """Raised when a PG 19 partition reorganisation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pg19PartitionsStatus:
    """Reports whether PG 19 partition reorganisation is usable.

    ``available`` is True when ``server_version_num`` >= 190000.
    ``detail`` is a guidance string suitable for surfacing to an LLM
    agent — on PG ≤ 18 it points at the detach / create / attach
    fallback.
    """

    available: bool
    server_version_num: int
    server_version: str
    detail: str


@dataclass(frozen=True, slots=True)
class MergePartitionsResult:
    """Outcome of `merge_partitions`.

    ``merge_sql`` is the rendered DDL that actually executed — same
    audit-friendly shape as ``maintenance_sql`` / ``repack_sql`` on
    sibling tools.
    """

    parent_schema: str
    parent_table: str
    source_partitions: tuple[str, ...]
    target_partition: str
    merge_sql: str


@dataclass(frozen=True, slots=True)
class SplitPartitionSpec:
    """One new-partition spec for `split_partition`.

    ``name`` is the new partition identifier (we quote it).
    ``for_values_clause`` is the verbatim text that follows
    ``FOR VALUES`` in the rendered SQL — caller supplies it because
    the syntax varies by partition kind (RANGE / LIST / HASH).
    Example RANGE form: ``"FROM ('2024-01-01') TO ('2024-04-01')"``.
    Example LIST form: ``"IN ('north', 'south')"``.
    """

    name: str
    for_values_clause: str


@dataclass(frozen=True, slots=True)
class SplitPartitionResult:
    """Outcome of `split_partition`."""

    parent_schema: str
    parent_table: str
    source_partition: str
    new_partitions: tuple[str, ...]
    split_sql: str


# ---------------------------------------------------------------------------
# Shared probes
# ---------------------------------------------------------------------------


def _quote_identifier(name: str) -> str:
    """Quote a SQL identifier, escaping embedded double-quotes."""
    if not name or "\x00" in name:
        raise Pg19PartitionsError(f"invalid identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


async def _server_version(driver: SqlDriver) -> tuple[int, str]:
    """Return ``(server_version_num, server_version)`` in one round trip."""
    rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::int AS ver_num, current_setting('server_version') AS ver",
        force_readonly=True,
    )
    if not rows:
        return 0, ""
    cells = rows[0].cells
    return int(cells.get("ver_num") or 0), str(cells.get("ver") or "")


# ---------------------------------------------------------------------------
# Status probe
# ---------------------------------------------------------------------------


async def get_pg19_partitions_status(driver: SqlDriver) -> Pg19PartitionsStatus:
    """Report whether PG 19's MERGE / SPLIT PARTITION forms are usable.

    Read-only; never raises. On PG ≤ 18 returns ``available=False``
    with a diagnostic pointing at the detach / create / attach
    fallback path.
    """
    try:
        ver_num, ver = await _server_version(driver)
    except Exception as exc:
        return Pg19PartitionsStatus(
            available=False,
            server_version_num=0,
            server_version="",
            detail=(
                f"PG 19 partition reorganisation unavailable (version probe failed: {exc}). "
                "Re-run after the server is back online."
            ),
        )
    available = ver_num >= _MIN_PG19_PARTITIONS_VERSION
    if available:
        detail = (
            "PG 19 MERGE PARTITIONS / SPLIT PARTITION are available. "
            "Use merge_partitions() to consolidate adjacent partitions or "
            "split_partition() to reshape a hot partition without rewriting data."
        )
    else:
        detail = (
            "MERGE PARTITIONS / SPLIT PARTITION require PostgreSQL 19 or newer; "
            "this server is older. Fall back to the detach / create / attach dance: "
            "DETACH the source partition(s), CREATE the replacement(s), INSERT to "
            "redistribute data, then ATTACH with the new bounds."
        )
    return Pg19PartitionsStatus(
        available=available,
        server_version_num=ver_num,
        server_version=ver,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# MERGE PARTITIONS
# ---------------------------------------------------------------------------


async def merge_partitions(
    database: Database,
    *,
    parent_schema: str,
    parent_table: str,
    source_partitions: list[str],
    target_partition_name: str,
) -> MergePartitionsResult:
    """Consolidate two or more partitions into a single new partition.

    Issues ``ALTER TABLE schema.parent MERGE PARTITIONS (p1, p2, …)
    INTO new_partition``. PG 19's MERGE PARTITIONS reuses the existing
    partition data files; no row-by-row copy. The merged partition's
    bounds span the union of the source bounds (PG validates contiguity
    — non-adjacent ranges are rejected by the server).

    Cannot run inside a transaction block; dispatches through
    :meth:`Database.run_unmanaged`. Requires PG 19+; raises
    :class:`Pg19PartitionsError` on older versions.
    """
    if len(source_partitions) < 2:
        raise Pg19PartitionsError(
            f"MERGE PARTITIONS requires at least two source partitions; got {len(source_partitions)}."
        )
    driver = database.driver()
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_PG19_PARTITIONS_VERSION:
        raise Pg19PartitionsError(
            "MERGE PARTITIONS requires PostgreSQL 19 or newer; this server "
            f"reports {ver or 'unknown'} (server_version_num={ver_num}). "
            "Use the detach / create / attach fallback during a maintenance window."
        )
    qualified_parent = f"{_quote_identifier(parent_schema)}.{_quote_identifier(parent_table)}"
    quoted_sources = ", ".join(_quote_identifier(p) for p in source_partitions)
    quoted_target = _quote_identifier(target_partition_name)
    merge_sql = f"ALTER TABLE {qualified_parent} MERGE PARTITIONS ({quoted_sources}) INTO {quoted_target}"
    try:
        await database.run_unmanaged(merge_sql)
    except Exception as exc:
        raise Pg19PartitionsError(f"MERGE PARTITIONS failed: {exc}") from exc
    return MergePartitionsResult(
        parent_schema=parent_schema,
        parent_table=parent_table,
        source_partitions=tuple(source_partitions),
        target_partition=target_partition_name,
        merge_sql=merge_sql,
    )


# ---------------------------------------------------------------------------
# SPLIT PARTITION
# ---------------------------------------------------------------------------


async def split_partition(
    database: Database,
    *,
    parent_schema: str,
    parent_table: str,
    source_partition: str,
    new_partitions: list[SplitPartitionSpec],
) -> SplitPartitionResult:
    """Split one partition into two or more new partitions.

    Issues ``ALTER TABLE schema.parent SPLIT PARTITION existing INTO
    (PARTITION new1 FOR VALUES …, PARTITION new2 FOR VALUES …)``.

    ``new_partitions`` is a list of :class:`SplitPartitionSpec`. The
    ``name`` of each spec is quoted as an identifier; the
    ``for_values_clause`` is embedded verbatim because PG's DDL grammar
    doesn't accept parameter-bound bounds expressions. Caller is
    responsible for composing safe ``FOR VALUES`` fragments from
    validated values — see the module docstring.

    Cannot run inside a transaction block; dispatches through
    :meth:`Database.run_unmanaged`. Requires PG 19+; raises
    :class:`Pg19PartitionsError` on older versions.
    """
    if len(new_partitions) < 2:
        raise Pg19PartitionsError(f"SPLIT PARTITION requires at least two new partitions; got {len(new_partitions)}.")
    driver = database.driver()
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_PG19_PARTITIONS_VERSION:
        raise Pg19PartitionsError(
            "SPLIT PARTITION requires PostgreSQL 19 or newer; this server "
            f"reports {ver or 'unknown'} (server_version_num={ver_num}). "
            "Use the detach / create / attach fallback during a maintenance window."
        )
    qualified_parent = f"{_quote_identifier(parent_schema)}.{_quote_identifier(parent_table)}"
    quoted_source = _quote_identifier(source_partition)
    # Defensive: PG-libpq truncates queries at NUL bytes — a caller-supplied
    # for_values_clause carrying \x00 could silently chop off the trailing
    # partitions and emit a partial DDL. Reject early so the failure is
    # explicit (gemini-review critical on PR #145).
    for spec in new_partitions:
        if "\x00" in spec.for_values_clause:
            raise Pg19PartitionsError(
                f"invalid for_values_clause for partition {spec.name!r}: contains NUL byte"
            )
    new_parts_sql = ", ".join(
        f"PARTITION {_quote_identifier(spec.name)} FOR VALUES {spec.for_values_clause}" for spec in new_partitions
    )
    split_sql = f"ALTER TABLE {qualified_parent} SPLIT PARTITION {quoted_source} INTO ({new_parts_sql})"
    try:
        await database.run_unmanaged(split_sql)
    except Exception as exc:
        raise Pg19PartitionsError(f"SPLIT PARTITION failed: {exc}") from exc
    return SplitPartitionResult(
        parent_schema=parent_schema,
        parent_table=parent_table,
        source_partition=source_partition,
        new_partitions=tuple(spec.name for spec in new_partitions),
        split_sql=split_sql,
    )


__all__ = [
    "MergePartitionsResult",
    "Pg19PartitionsError",
    "Pg19PartitionsStatus",
    "SplitPartitionResult",
    "SplitPartitionSpec",
    "get_pg19_partitions_status",
    "merge_partitions",
    "split_partition",
]
