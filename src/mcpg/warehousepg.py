"""WarehousePG (Greenplum-derived MPP) integration.

`WarehousePG <https://github.com/WarehousePG/warehousepg>`_ is the
community-maintained fork of Greenplum that picked up the mantle
after Broadcom's licensing change. It's an MPP analytical database
built on top of PostgreSQL — distributed coordinator + segments,
append-optimized + column-oriented tables, ``DISTRIBUTED BY`` clause,
resource groups, ``gp_*`` catalog surface.

Since WarehousePG is wire-compatible with libpq and the core SQL
surface is PostgreSQL, most MCPg tools already work. This module
provides the **gating status probe** for the MPP-specific surface:
``get_warehousepg_status``. Every subsequent ``mcpg.warehousepg.*``
tool advertises its readiness via this probe, mirroring the pattern
established by ``mcpg.pgq.get_pgq_status`` and ``mcpg.pg19_runtime
.get_logical_replication_status``.

Detection strategy
==================

WarehousePG identifies itself in two complementary ways:

1. **Version string** — ``SELECT version()`` returns something like
   ``PostgreSQL 14.4 (WarehousePG 7.2.0 build commit:...)``. The
   ``WarehousePG`` / ``Greenplum`` substring is the canonical
   signal; we honour both for forward compatibility with any
   downstream rebrand.

2. **Catalog presence** — the MPP coordinator exposes
   ``gp_segment_configuration``, a view describing each primary +
   mirror segment. Vanilla PostgreSQL doesn't have this. Probing it
   via ``to_regclass()`` is cheaper than running an actual SELECT
   and works even when the audit role lacks segment-level grants.

Both signals must agree before we report ``available=True``: an
operator who's set up a vanilla-PG cluster with a hand-rolled
``gp_segment_configuration`` view shouldn't be misclassified as MPP.

Returned payload
================

When ``available=True``, the probe additionally surfaces:

- ``coordinator_role`` — the coordinator's role label
  (``coordinator`` on modern WarehousePG; ``master`` on legacy
  Greenplum versions). Lets agents differentiate vintage without
  forcing them to parse the version string.
- ``segment_count`` — total primary segments (excludes mirrors and
  the coordinator). The single most-asked-for number for capacity
  planning.
- ``mirroring`` — ``True`` when at least one mirror segment exists,
  ``False`` on an unmirrored cluster (common in dev / test).

Security
========

Pure read-only catalog joins. No caller-supplied identifiers in
identifier slots. Driver errors surface as ``available=False`` with
the actual error message in ``detail`` — same convention as
``mcpg.health`` and the other status probes.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver


@dataclass(frozen=True)
class WarehousePGStatus:
    """Reports whether the connected server is a WarehousePG cluster.

    ``available`` is the gating bit for every other ``mcpg.warehousepg.*``
    tool — when ``False``, those tools advertise themselves as inert
    rather than raising on a vanilla PG cluster.

    ``version`` carries the full ``SELECT version()`` string verbatim
    so agents can pin behaviour to specific WarehousePG releases
    without re-querying. ``coordinator_role`` / ``segment_count`` /
    ``mirroring`` populate only when ``available`` is ``True``.
    """

    available: bool
    version: str
    coordinator_role: str | None
    segment_count: int | None
    mirroring: bool | None
    detail: str


# Substring matches for the MPP variant. Both forms surface in the
# wild — WarehousePG keeps the Greenplum compatibility identifier in
# many versions for tooling compatibility.
_MPP_VERSION_MARKERS: tuple[str, ...] = ("warehousepg", "greenplum")


async def get_warehousepg_status(driver: SqlDriver) -> WarehousePGStatus:
    """Probe the connected server for WarehousePG / MPP signature.

    Read-only; never raises. Returns ``available=False`` on every
    error path so callers can branch on the bit without try/except
    boilerplate. Carries a diagnostic in ``detail`` for the cases an
    agent should surface to the user.
    """
    version = ""
    try:
        rows = await driver.execute_query(
            "SELECT version() AS version",
            force_readonly=True,
        )
        if rows:
            version = str(rows[0].cells.get("version") or "")
    except Exception as exc:
        return WarehousePGStatus(
            available=False,
            version="",
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=f"WarehousePG status probe failed (version): {exc}",
        )

    if not any(marker in version.lower() for marker in _MPP_VERSION_MARKERS):
        return WarehousePGStatus(
            available=False,
            version=version,
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=(
                "Not a WarehousePG / Greenplum cluster — `SELECT version()` "
                "doesn't mention either. The mcpg.warehousepg tools are inert "
                "on this server; the core MCPg tools work as usual."
            ),
        )

    # Second confirmation — catalog presence. An operator could in
    # principle put 'WarehousePG' in their version string without
    # being on the real product; the gp_segment_configuration view
    # is the canonical MPP signal.
    try:
        catalog_rows = await driver.execute_query(
            "SELECT to_regclass('pg_catalog.gp_segment_configuration')::text AS oid",
            force_readonly=True,
        )
    except Exception as exc:
        return WarehousePGStatus(
            available=False,
            version=version,
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=(f"WarehousePG status probe failed (catalog probe): {exc}"),
        )
    has_seg_view = bool(catalog_rows and catalog_rows[0].cells.get("oid"))
    if not has_seg_view:
        return WarehousePGStatus(
            available=False,
            version=version,
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=(
                "Version string mentions WarehousePG / Greenplum but "
                "`gp_segment_configuration` view is missing — this looks "
                "like a vanilla PostgreSQL cluster with an unusual version "
                "banner. mcpg.warehousepg tools stay inert."
            ),
        )

    # Both signals positive — we're on a real MPP. Surface coordinator
    # role + segment counts. role='c' is the modern coordinator label;
    # role='p' / 'm' are primary / mirror segments.
    coordinator_role: str | None = None
    segment_count: int | None = None
    mirroring: bool | None = None
    try:
        seg_rows = await driver.execute_query(
            "SELECT "
            "  (SELECT role FROM gp_segment_configuration "
            "    WHERE content = -1 LIMIT 1) AS coordinator_role, "
            "  (SELECT count(*)::int FROM gp_segment_configuration "
            "    WHERE role = 'p' AND content >= 0) AS primary_count, "
            "  (SELECT count(*)::int FROM gp_segment_configuration "
            "    WHERE role = 'm' AND content >= 0) AS mirror_count",
            force_readonly=True,
        )
        if seg_rows:
            cells = seg_rows[0].cells
            raw_role = cells.get("coordinator_role")
            # Legacy Greenplum labels coordinator as 'master' ('m' on
            # primaries vs the literal 'master' string on the
            # coordinator — they don't collide); modern WarehousePG
            # uses 'coordinator'. Normalise to the human-readable form.
            if raw_role == "p":
                # On some versions the coordinator's role column also
                # carries 'p' (primary); content=-1 still distinguishes
                # it. Translate to the human form.
                coordinator_role = "coordinator"
            elif raw_role is not None:
                coordinator_role = str(raw_role)
            primary_count = cells.get("primary_count")
            mirror_count = cells.get("mirror_count")
            if primary_count is not None:
                segment_count = int(primary_count)
            if mirror_count is not None:
                mirroring = int(mirror_count) > 0
    except Exception as exc:
        # Segment metadata probe failed but we already confirmed MPP —
        # report available=True with the metadata fields None plus
        # the diagnostic. Avoids the awkward "version says WarehousePG,
        # but the gating bit flips to False because we couldn't count
        # segments" trap.
        return WarehousePGStatus(
            available=True,
            version=version,
            coordinator_role=None,
            segment_count=None,
            mirroring=None,
            detail=(
                f"WarehousePG detected, but segment metadata probe failed "
                f"({exc}). Check that the audit role has SELECT on "
                "`gp_segment_configuration` for full status reporting."
            ),
        )

    return WarehousePGStatus(
        available=True,
        version=version,
        coordinator_role=coordinator_role,
        segment_count=segment_count,
        mirroring=mirroring,
        detail=(
            f"WarehousePG cluster detected: {segment_count} primary "
            f"segment(s), mirroring "
            f"{'enabled' if mirroring else 'disabled'}. "
            "`mcpg.warehousepg.*` tools are active on this server."
        ),
    )


# ---------------------------------------------------------------------------
# 15.2 — list_distribution_policies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistributionPolicy:
    """How one table's data is distributed across the MPP segments.

    ``method`` is one of ``HASH``, ``RANDOM``, or ``REPLICATED`` —
    the three policies WarehousePG supports. ``distribution_columns``
    is empty for RANDOM / REPLICATED tables. ``num_segments`` is the
    table-level segment count (0 means "default, all segments").
    """

    schema: str
    table: str
    method: str
    distribution_columns: list[str]
    num_segments: int


@dataclass(frozen=True)
class DistributionPolicyReport:
    """Aggregate result of :func:`list_distribution_policies`.

    ``available=False`` on a vanilla PG cluster (returned without
    running any catalog query — gated by the 15.1 probe).
    """

    available: bool
    policies: list[DistributionPolicy]
    detail: str


# `gp_distribution_policy.policytype` codes — single-letter encoding
# in modern WarehousePG releases. Pre-7.x stored the method as a more
# verbose string; the COALESCE in the query handles both shapes.
_POLICY_TYPE_NAMES = {"p": "HASH", "r": "REPLICATED", "n": "RANDOM"}


async def list_distribution_policies(driver: SqlDriver, schema: str | None = None) -> DistributionPolicyReport:
    """List distribution policies for tables in ``schema``.

    Joins ``gp_distribution_policy`` (one row per distributed table)
    to ``pg_class`` + ``pg_namespace`` for the schema-qualified name
    and ``pg_attribute`` for the distribution-key column names. The
    column-name lookup uses array agg ordered by the position in the
    catalog's ``distkey`` int2vector so a composite hash distribution
    surfaces in the right column order.

    ``schema`` is optional — when ``None`` returns policies for every
    non-system schema; when supplied (typical) restricts the report
    to that schema. The schema name is parameter-bound, never
    interpolated into SQL.
    """
    status = await get_warehousepg_status(driver)
    if not status.available:
        return DistributionPolicyReport(
            available=False,
            policies=[],
            detail=(
                "Distribution policies are only available on WarehousePG / Greenplum MPP clusters. " + status.detail
            ),
        )

    where_clause = "n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
    params: list[str] = []
    if schema is not None:
        where_clause = "n.nspname = %s"
        params.append(schema)

    query = (
        "SELECT n.nspname AS schema, c.relname AS table_name, "
        "       d.policytype AS policy_type, "
        "       COALESCE(d.numsegments, 0)::int AS num_segments, "
        "       COALESCE("
        "         ARRAY(SELECT a.attname FROM unnest(d.distkey::int[]) WITH ORDINALITY AS k(att, ord) "
        "               JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.att "
        "               ORDER BY k.ord), "
        "         '{}'::text[]"
        "       ) AS distribution_columns "
        "FROM gp_distribution_policy d "
        "JOIN pg_class c ON c.oid = d.localoid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE {where_clause} "
        "ORDER BY n.nspname, c.relname"
    )
    try:
        rows = await driver.execute_query(query, params=params, force_readonly=True)
    except Exception as exc:
        return DistributionPolicyReport(
            available=False,
            policies=[],
            detail=f"Distribution policy probe failed: {exc}",
        )

    policies: list[DistributionPolicy] = []
    for row in rows or []:
        cells = row.cells
        policy_code = str(cells.get("policy_type") or "")
        method = _POLICY_TYPE_NAMES.get(policy_code, policy_code.upper() or "UNKNOWN")
        cols = cells.get("distribution_columns") or []
        policies.append(
            DistributionPolicy(
                schema=str(cells.get("schema") or ""),
                table=str(cells.get("table_name") or ""),
                method=method,
                distribution_columns=[str(c) for c in cols],
                num_segments=int(cells.get("num_segments") or 0),
            )
        )

    return DistributionPolicyReport(
        available=True,
        policies=policies,
        detail=(f"{len(policies)} distributed table(s) found" + (f" in schema '{schema}'" if schema else "") + "."),
    )


# ---------------------------------------------------------------------------
# 15.3 — check_segment_health
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentHealth:
    """One row from ``gp_segment_configuration`` — segment posture.

    ``content`` is -1 for the coordinator, 0+ for segments (paired
    primary + mirror share a content number). ``role`` is what the
    segment is doing now (``p`` primary / ``m`` mirror / ``c``
    coordinator); ``preferred_role`` is what it should be doing — if
    they differ the segment has failed over and needs recovery.
    """

    dbid: int
    content: int
    role: str
    preferred_role: str
    mode: str
    status: str
    hostname: str
    port: int


@dataclass(frozen=True)
class SegmentHealthReport:
    """Aggregate result of :func:`check_segment_health`.

    ``unhealthy`` flags any segment whose `status != 'u'` (up) or
    whose `role != preferred_role` (failed over). ``mode='n'`` means
    "not in sync" — the segment is catching up on WAL.
    """

    available: bool
    total_segments: int
    healthy_count: int
    unhealthy_count: int
    out_of_sync_count: int
    segments: list[SegmentHealth]
    detail: str


async def check_segment_health(driver: SqlDriver) -> SegmentHealthReport:
    """Walk ``gp_segment_configuration`` and surface segment posture.

    Sorts segments by content ID so the coordinator (content=-1)
    surfaces first, then primary/mirror pairs in order. ``unhealthy``
    rolls up rows where the `status` column is not `'u'` (up) OR the
    `role` doesn't match `preferred_role` (post-failover).
    """
    status = await get_warehousepg_status(driver)
    if not status.available:
        return SegmentHealthReport(
            available=False,
            total_segments=0,
            healthy_count=0,
            unhealthy_count=0,
            out_of_sync_count=0,
            segments=[],
            detail=("Segment health is only available on WarehousePG / Greenplum MPP. " + status.detail),
        )

    try:
        rows = await driver.execute_query(
            "SELECT dbid, content, role, preferred_role, mode, status, hostname, port "
            "FROM gp_segment_configuration ORDER BY content, role",
            force_readonly=True,
        )
    except Exception as exc:
        return SegmentHealthReport(
            available=False,
            total_segments=0,
            healthy_count=0,
            unhealthy_count=0,
            out_of_sync_count=0,
            segments=[],
            detail=f"Segment health probe failed: {exc}",
        )

    segments: list[SegmentHealth] = []
    healthy = 0
    unhealthy = 0
    out_of_sync = 0
    for row in rows or []:
        cells = row.cells
        status_code = str(cells.get("status") or "")
        role = str(cells.get("role") or "")
        preferred = str(cells.get("preferred_role") or "")
        mode = str(cells.get("mode") or "")
        is_up = status_code == "u"
        is_failover = bool(role and preferred and role != preferred)
        is_sync = mode != "n"  # 'n' = not-in-sync; 's' = sync; 'c' = changetracking
        if is_up and not is_failover:
            healthy += 1
        else:
            unhealthy += 1
        if not is_sync:
            out_of_sync += 1
        segments.append(
            SegmentHealth(
                dbid=int(cells.get("dbid") or 0),
                content=int(cells.get("content") or 0),
                role=role,
                preferred_role=preferred,
                mode=mode,
                status=status_code,
                hostname=str(cells.get("hostname") or ""),
                port=int(cells.get("port") or 0),
            )
        )

    if unhealthy or out_of_sync:
        detail = f"{unhealthy} segment(s) unhealthy (status != 'u' or post-failover); {out_of_sync} not in sync."
    else:
        detail = f"All {len(segments)} entries healthy and in sync."
    return SegmentHealthReport(
        available=True,
        total_segments=len(segments),
        healthy_count=healthy,
        unhealthy_count=unhealthy,
        out_of_sync_count=out_of_sync,
        segments=segments,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 15.4 — describe_ao_table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppendOptimizedTableInfo:
    """Storage metadata for an append-optimized (AO) or AO/CO table.

    ``columnar`` is ``True`` for AO/CO (column-oriented) tables,
    ``False`` for AO row-oriented. ``compression_type`` is the
    catalog's ``compresstype`` value (``none``, ``zlib``, ``zstd``,
    ``rle_type``, ``quicklz`` — historic versions only). When the
    table isn't AO at all, ``is_ao=False`` and the storage fields
    are all ``None``.
    """

    schema: str
    table: str
    is_ao: bool
    columnar: bool
    compression_type: str | None
    compression_level: int | None
    block_size: int | None
    checksum: bool | None
    detail: str


async def describe_ao_table(driver: SqlDriver, schema: str, table: str) -> AppendOptimizedTableInfo:
    """Describe storage settings for an append-optimized table.

    Reads ``pg_appendonly`` for the relation's catalog metadata —
    compression, block size, checksum, and row vs column orientation.
    Returns ``is_ao=False`` (with storage fields ``None``) when the
    table is a regular heap table.
    """
    status = await get_warehousepg_status(driver)
    if not status.available:
        return AppendOptimizedTableInfo(
            schema=schema,
            table=table,
            is_ao=False,
            columnar=False,
            compression_type=None,
            compression_level=None,
            block_size=None,
            checksum=None,
            detail=("AO table introspection is only available on WarehousePG / Greenplum MPP. " + status.detail),
        )

    try:
        rows = await driver.execute_query(
            "SELECT ao.columnstore, ao.compresstype, ao.compresslevel, "
            "       ao.blocksize, ao.checksum "
            "FROM pg_appendonly ao "
            "JOIN pg_class c ON c.oid = ao.relid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            params=[schema, table],
            force_readonly=True,
        )
    except Exception as exc:
        return AppendOptimizedTableInfo(
            schema=schema,
            table=table,
            is_ao=False,
            columnar=False,
            compression_type=None,
            compression_level=None,
            block_size=None,
            checksum=None,
            detail=f"AO table probe failed: {exc}",
        )
    if not rows:
        return AppendOptimizedTableInfo(
            schema=schema,
            table=table,
            is_ao=False,
            columnar=False,
            compression_type=None,
            compression_level=None,
            block_size=None,
            checksum=None,
            detail=(
                f"Table '{schema}.{table}' is not append-optimized "
                "(no row in pg_appendonly). Regular heap tables don't "
                "have AO storage metadata."
            ),
        )

    cells = rows[0].cells
    columnar = bool(cells.get("columnstore"))
    compression = cells.get("compresstype")
    compression_str = str(compression) if compression else None
    if compression_str and compression_str.lower() == "none":
        compression_str = None
    return AppendOptimizedTableInfo(
        schema=schema,
        table=table,
        is_ao=True,
        columnar=columnar,
        compression_type=compression_str,
        compression_level=int(cells.get("compresslevel") or 0) or None,
        block_size=int(cells.get("blocksize") or 0) or None,
        checksum=bool(cells.get("checksum")) if cells.get("checksum") is not None else None,
        detail=(
            f"{'AO/CO column-oriented' if columnar else 'AO row-oriented'} "
            f"table, compression={compression_str or 'none'}, "
            f"block_size={cells.get('blocksize') or 'unknown'}."
        ),
    )


# ---------------------------------------------------------------------------
# 15.5 — list_resource_groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceGroup:
    """One row from ``gp_toolkit.gp_resgroup_config``.

    ``cpu_max_percent`` is the cgroup CPU cap; ``cpu_weight`` is the
    relative share when groups compete (higher = more). ``memory_limit``
    is the percentage of segment memory available to the group.
    ``active_queries`` / ``queued_queries`` are derived from
    ``pg_resgroup_get_status_kv`` where available.
    """

    name: str
    concurrency: int
    cpu_max_percent: int
    cpu_weight: int
    memory_limit: int
    memory_shared_quota: int
    active_queries: int | None
    queued_queries: int | None


@dataclass(frozen=True)
class ResourceGroupReport:
    """Aggregate result of :func:`list_resource_groups`."""

    available: bool
    groups: list[ResourceGroup]
    detail: str


async def list_resource_groups(driver: SqlDriver) -> ResourceGroupReport:
    """List configured resource groups + their utilisation."""
    status = await get_warehousepg_status(driver)
    if not status.available:
        return ResourceGroupReport(
            available=False,
            groups=[],
            detail=("Resource groups are only available on WarehousePG / Greenplum MPP. " + status.detail),
        )

    try:
        rows = await driver.execute_query(
            "SELECT groupname, concurrency, cpu_max_percent, cpu_weight, "
            "       memory_limit, memory_shared_quota, "
            "       num_running, num_queueing "
            "FROM gp_toolkit.gp_resgroup_status "
            "ORDER BY groupname",
            force_readonly=True,
        )
    except Exception as exc:
        return ResourceGroupReport(
            available=False,
            groups=[],
            detail=f"Resource group probe failed: {exc}",
        )

    groups: list[ResourceGroup] = []
    for row in rows or []:
        cells = row.cells
        groups.append(
            ResourceGroup(
                name=str(cells.get("groupname") or ""),
                concurrency=int(cells.get("concurrency") or 0),
                cpu_max_percent=int(cells.get("cpu_max_percent") or 0),
                cpu_weight=int(cells.get("cpu_weight") or 0),
                memory_limit=int(cells.get("memory_limit") or 0),
                memory_shared_quota=int(cells.get("memory_shared_quota") or 0),
                active_queries=(int(num_running) if (num_running := cells.get("num_running")) is not None else None),
                queued_queries=(int(num_queueing) if (num_queueing := cells.get("num_queueing")) is not None else None),
            )
        )

    return ResourceGroupReport(
        available=True,
        groups=groups,
        detail=f"{len(groups)} resource group(s) configured.",
    )


__all__ = [
    "AppendOptimizedTableInfo",
    "DistributionPolicy",
    "DistributionPolicyReport",
    "ResourceGroup",
    "ResourceGroupReport",
    "SegmentHealth",
    "SegmentHealthReport",
    "WarehousePGStatus",
    "check_segment_health",
    "describe_ao_table",
    "get_warehousepg_status",
    "list_distribution_policies",
    "list_resource_groups",
]
