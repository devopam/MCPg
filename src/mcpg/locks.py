"""Lock-inspection helpers — ``list_locks`` and ``find_blocking_chains``.

Two read-only tools that surface what ``pg_locks`` and
``pg_stat_activity`` say about who is waiting on whom right now. Both
are pure SELECTs against the live catalog; they take no parameters
beyond an optional limit.

The lock catalog is volatile — ``find_blocking_chains`` reports the
state at the moment it runs, not a longitudinal trace. Pair with
``analyze_query_plan`` / ``why_is_this_slow`` when investigating a
slow / stuck query.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

DEFAULT_LOCK_LIMIT = 100
DEFAULT_BLOCKING_LIMIT = 50


@dataclass(frozen=True, slots=True)
class LockInfo:
    """One row from ``pg_locks`` joined with ``pg_stat_activity``.

    ``relation`` is the qualified relation name when the lock is on a
    table / index / sequence; ``None`` for transaction-id and advisory
    locks. ``query`` is the snippet from ``pg_stat_activity.query`` so
    the agent can correlate a held lock with the work that took it.
    """

    pid: int
    locktype: str
    mode: str
    granted: bool
    relation: str | None
    transactionid: int | None
    virtualxid: str | None
    application_name: str | None
    state: str | None
    wait_event_type: str | None
    wait_event: str | None
    query: str | None


@dataclass(frozen=True, slots=True)
class BlockingPair:
    """One ``(blocked, blocking)`` pair from ``pg_blocking_pids``.

    The ``blocking_*`` fields describe the backend that's holding the
    lock the ``blocked_*`` backend wants. Cycles are possible (A blocks
    B, B blocks A) — render with care.
    """

    blocked_pid: int
    blocked_query: str | None
    blocked_application_name: str | None
    blocked_wait_event: str | None
    blocking_pid: int
    blocking_query: str | None
    blocking_application_name: str | None
    blocking_state: str | None


async def list_locks(driver: SqlDriver, *, limit: int = DEFAULT_LOCK_LIMIT) -> list[LockInfo]:
    """List currently-held and waiting locks, joined with backend state.

    Ordered by ``(granted ASC, pid)`` so waiting locks float to the top
    — the entries most likely to need agent attention.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    rows = await driver.execute_query(
        "SELECT l.pid, l.locktype, l.mode, l.granted, "
        "       CASE WHEN l.relation IS NOT NULL "
        "            THEN format('%I.%I', n.nspname, c.relname) ELSE NULL END AS relation, "
        "       l.transactionid, l.virtualxid::text AS virtualxid, "
        "       a.application_name, a.state, a.wait_event_type, a.wait_event, "
        "       LEFT(a.query, 200) AS query "
        "FROM pg_locks l "
        "LEFT JOIN pg_class c ON c.oid = l.relation "
        "LEFT JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
        "WHERE l.pid IS NOT NULL "
        "ORDER BY l.granted ASC, l.pid "
        "LIMIT %s",
        params=[limit],
        force_readonly=True,
    )
    return [
        LockInfo(
            pid=int(row.cells["pid"]),
            locktype=str(row.cells["locktype"]),
            mode=str(row.cells["mode"]),
            granted=bool(row.cells["granted"]),
            relation=str(row.cells["relation"]) if row.cells["relation"] is not None else None,
            transactionid=(int(row.cells["transactionid"]) if row.cells["transactionid"] is not None else None),
            virtualxid=str(row.cells["virtualxid"]) if row.cells["virtualxid"] is not None else None,
            application_name=(
                str(row.cells["application_name"]) if row.cells["application_name"] is not None else None
            ),
            state=str(row.cells["state"]) if row.cells["state"] is not None else None,
            wait_event_type=(str(row.cells["wait_event_type"]) if row.cells["wait_event_type"] is not None else None),
            wait_event=str(row.cells["wait_event"]) if row.cells["wait_event"] is not None else None,
            query=str(row.cells["query"]) if row.cells["query"] is not None else None,
        )
        for row in rows or []
    ]


async def find_blocking_chains(driver: SqlDriver, *, limit: int = DEFAULT_BLOCKING_LIMIT) -> list[BlockingPair]:
    """Return ``(blocked, blocking)`` backend pairs via ``pg_blocking_pids``.

    ``pg_blocking_pids(pid)`` is the canonical source — it walks the
    lock graph and returns every PID whose lock is preventing the
    given PID from making progress. We unnest the result so each pair
    becomes one row.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    rows = await driver.execute_query(
        "SELECT blocked.pid AS blocked_pid, "
        "       LEFT(blocked.query, 200) AS blocked_query, "
        "       blocked.application_name AS blocked_application_name, "
        "       blocked.wait_event AS blocked_wait_event, "
        "       blocker.pid AS blocking_pid, "
        "       LEFT(blocker.query, 200) AS blocking_query, "
        "       blocker.application_name AS blocking_application_name, "
        "       blocker.state AS blocking_state "
        "FROM pg_stat_activity blocked "
        "JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(pid) ON TRUE "
        "JOIN pg_stat_activity blocker ON blocker.pid = bp.pid "
        "WHERE blocked.wait_event_type = 'Lock' "
        "ORDER BY blocked.pid "
        "LIMIT %s",
        params=[limit],
        force_readonly=True,
    )
    return [
        BlockingPair(
            blocked_pid=int(row.cells["blocked_pid"]),
            blocked_query=str(row.cells["blocked_query"]) if row.cells["blocked_query"] is not None else None,
            blocked_application_name=(
                str(row.cells["blocked_application_name"])
                if row.cells["blocked_application_name"] is not None
                else None
            ),
            blocked_wait_event=(
                str(row.cells["blocked_wait_event"]) if row.cells["blocked_wait_event"] is not None else None
            ),
            blocking_pid=int(row.cells["blocking_pid"]),
            blocking_query=str(row.cells["blocking_query"]) if row.cells["blocking_query"] is not None else None,
            blocking_application_name=(
                str(row.cells["blocking_application_name"])
                if row.cells["blocking_application_name"] is not None
                else None
            ),
            blocking_state=str(row.cells["blocking_state"]) if row.cells["blocking_state"] is not None else None,
        )
        for row in rows or []
    ]
