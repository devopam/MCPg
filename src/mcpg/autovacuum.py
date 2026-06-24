"""Autovacuum priority advisor — `read_autovacuum_priority`.

Surfaces the tables most urgently needing autovacuum, ordered by an
explicit priority score so an operator can answer "what's about to
trip autovacuum next" without combining ``pg_stat_user_tables`` with
the per-table threshold formula by hand.

Priority signals (per the PostgreSQL autovacuum heuristic itself):

* **Dead-tuple pressure** — how close ``n_dead_tup`` is to the
  per-table vacuum threshold (``autovacuum_vacuum_threshold +
  autovacuum_vacuum_scale_factor * reltuples``). A ratio of ≥1.0
  means autovacuum *should* already have woken up — the table is
  overdue. Reloptions override the cluster-level scale factor when
  set per-table via ``ALTER TABLE … SET (autovacuum_vacuum_scale_factor = …)``.

* **Analyze pressure** — same shape but for the ANALYZE pass:
  ``n_mod_since_analyze`` vs ``autovacuum_analyze_threshold +
  autovacuum_analyze_scale_factor * reltuples``.

* **Last-run staleness** — ``last_autovacuum`` IS NULL on a table
  with churn is a red flag; a recent ``last_autovacuum`` on a
  high-ratio table suggests autovacuum is running but can't keep up.

* **Per-table opt-out** — ``ALTER TABLE … SET (autovacuum_enabled =
  off)`` shows up in ``pg_class.reloptions``. Flagged explicitly so
  the agent doesn't recommend pushing harder on a table whose
  autovacuum is intentionally disabled.

PG 19 ships ``pg_stat_user_tables.n_ins_since_vacuum``-aware autovacuum
triggers; the helper exposes both ``n_ins_since_vacuum`` and
``n_dead_tup`` so the agent can see why the table got onto the
shortlist.

Backward compatibility: works on every supported PG version. The
report shape is uniform — pre-PG 19 servers report
``n_ins_since_vacuum=None`` (the column exists since PG 13, so this
is just a defensive None on a probe failure).

Security: pure read-only catalog joins; no caller-supplied identifiers
land in any identifier slot. All driver errors surface as an empty
list, not a raise — same convention as ``mcpg.health``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# Cluster defaults that drive the per-table threshold formula. We don't
# hardcode them — the query reads them from `current_setting` so a
# customised cluster gets the right numbers.
_DEFAULT_LIMIT = 25

# A table whose dead-tuple ratio (`n_dead_tup` / threshold) is at least
# this value lands in the "overdue" bucket; below this it's "watchlist".
# 1.0 is the line PG itself uses; we surface 0.5+ so an agent can act
# *before* the table tips over.
_WATCHLIST_RATIO = 0.5


@dataclass(frozen=True)
class AutovacuumPriorityRow:
    """One row of the priority report — a table on the shortlist.

    ``priority`` is ``overdue`` (``dead_tuple_ratio >= 1.0``),
    ``watchlist`` (``>= 0.5``), or ``borderline`` (everything else
    surfaced — typically reltuples-only candidates whose threshold
    is small enough that small dead-tuple counts still register).

    ``dead_tuple_ratio`` is ``n_dead_tup / vacuum_threshold`` — useful
    for sorting beyond the categorical bucket.
    """

    schema: str
    table: str
    reltuples: int
    n_dead_tup: int
    n_live_tup: int
    n_mod_since_analyze: int
    n_ins_since_vacuum: int | None
    vacuum_threshold: float
    analyze_threshold: float
    dead_tuple_ratio: float
    analyze_ratio: float
    last_autovacuum: str | None
    last_autoanalyze: str | None
    autovacuum_enabled: bool
    priority: str


@dataclass(frozen=True)
class AutovacuumPriorityReport:
    """Aggregate result of :func:`read_autovacuum_priority`.

    ``rows`` is ordered by ``dead_tuple_ratio`` descending so the most
    urgent tables come first. ``overdue_count`` is a quick at-a-glance
    "is anything actually past due" — agents can branch on it without
    walking the list.
    """

    available: bool
    overdue_count: int
    watchlist_count: int
    rows: list[AutovacuumPriorityRow]
    detail: str


_PRIORITY_QUERY = """
WITH defaults AS (
    SELECT
        current_setting('autovacuum_vacuum_threshold')::float AS vt,
        current_setting('autovacuum_vacuum_scale_factor')::float AS vsf,
        current_setting('autovacuum_analyze_threshold')::float AS at,
        current_setting('autovacuum_analyze_scale_factor')::float AS asf
)
SELECT
    s.schemaname AS schema,
    s.relname AS table_name,
    c.reltuples::bigint AS reltuples,
    s.n_dead_tup,
    s.n_live_tup,
    s.n_mod_since_analyze,
    s.n_ins_since_vacuum,
    -- Effective per-table thresholds, honouring reloptions when set.
    COALESCE(
        (SELECT option_value::float FROM pg_options_to_table(c.reloptions)
         WHERE option_name = 'autovacuum_vacuum_threshold' LIMIT 1),
        d.vt
    )
    + COALESCE(
        (SELECT option_value::float FROM pg_options_to_table(c.reloptions)
         WHERE option_name = 'autovacuum_vacuum_scale_factor' LIMIT 1),
        d.vsf
    ) * GREATEST(c.reltuples, 0) AS vacuum_threshold,
    COALESCE(
        (SELECT option_value::float FROM pg_options_to_table(c.reloptions)
         WHERE option_name = 'autovacuum_analyze_threshold' LIMIT 1),
        d.at
    )
    + COALESCE(
        (SELECT option_value::float FROM pg_options_to_table(c.reloptions)
         WHERE option_name = 'autovacuum_analyze_scale_factor' LIMIT 1),
        d.asf
    ) * GREATEST(c.reltuples, 0) AS analyze_threshold,
    s.last_autovacuum::text AS last_autovacuum,
    s.last_autoanalyze::text AS last_autoanalyze,
    -- autovacuum_enabled defaults to true; only flipped to false via
    -- ALTER TABLE ... SET (autovacuum_enabled = off).
    COALESCE(
        (SELECT option_value::bool FROM pg_options_to_table(c.reloptions)
         WHERE option_name = 'autovacuum_enabled' LIMIT 1),
        TRUE
    ) AS autovacuum_enabled
