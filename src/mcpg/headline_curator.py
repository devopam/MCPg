"""Dynamic ``headline_tools`` recommender — empirical curation from the audit log.

Realises roadmap row **14.4**. The hand-curated ``headline_tools`` tuple
on each :class:`mcpg.about.Capability` is the agent's first-pass map of
"what should I reach for in this bucket?" — but the curated list ages
as new tools land and as real usage shifts. This module surfaces the
top-N most-called tools per bucket over a configurable lookback window,
read from ``mcpg_audit.events`` (same data source 8.7's session-cost
advisor uses), so operators have an empirical second opinion.

Lives next to :mod:`mcpg.about` rather than inside it because it
needs a live DB driver — ``about.py`` is intentionally pure-python so
the surface metadata stays importable from boot-time contexts where
no driver exists.

Why this is a recommendation, not a default
===========================================

Telemetry-driven headlines have failure modes that pure-curation
doesn't: a bucket nobody touched for a week gets an empty headline
list; a tool called for diagnostic reasons (e.g. ``why_is_this_slow``
during an incident) spikes to the top and stays there until the
window rolls forward. So we ship ``recommend_headline_tools`` as a
**reviewable recommendation**, not an auto-applied override. The
operator reads the report, decides whether to update the curated
tuple, and commits the change deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg.about import CAPABILITIES, classify_tool
from mcpg.sql import SqlDriver


class HeadlineCuratorError(Exception):
    """Raised when the recommender's arguments fail validation."""


@dataclass(frozen=True, slots=True)
class BucketHeadlineRecommendation:
    """Per-bucket recommendation roll-up.

    ``current`` is the bucket's hand-curated ``headline_tools`` tuple
    today (so the diff against ``recommended`` is visible without a
    second lookup). ``recommended`` is the top-N tools by call count
    in the lookback window — empty when the bucket saw no audit
    events. ``newcomers`` are recommended tools NOT present in
    ``current`` (the most actionable signal); ``departures`` are
    current headlines that didn't crack the top-N (candidates for
    removal). ``call_counts`` is the per-tool tally so a reviewer
    can spot a one-off spike vs sustained usage.
    """

    bucket_id: str
    current: tuple[str, ...]
    recommended: tuple[str, ...]
    newcomers: tuple[str, ...]
    departures: tuple[str, ...]
    call_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HeadlineRecommendationReport:
    """Roll-up of :func:`recommend_headline_tools`.

    ``audit_table_present=False`` distinguishes "no recommendations
    because the audit subsystem is off" from "no recommendations
    because the lookback window was idle" (the latter still returns
    ``audit_table_present=True`` with per-bucket empty
    ``recommended`` lists).
    """

    audit_table_present: bool
    lookback_days: int
    top_n: int
    events_examined: int
    buckets: list[BucketHeadlineRecommendation] = field(default_factory=list)
    detail: str = ""


async def _audit_table_present(driver: SqlDriver) -> bool:
    """Cheap to_regclass probe — single round trip."""
    rows = await driver.execute_query(
        "SELECT to_regclass('mcpg_audit.events') IS NOT NULL AS present",
        force_readonly=True,
    )
    if not rows:
        return False
    return bool(rows[0].cells.get("present"))


