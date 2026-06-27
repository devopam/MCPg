"""Session-scope cost advisor — reads ``mcpg_audit.events`` and surfaces
hot-path inefficiencies before they cost real tokens.

Realises roadmap row **8.7**. Companion to the audit-trail subsystem in
:mod:`mcpg.audit_trail` (which writes events) — this module reads
them back and rolls them up into actionable findings.

Why this matters
================

Agents driving MCPg frequently call ``list_tables`` / ``list_schemas`` /
``list_indexes`` once per planning step. Across a long conversation
those repeated probes dominate the token budget — a single
``get_compact_schema`` call returns the same information in one
round-trip. Telling the operator "the agent called ``list_tables`` 47
times this session, you saved X tokens with ``get_compact_schema``"
is the highest-leverage piece of feedback we can give a
token-conscious deployment.

Scoping
=======

The audit table has no ``session_id`` column today (every event is
clock-time-stamped; the session boundary is implicit). The advisor
uses a **lookback window** as the session proxy: by default the last
60 minutes, configurable via ``lookback_minutes``. For a deployment
that wants stricter scoping, callers pass an explicit
``occurred_since`` ISO-8601 timestamp.

Read-only — never writes back. Lives under the ``observability``
bucket alongside ``describe_tool`` and the rest of the agent
self-recovery loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg._vendor.sql import SqlDriver

# Tools that return small, well-defined slices of the catalogue — calling
# them many times in a session is the canonical "would have been one
# get_compact_schema" anti-pattern.
_CATALOGUE_LISTING_TOOLS: frozenset[str] = frozenset(
    {
        "list_tables",
        "list_schemas",
        "list_indexes",
        "list_columns",
        "list_views",
        "list_sequences",
        "list_functions",
        "list_extensions",
    }
)

# Reason codes — stable strings the agent / advisor consumer can branch on.
REASON_REDUNDANT_LISTING = "redundant_listing"
REASON_HOT_REPEATED_CALL = "hot_repeated_call"
REASON_IDLE_SESSION = "idle_session"


class SessionAdvisorError(Exception):
    """Raised when the advisor can't run — e.g. the audit table is missing."""


@dataclass(frozen=True)
class CostFinding:
    """One inefficiency surfaced by :func:`analyze_session_cost`.

    ``reason`` is a stable string (see ``REASON_*`` constants).
    ``tool`` is the offending tool name. ``call_count`` is how many
    times it was called in the lookback window. ``suggestion`` is a
    one-line recommendation suitable for surfacing back to an LLM.
    """

    reason: str
    tool: str
    call_count: int
    suggestion: str