FROM pg_stat_user_tables s
JOIN pg_class c ON c.oid = s.relid
CROSS JOIN defaults d
WHERE c.relkind IN ('r', 'm')
"""


def _classify(dead_ratio: float) -> str:
    if dead_ratio >= 1.0:
        return "overdue"
    if dead_ratio >= _WATCHLIST_RATIO:
        return "watchlist"
    return "borderline"


async def read_autovacuum_priority(driver: SqlDriver, *, limit: int = _DEFAULT_LIMIT) -> AutovacuumPriorityReport:
    """Return tables ranked by how urgently they need autovacuum.

    Pure read-only catalog query. Sorts descending by
    ``dead_tuple_ratio`` (``n_dead_tup / vacuum_threshold``); the
    first ``limit`` rows are returned with explicit priority bucket
    labels.

    Driver errors surface as ``available=False`` with a diagnostic
    rather than a raise — same convention as the health checks.
    """
    if limit < 1:
        limit = _DEFAULT_LIMIT
    try:
        rows = await driver.execute_query(_PRIORITY_QUERY, force_readonly=True)
    except Exception as exc:
        return AutovacuumPriorityReport(
            available=False,
            overdue_count=0,
            watchlist_count=0,
            rows=[],
            detail=f"autovacuum priority probe failed: {exc}",
        )

    enriched: list[AutovacuumPriorityRow] = []
    for row in rows or []:
        cells = row.cells
        vacuum_threshold = float(cells.get("vacuum_threshold") or 0.0)
        analyze_threshold = float(cells.get("analyze_threshold") or 0.0)
        n_dead = int(cells.get("n_dead_tup") or 0)
        n_mod = int(cells.get("n_mod_since_analyze") or 0)
        n_ins = cells.get("n_ins_since_vacuum")
        dead_ratio = n_dead / vacuum_threshold if vacuum_threshold else 0.0
        analyze_ratio = n_mod / analyze_threshold if analyze_threshold else 0.0
        enriched.append(
            AutovacuumPriorityRow(
                schema=str(cells.get("schema") or ""),
                table=str(cells.get("table_name") or ""),
                reltuples=int(cells.get("reltuples") or 0),
                n_dead_tup=n_dead,
                n_live_tup=int(cells.get("n_live_tup") or 0),
                n_mod_since_analyze=n_mod,
                n_ins_since_vacuum=int(n_ins) if n_ins is not None else None,
                vacuum_threshold=vacuum_threshold,
                analyze_threshold=analyze_threshold,
                dead_tuple_ratio=dead_ratio,
                analyze_ratio=analyze_ratio,
                last_autovacuum=cells.get("last_autovacuum"),
                last_autoanalyze=cells.get("last_autoanalyze"),
                autovacuum_enabled=bool(cells.get("autovacuum_enabled", True)),
                priority=_classify(dead_ratio),
            )
        )

    # Sort by dead-tuple ratio descending — the most-overdue table on top.
    enriched.sort(key=lambda r: r.dead_tuple_ratio, reverse=True)
    overdue = sum(1 for r in enriched if r.priority == "overdue")
    watchlist = sum(1 for r in enriched if r.priority == "watchlist")

    # Trim to limit AFTER counting overdue / watchlist so the totals
    # remain accurate even when limit is small relative to the cluster.
    top = enriched[:limit]
    if not top:
        detail = "No tables on the autovacuum shortlist."
    elif overdue:
        detail = (
            f"{overdue} table(s) past the autovacuum threshold (priority=overdue); "
            f"{watchlist} approaching (priority=watchlist)."
        )
    elif watchlist:
        detail = (
            f"No overdue tables. {watchlist} approaching the autovacuum threshold "
            "(priority=watchlist) — monitor; no immediate action required."
        )
    else:
        detail = "All shortlisted tables are well below their autovacuum threshold."

    return AutovacuumPriorityReport(
        available=True,
        overdue_count=overdue,
        watchlist_count=watchlist,
        rows=top,
        detail=detail,
    )


__all__ = [
    "AutovacuumPriorityReport",
    "AutovacuumPriorityRow",
    "read_autovacuum_priority",
]