async def recommend_headline_tools(
    driver: SqlDriver,
    *,
    lookback_days: int = 7,
    top_n: int = 6,
    current_headlines: dict[str, tuple[str, ...]] | None = None,
) -> HeadlineRecommendationReport:
    """Empirically curate ``headline_tools`` from recent audit data.

    Args:
        lookback_days: How far back to read events. Default ``7``;
            capped at ``90`` so the SQL ``interval`` doesn't underflow.
        top_n: How many tools to recommend per bucket. Default ``6``
            matches the existing hand-curated lists.
        current_headlines: Optional mapping ``bucket_id ->
            current headline tuple``. When supplied the report computes
            ``newcomers`` / ``departures`` against this baseline.
            Defaulting to ``None`` means the diff fields are populated
            with empty tuples (recommendation alone is still useful).

    Returns:
        :class:`HeadlineRecommendationReport`. ``audit_table_present=False``
        comes back with a guidance ``detail`` and an empty bucket list
        when the audit subsystem isn't on.
    """
    if lookback_days < 1:
        raise HeadlineCuratorError(f"lookback_days must be >= 1; got {lookback_days}")
    if lookback_days > 90:
        raise HeadlineCuratorError(f"lookback_days capped at 90; got {lookback_days}")
    if top_n < 1:
        raise HeadlineCuratorError(f"top_n must be >= 1; got {top_n}")
    if top_n > 50:
        raise HeadlineCuratorError(f"top_n capped at 50; got {top_n}")

    if not await _audit_table_present(driver):
        return HeadlineRecommendationReport(
            audit_table_present=False,
            lookback_days=lookback_days,
            top_n=top_n,
            events_examined=0,
            buckets=[],
            detail=(
                "mcpg_audit.events is not present — enable the audit "
                "subsystem (MCPG_AUDIT=true) so this recommender has data."
            ),
        )

    rows = await driver.execute_query(
        "SELECT tool, COUNT(*) AS call_count "
        "FROM mcpg_audit.events "
        "WHERE occurred_at >= now() - make_interval(days => %s) "
        "  AND status = 'success' "
        "GROUP BY tool",
        params=[lookback_days],
        force_readonly=True,
    )

    # Group by bucket; preserve call counts so the report can show
    # the magnitude alongside each recommendation. Initialised from
    # CAPABILITIES (the curated tuple, not BUCKET_IDS which is a
    # frozenset with non-deterministic iteration order — gemini review
    # on #180) so the resulting bucket list comes back in the same
    # display order as describe_self.
    bucket_to_counts: dict[str, dict[str, int]] = {cap.id: {} for cap in CAPABILITIES}
    total_events = 0
    for row in rows or []:
        tool = str(row.cells["tool"])
        count = int(row.cells["call_count"])
        total_events += count
        bucket = classify_tool(tool)
        # Defensive: classify_tool *should* always return a bucket in
        # BUCKET_IDS (the contract test enforces it) but a stale override
        # typo could slip through before that test runs. Skip rather
        # than crash the recommender on the bad row.
        if bucket is None or bucket not in bucket_to_counts:
            continue
        bucket_to_counts[bucket][tool] = count

    current_headlines = current_headlines or {}
    recommendations: list[BucketHeadlineRecommendation] = []
    for capability in CAPABILITIES:
        bucket_id = capability.id
        counts = bucket_to_counts.get(bucket_id, {})
        # Sort by count DESC, then alphabetically so ties are deterministic.
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        recommended = tuple(tool for tool, _ in ranked[:top_n])
        current = current_headlines.get(bucket_id, ())
        newcomers = tuple(t for t in recommended if t not in current)
        departures = tuple(t for t in current if t not in recommended)
        recommendations.append(
            BucketHeadlineRecommendation(
                bucket_id=bucket_id,
                current=current,
                recommended=recommended,
                newcomers=newcomers,
                departures=departures,
                call_counts=dict(counts),
            )
        )

    detail = (
        f"{total_events} successful tool call(s) over the last {lookback_days} day(s); top-{top_n} per bucket reported."
        if total_events
        else (
            f"No successful tool calls in the last {lookback_days} day(s) — "
            "every bucket's recommendation is empty. Lengthen the window or "
            "wait for traffic before treating these as actionable."
        )
    )

    return HeadlineRecommendationReport(
        audit_table_present=True,
        lookback_days=lookback_days,
        top_n=top_n,
        events_examined=total_events,
        buckets=recommendations,
        detail=detail,
    )


__all__ = [
    "BucketHeadlineRecommendation",
    "HeadlineCuratorError",
    "HeadlineRecommendationReport",
    "recommend_headline_tools",
]