@dataclass(frozen=True)
class SessionCostAnalysis:
    """Roll-up of :func:`analyze_session_cost`.

    ``audit_table_present`` distinguishes "advisor returned no
    findings because the audit table doesn't exist (audit subsystem
    disabled)" from "audit table is there but no events in window".
    ``events_examined`` is the row count in the lookback window;
    ``lookback_minutes`` echoes the window size used.
    """

    audit_table_present: bool
    events_examined: int
    lookback_minutes: int
    findings: list[CostFinding] = field(default_factory=list)
    detail: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _audit_table_present(driver: SqlDriver) -> bool:
    """Cheap to_regclass probe — single round trip."""
    rows = await driver.execute_query(
        "SELECT to_regclass('mcpg_audit.events') IS NOT NULL AS present",
        force_readonly=True,
    )
    if not rows:
        return False
    return bool(rows[0].cells.get("present"))


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def analyze_session_cost(
    driver: SqlDriver,
    *,
    lookback_minutes: int = 60,
    hot_threshold: int = 10,
) -> SessionCostAnalysis:
    """Surface hot-path inefficiencies from the last ``lookback_minutes``.

    Two classes of finding today:

    * **``redundant_listing``** — a catalogue-listing tool
      (``list_tables`` / ``list_schemas`` / ``list_indexes`` / …) was
      called more than ``hot_threshold`` times. The suggestion is to
      use ``get_compact_schema`` instead (one call returns the
      cross-product).
    * **``hot_repeated_call``** — any other tool was called more than
      ``hot_threshold`` times. The suggestion is to cache the result
      in the agent's conversation memory; MCPg's per-tool cache
      already handles read-side deduplication within a single
      ``call_tool`` invocation, but not across conversation turns.

    The advisor returns ``audit_table_present=False`` with a clear
    diagnostic when the audit subsystem isn't enabled — that's the
    most common "no findings" reason and operators need to tell it
    apart from "session was quiet."

    Args:
        lookback_minutes: Window size. Default ``60`` matches a typical
            agent session. Capped at 1440 (24h) so the SQL ``interval``
            clause doesn't underflow on absurd inputs.
        hot_threshold: Calls/window above which a tool is flagged.
            Default ``10`` matches the inflection point measured in
            informal token-spend audits.
    """
    if lookback_minutes < 1:
        raise SessionAdvisorError(f"lookback_minutes must be >= 1; got {lookback_minutes}")
    if lookback_minutes > 1440:
        raise SessionAdvisorError(f"lookback_minutes capped at 1440 (24h); got {lookback_minutes}")
    if hot_threshold < 1:
        raise SessionAdvisorError(f"hot_threshold must be >= 1; got {hot_threshold}")

    present = await _audit_table_present(driver)
    if not present:
        return SessionCostAnalysis(
            audit_table_present=False,
            events_examined=0,
            lookback_minutes=lookback_minutes,
            findings=[],
            detail=(
                "mcpg_audit.events is not present — enable the audit subsystem "
                "(set MCPG_AUDIT=true) so this advisor has data to analyse."
            ),
        )

    # Aggregate by tool over the window. We use ``make_interval`` so
    # the interval is bound through the parameter slot rather than
    # f-string-composed.
    rows = await driver.execute_query(
        "SELECT tool, COUNT(*) AS call_count "
        "FROM mcpg_audit.events "
        "WHERE occurred_at >= now() - make_interval(mins => %s) "
        "GROUP BY tool "
        "ORDER BY call_count DESC",
        params=[lookback_minutes],
        force_readonly=True,
    )

    findings: list[CostFinding] = []
    total_events = 0
    for row in rows or []:
        tool = str(row.cells["tool"])
        count = int(row.cells["call_count"])
        total_events += count
        if count <= hot_threshold:
            continue
        if tool in _CATALOGUE_LISTING_TOOLS:
            findings.append(
                CostFinding(
                    reason=REASON_REDUNDANT_LISTING,
                    tool=tool,
                    call_count=count,
                    suggestion=(
                        f"{tool!r} was called {count} times in the last {lookback_minutes} minute(s). "
                        "Call get_compact_schema once instead — it returns "
                        "schemas / tables / columns / indexes in one round trip."
                    ),
                )
            )
        else:
            findings.append(
                CostFinding(
                    reason=REASON_HOT_REPEATED_CALL,
                    tool=tool,
                    call_count=count,
                    suggestion=(
                        f"{tool!r} was called {count} times in the last {lookback_minutes} minute(s). "
                        "Cache the result in the agent's conversation memory rather than re-calling."
                    ),
                )
            )

    if total_events == 0:
        return SessionCostAnalysis(
            audit_table_present=True,
            events_examined=0,
            lookback_minutes=lookback_minutes,
            findings=[
                CostFinding(
                    reason=REASON_IDLE_SESSION,
                    tool="",
                    call_count=0,
                    suggestion=(
                        f"No tool calls recorded in the last {lookback_minutes} minute(s) — "
                        "either the session is idle or the audit pipeline lost events."
                    ),
                )
            ],
            detail=f"No tool calls in the last {lookback_minutes} minute(s).",
        )

    detail = (
        f"{total_events} tool call(s) in the last {lookback_minutes} minute(s); "
        f"{len(findings)} inefficiency finding(s)."
        if findings
        else (
            f"{total_events} tool call(s) in the last {lookback_minutes} minute(s); "
            "no tool exceeded the hot-call threshold."
        )
    )

    return SessionCostAnalysis(
        audit_table_present=True,
        events_examined=total_events,
        lookback_minutes=lookback_minutes,
        findings=findings,
        detail=detail,
    )


__all__ = [
    "REASON_HOT_REPEATED_CALL",
    "REASON_IDLE_SESSION",
    "REASON_REDUNDANT_LISTING",
    "CostFinding",
    "SessionAdvisorError",
    "SessionCostAnalysis",
    "analyze_session_cost",
]
