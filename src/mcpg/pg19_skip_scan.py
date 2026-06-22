"""PG 19 skip-scan-aware index advisor — `recommend_skip_scan_indexes`.

PG 19's B-tree skip-scan optimisation makes existing composite indexes
useful for queries that filter only on *non-leading* columns. Pre-19, a
composite index ``(a, b, c)`` only accelerated queries that filtered on
``a`` (or ``a, b``, or ``a, b, c``); to handle a query filtering only
on ``b`` an operator had to create a second index. PG 19 lets the
planner "skip" over distinct values of ``a`` and binary-search within
each, so the same composite index now serves the b-only / c-only cases
too — provided ``a`` has low cardinality.

The canonical win is a composite index whose **leading column has low
NDV** (number of distinct values). For example, ``(status, created_at)``
where ``status`` has 4 distinct values: PG 19 can use this index to
satisfy a ``WHERE created_at > '...'`` filter that previously demanded
a separate index on ``created_at``.

Module surface (one read tool + status probe):

* ``get_skip_scan_status`` — version probe; never raises. Reports
  whether PG 19's skip-scan is the planner's default on this server.
* ``recommend_skip_scan_indexes`` — scans ``pg_index`` joined to ``pg_stats``
  for composite B-tree indexes whose leading column has a low NDV.
  Returns a list of :class:`SkipScanCandidate` so the agent can tell
  the user "these indexes just got more useful — consider dropping
  the dedicated single-column indexes on the trailing columns".

Backward compatibility
----------------------
Additive. The existing ``recommend_indexes`` and ``recommend_index_drops``
keep working on every supported PG version with the same shape and
reason codes. On PG ≤ 18 this advisor returns ``available=False``
with a guidance string pointing at the standard catch-up: add a
second single-column index for the non-leading column.

Security posture
----------------
* Pure read-only catalog queries; no caller-supplied identifiers in
  any identifier slot.
* All driver failures route through ``try/except`` and surface as
  ``available=False`` (status probe) or an empty list with the
  status-probe diagnostic on the advisor — never raise.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# PG 19 ships skip-scan as the planner default. The version-num boundary.
_MIN_PG19_SKIP_SCAN_VERSION = 190000

# A composite B-tree index whose leading column has fewer than this many
# distinct values is a strong skip-scan candidate. Picked empirically:
# 1000 is generous enough to flag most useful cases (status, category,
# region, etc.) without flooding the output with high-NDV leaders
# (timestamp, user_id, etc.) where skip-scan is too expensive.
_MAX_LEADING_NDV = 1000

# pg_stats.n_distinct can be negative (a "fraction of row count"); we
# normalise it to an absolute estimate. ANALYZE not run yet → 0.
_NDV_UNKNOWN = 0


class Pg19SkipScanError(Exception):
    """Raised when a skip-scan advisor operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkipScanStatus:
    """Reports whether PG 19's skip-scan optimisation is usable.

    ``available`` is True when ``server_version_num`` >= 190000.
    ``detail`` is the agent-facing guidance string — on PG ≤ 18 it
    points at the standard "create a second index" fallback.
    """

    available: bool
    server_version_num: int
    server_version: str
    detail: str


@dataclass(frozen=True, slots=True)
class SkipScanCandidate:
    """One composite B-tree index that PG 19's skip-scan unlocks.

    ``leading_column`` is the index's first key column; its low NDV
    is what makes skip-scan profitable. ``trailing_columns`` are the
    remaining key columns — those are the ones whose dedicated
    single-column indexes can be reviewed for possible drop once
    skip-scan is in play.

    ``estimated_leading_ndv`` is the absolute NDV estimate from
    ``pg_stats.n_distinct``; 0 means the stat is unavailable (ANALYZE
    hasn't run yet on the table).
    """

    schema: str
    table: str
    index_name: str
    leading_column: str
    trailing_columns: tuple[str, ...]
    estimated_leading_ndv: int
    rationale: str


# ---------------------------------------------------------------------------
# Shared probes
# ---------------------------------------------------------------------------


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


def _absolute_ndv(n_distinct: float | int | None, reltuples: float | int | None) -> int:
    """Normalise pg_stats.n_distinct to an absolute integer estimate.

    Per the PG docs:
    * positive → absolute number of distinct values
    * negative → fraction of row count (e.g. -1 = unique, -0.5 = half-unique)
    * 0       → estimate unavailable (no ANALYZE since last truncation, etc.)
    """
    if n_distinct is None:
        return _NDV_UNKNOWN
    try:
        nd = float(n_distinct)
    except (TypeError, ValueError):
        return _NDV_UNKNOWN
    if nd == 0:
        return _NDV_UNKNOWN
    if nd > 0:
        return int(nd)
    # Negative → fraction; need reltuples to convert. If we don't have it,
    # report 0 (unknown) rather than guess.
    if reltuples is None:
        return _NDV_UNKNOWN
    try:
        rt = float(reltuples)
    except (TypeError, ValueError):
        return _NDV_UNKNOWN
    if rt <= 0:
        return _NDV_UNKNOWN
    return max(int(-nd * rt), 1)


# ---------------------------------------------------------------------------
# Status probe
# ---------------------------------------------------------------------------


