"""WAL archive inspection — `pg_stat_archiver` + archive configuration.

Realises roadmap row **5.2**. Companion to `pg_walinspect` (roadmap
2.3, which inspects WAL *records*): this surfaces the WAL *archive*
status — whether continuous archiving is on, how many segments have
been archived vs failed, and the last-archived / last-failed segment
and timestamp.

The single read tool `get_wal_archive_status` answers the operator
question "is my WAL archiving healthy?" in one call, with a computed
`healthy` verdict and a human-readable `detail` so an agent can branch
without re-deriving the heuristic.

Why this matters
================

WAL archiving is the backbone of point-in-time recovery and
streaming-replica seeding. When `archive_command` / `archive_library`
starts failing (full archive volume, bad credentials, network
partition to the object store), Postgres retries the *same* segment
forever and WAL accumulates in `pg_wal/` until the volume fills — a
silent, slow-motion outage. The early signal lives in
`pg_stat_archiver`: a rising `failed_count` and a `last_failed_time`
more recent than `last_archived_time`. This tool makes that signal a
one-call check.

Read-only; never raises — a driver failure surfaces as
`available=False` with the error in `detail`. The `archive_command`
string is **not** echoed (it can embed credentials for the object
store); only a boolean `archive_command_set` is reported.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver


@dataclass(frozen=True)
class WalArchiveStatus:
    """Roll-up of WAL-archiving health from `pg_stat_archiver` + GUCs.

    ``available`` is ``False`` only when the status probe itself fails
    (driver error). ``archiving_enabled`` reflects ``archive_mode``
    (``on`` / ``always``); when it's off the counters are reported as-is
    but ``healthy`` is ``True`` (nothing to fail).

    ``archive_command_set`` is a boolean — the raw command/library
    string is deliberately not surfaced (it can carry object-store
    credentials). ``healthy`` is ``False`` when archiving is enabled and
    the most recent archive attempt *failed* (``last_failed_time`` is
    newer than ``last_archived_time``, or there are failures and nothing
    has ever archived).
    """

    available: bool
    archiving_enabled: bool
    archive_mode: str
    archive_command_set: bool
    archived_count: int
    last_archived_wal: str | None
    last_archived_time: str | None
    failed_count: int
    last_failed_wal: str | None
    last_failed_time: str | None
    stats_reset: str | None
    healthy: bool
    detail: str


def _assess(
    *,
    archiving_enabled: bool,
    archived_count: int,
    failed_count: int,
    last_archived_time: str | None,
    last_failed_time: str | None,
) -> tuple[bool, str]:
    """Return ``(healthy, detail)`` from the archiver counters.

    Heuristic: archiving is unhealthy when it's enabled AND the latest
    attempt failed. We compare ISO-8601 text timestamps — they sort
    lexicographically in the same order as chronologically when both
    carry the same offset (Postgres renders both from the same
    ``timestamptz`` column, so the offsets match), which is good enough
    for "which happened more recently".
    """
    if not archiving_enabled:
        return True, "WAL archiving is disabled (archive_mode = off); nothing to monitor."
    if failed_count == 0:
        return True, f"WAL archiving healthy — {archived_count} segment(s) archived, no failures."
    # There have been failures at some point. Unhealthy only if the most
    # recent attempt was a failure.
    latest_failed_more_recent = last_failed_time is not None and (
        last_archived_time is None or last_failed_time > last_archived_time
    )
    if latest_failed_more_recent:
        return False, (
            f"WAL archiving is FAILING — {failed_count} failure(s); the most recent attempt failed. "
            "Check the archive_command / archive_library target (full volume, credentials, "
            "or network to the object store). WAL will accumulate in pg_wal/ until resolved."
        )
    return True, (
        f"WAL archiving recovered — {failed_count} historical failure(s) but the latest "
        f"attempt succeeded ({archived_count} segment(s) archived total)."
    )


async def get_wal_archive_status(driver: SqlDriver) -> WalArchiveStatus:
    """Report WAL-archiving health from ``pg_stat_archiver`` + GUCs.

    Single round trip. Read-only; never raises — a driver failure comes
    back as ``available=False`` with the error in ``detail``.
    """
    try:
        rows = await driver.execute_query(
            "SELECT "
            "  current_setting('archive_mode') AS archive_mode, "
            "  NULLIF(current_setting('archive_command'), '') IS NOT NULL AS archive_command_set, "
            "  s.archived_count, "
            "  s.last_archived_wal, "
            "  s.last_archived_time::text AS last_archived_time, "
            "  s.failed_count, "
            "  s.last_failed_wal, "
            "  s.last_failed_time::text AS last_failed_time, "
            "  s.stats_reset::text AS stats_reset "
            "FROM pg_stat_archiver s",
            force_readonly=True,
        )
    except Exception as exc:
        return WalArchiveStatus(
            available=False,
            archiving_enabled=False,
            archive_mode="unknown",
            archive_command_set=False,
            archived_count=0,
            last_archived_wal=None,
            last_archived_time=None,
            failed_count=0,
            last_failed_wal=None,
            last_failed_time=None,
            stats_reset=None,
            healthy=False,
            detail=f"Could not read pg_stat_archiver: {exc}",
        )

    if not rows:
        return WalArchiveStatus(
            available=False,
            archiving_enabled=False,
            archive_mode="unknown",
            archive_command_set=False,
            archived_count=0,
            last_archived_wal=None,
            last_archived_time=None,
            failed_count=0,
            last_failed_wal=None,
            last_failed_time=None,
            stats_reset=None,
            healthy=False,
            detail="pg_stat_archiver returned no rows.",
        )

    c = rows[0].cells
    archive_mode = str(c.get("archive_mode") or "off")
    archiving_enabled = archive_mode in {"on", "always"}
    archived_count = int(c.get("archived_count") or 0)
    failed_count = int(c.get("failed_count") or 0)
    last_archived_time = c.get("last_archived_time")
    last_failed_time = c.get("last_failed_time")

    healthy, detail = _assess(
        archiving_enabled=archiving_enabled,
        archived_count=archived_count,
        failed_count=failed_count,
        last_archived_time=last_archived_time,
        last_failed_time=last_failed_time,
    )

    return WalArchiveStatus(
        available=True,
        archiving_enabled=archiving_enabled,
        archive_mode=archive_mode,
        archive_command_set=bool(c.get("archive_command_set")),
        archived_count=archived_count,
        last_archived_wal=c.get("last_archived_wal"),
        last_archived_time=last_archived_time,
        failed_count=failed_count,
        last_failed_wal=c.get("last_failed_wal"),
        last_failed_time=last_failed_time,
        stats_reset=c.get("stats_reset"),
        healthy=healthy,
        detail=detail,
    )


__all__ = ["WalArchiveStatus", "get_wal_archive_status"]