async def get_skip_scan_status(driver: SqlDriver) -> SkipScanStatus:
    """Report whether PG 19's B-tree skip-scan is usable on this server.

    Read-only; never raises. On PG ≤ 18 returns ``available=False``
    with a guidance string pointing at the standard "add a second
    single-column index" fallback.
    """
    try:
        ver_num, ver = await _server_version(driver)
    except Exception as exc:
        return SkipScanStatus(
            available=False,
            server_version_num=0,
            server_version="",
            detail=(
                f"Skip-scan status unavailable (version probe failed: {exc}). Re-run after the server is back online."
            ),
        )
    available = ver_num >= _MIN_PG19_SKIP_SCAN_VERSION
    if available:
        detail = (
            "PG 19 B-tree skip-scan is the planner default. Composite indexes "
            "whose leading column has low NDV can now serve queries that filter "
            "on trailing columns — call recommend_skip_scan_indexes() to find them."
        )
    else:
        detail = (
            "PG 19 B-tree skip-scan requires PostgreSQL 19 or newer; this "
            "server is older. For queries that filter on a non-leading column "
            "of a composite index, fall back to creating a dedicated "
            "single-column index on that column."
        )
    return SkipScanStatus(
        available=available,
        server_version_num=ver_num,
        server_version=ver,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# recommend_skip_scan_indexes — the advisor
# ---------------------------------------------------------------------------


async def recommend_skip_scan_indexes(
    driver: SqlDriver, *, max_leading_ndv: int = _MAX_LEADING_NDV
) -> list[SkipScanCandidate]:
    """Find composite B-tree indexes that PG 19's skip-scan unlocks.

    Walks ``pg_index`` for composite B-tree indexes (``natts > 1``) and
    joins with ``pg_stats`` to read the leading column's NDV. An index
    is flagged when its leading column NDV is below ``max_leading_ndv``
    (default 1000 — see the module-level constant for rationale).

    Read-only; returns an empty list on driver failure or on PG ≤ 18.
    For the PG ≤ 18 / driver-failure paths the caller should pair with
    ``get_skip_scan_status`` to surface the diagnostic.
    """
    try:
        ver_num, _ = await _server_version(driver)
    except Exception:
        return []
    if ver_num < _MIN_PG19_SKIP_SCAN_VERSION:
        return []
    try:
        rows = await driver.execute_query(
            # B-tree (am.amname = 'btree'), multi-column (cardinality > 1),
            # exclude expression / partial indexes (indexprs / indpred IS NULL)
            # for the simplest case — those are a follow-up.
            "WITH idx AS ( "
            "  SELECT i.indexrelid, i.indrelid, "
            "         (SELECT array_agg(attname ORDER BY ord) "
            "          FROM unnest(i.indkey) WITH ORDINALITY AS u(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = u.attnum) AS cols "
            "  FROM pg_index i "
            "  JOIN pg_class ci ON ci.oid = i.indexrelid "
            "  JOIN pg_am am ON am.oid = ci.relam "
            "  WHERE am.amname = 'btree' "
            "    AND i.indnatts > 1 "
            "    AND i.indexprs IS NULL "
            "    AND i.indpred IS NULL "
            ") "
            "SELECT n.nspname AS schema, t.relname AS table_name, ci.relname AS index_name, "
            "       idx.cols AS cols, s.n_distinct AS n_distinct, t.reltuples AS reltuples "
            "FROM idx "
            "JOIN pg_class ci ON ci.oid = idx.indexrelid "
            "JOIN pg_class t ON t.oid = idx.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "LEFT JOIN pg_stats s "
            "  ON s.schemaname = n.nspname "
            " AND s.tablename = t.relname "
            " AND s.attname = idx.cols[1] "
            "WHERE array_length(idx.cols, 1) > 1 "
            "  AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY n.nspname, t.relname, ci.relname",
            force_readonly=True,
        )
    except Exception:
        return []

    candidates: list[SkipScanCandidate] = []
    for row in rows or []:
        cells = row.cells
        cols = cells.get("cols")
        if not cols or len(cols) < 2:
            continue
        ndv = _absolute_ndv(cells.get("n_distinct"), cells.get("reltuples"))
        # ndv == 0 means we couldn't measure — skip rather than guess that
        # the leading column is low-cardinality.
        if ndv == _NDV_UNKNOWN or ndv > max_leading_ndv:
            continue
        leading = str(cols[0])
        trailing = tuple(str(c) for c in cols[1:])
        rationale = (
            f"composite B-tree index whose leading column {leading!r} has only ~{ndv} "
            f"distinct values — PG 19 skip-scan can satisfy queries filtering on "
            f"{', '.join(repr(c) for c in trailing)} alone. Review any dedicated "
            "single-column indexes on those trailing columns: they may be droppable."
        )
        candidates.append(
            SkipScanCandidate(
                schema=str(cells.get("schema") or ""),
                table=str(cells.get("table_name") or ""),
                index_name=str(cells.get("index_name") or ""),
                leading_column=leading,
                trailing_columns=trailing,
                estimated_leading_ndv=ndv,
                rationale=rationale,
            )
        )
    return candidates


__all__ = [
    "Pg19SkipScanError",
    "SkipScanCandidate",
    "SkipScanStatus",
    "get_skip_scan_status",
    "recommend_skip_scan_indexes",
]
