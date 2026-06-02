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


@dataclass(frozen=True, slots=True)
class BlockingChainDetail:
    """Detailed state information for a backend in a blocking chain."""

    pid: int
    query: str | None
    application_name: str | None
    wait_event: str | None
    state: str | None


@dataclass(frozen=True, slots=True)
class BlockingGraphReport:
    """The reconstructed lock-wait graph report.

    Contains all detected simple deadlock cycles, linear blocking paths
    running to root blockers or cycle points, identified root blocking PIDs,
    and a pre-rendered Mermaid flowchart representing the dependency graph.
    """

    cycles: list[list[int]]
    paths: list[list[int]]
    roots: list[int]
    nodes: dict[int, BlockingChainDetail]
    mermaid: str


async def walk_blocking_chains(driver: SqlDriver, *, limit: int = DEFAULT_BLOCKING_LIMIT) -> BlockingGraphReport:
    """Analyze the PostgreSQL lock-wait graph.

    Detects deadlock cycles, traces blocking paths to their root blockers,
    and renders a Mermaid flowchart representing the lock dependency graph.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")

    rows = await driver.execute_query(
        "SELECT blocked.pid AS blocked_pid, "
        "       LEFT(blocked.query, 100) AS blocked_query, "
        "       blocked.application_name AS blocked_application_name, "
        "       blocked.wait_event AS blocked_wait_event, "
        "       blocked.state AS blocked_state, "
        "       blocker.pid AS blocking_pid, "
        "       LEFT(blocker.query, 100) AS blocking_query, "
        "       blocker.application_name AS blocking_application_name, "
        "       blocker.wait_event AS blocking_wait_event, "
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

    nodes: dict[int, BlockingChainDetail] = {}
    adj: dict[int, list[int]] = {}
    in_degrees: dict[int, int] = {}
    out_degrees: dict[int, int] = {}
    all_pids: set[int] = set()

    for row in rows or []:
        b_pid = int(row.cells["blocked_pid"])
        blk_pid = int(row.cells["blocking_pid"])

        all_pids.add(b_pid)
        all_pids.add(blk_pid)

        adj.setdefault(b_pid, []).append(blk_pid)

        out_degrees[b_pid] = out_degrees.get(b_pid, 0) + 1
        in_degrees[blk_pid] = in_degrees.get(blk_pid, 0) + 1

        if b_pid not in nodes:
            nodes[b_pid] = BlockingChainDetail(
                pid=b_pid,
                query=str(row.cells["blocked_query"]) if row.cells["blocked_query"] is not None else None,
                application_name=(
                    str(row.cells["blocked_application_name"])
                    if row.cells["blocked_application_name"] is not None
                    else None
                ),
                wait_event=(
                    str(row.cells["blocked_wait_event"]) if row.cells["blocked_wait_event"] is not None else None
                ),
                state=str(row.cells["blocked_state"]) if row.cells["blocked_state"] is not None else None,
            )

        if blk_pid not in nodes:
            nodes[blk_pid] = BlockingChainDetail(
                pid=blk_pid,
                query=str(row.cells["blocking_query"]) if row.cells["blocking_query"] is not None else None,
                application_name=(
                    str(row.cells["blocking_application_name"])
                    if row.cells["blocking_application_name"] is not None
                    else None
                ),
                wait_event=(
                    str(row.cells["blocking_wait_event"]) if row.cells["blocking_wait_event"] is not None else None
                ),
                state=str(row.cells["blocking_state"]) if row.cells["blocking_state"] is not None else None,
            )

    # 1. Root blockers are PIDs that block others (in_degree > 0) but are not blocked themselves (out_degree == 0)
    roots = sorted([pid for pid in all_pids if in_degrees.get(pid, 0) > 0 and out_degrees.get(pid, 0) == 0])

    # 2. DFS simple cycle detection
    cycles: list[list[int]] = []
    visited_cycles: set[int] = set()
    path: list[int] = []
    path_set: set[int] = set()

    def dfs_cycles(node: int) -> None:
        if node in path_set:
            idx = path.index(node)
            cycle = path[idx:]
            min_val = min(cycle)
            min_idx = cycle.index(min_val)
            normalized = cycle[min_idx:] + cycle[:min_idx]
            full_cycle = [*normalized, min_val]
            if full_cycle not in cycles:
                cycles.append(full_cycle)
            return

        if node in visited_cycles:
            return

        path.append(node)
        path_set.add(node)

        for neighbor in adj.get(node, []):
            dfs_cycles(neighbor)

        path.pop()
        path_set.remove(node)
        visited_cycles.add(node)

    for pid in sorted(all_pids):
        dfs_cycles(pid)

    # 3. Path tracing starting from leaf nodes (in-degree == 0, out-degree > 0)
    leaves = [pid for pid in all_pids if in_degrees.get(pid, 0) == 0 and out_degrees.get(pid, 0) > 0]
    paths: list[list[int]] = []

    def trace_path(node: int, current_path: list[int]) -> None:
        neighbors = adj.get(node, [])
        if not neighbors:
            paths.append(current_path)
            return

        for neighbor in neighbors:
            if neighbor in current_path:
                paths.append([*current_path, neighbor])
                continue
            trace_path(neighbor, [*current_path, neighbor])

    for leaf in sorted(leaves):
        trace_path(leaf, [leaf])

    # 4. Mermaid Flowchart construction
    mermaid_lines = ["graph TD"]
    mermaid_lines.append("  classDef root fill:#ff9999,stroke:#333,stroke-width:2px;")
    mermaid_lines.append("  classDef cycle fill:#ffff99,stroke:#333,stroke-width:2px;")

    cycle_pids = {pid for cyc in cycles for pid in cyc}

    # Define nodes
    for pid in sorted(all_pids):
        detail = nodes[pid]
        query_snippet = ""
        if detail.query:
            q = detail.query.strip().replace("\n", " ").replace('"', "'")
            if len(q) > 60:
                q = q[:57] + "..."
            query_snippet = f"<br/>`{q}`"

        app_str = f" ({detail.application_name})" if detail.application_name else ""
        state_str = f" [{detail.state or 'unknown'}]"
        label = f"PID {pid}{app_str}{state_str}{query_snippet}"
        label_escaped = label.replace('"', '\\"')

        mermaid_lines.append(f'  {pid}["{label_escaped}"]')

    # Define edges
    for row in rows or []:
        b_pid = int(row.cells["blocked_pid"])
        blk_pid = int(row.cells["blocking_pid"])
        wait_evt = str(row.cells["blocked_wait_event"]) if row.cells["blocked_wait_event"] is not None else "Lock"
        mermaid_lines.append(f'  {b_pid} -->|"{wait_evt}"| {blk_pid}')

    # Apply style classes
    for r in roots:
        mermaid_lines.append(f"  class {r} root;")
    for c_pid in sorted(cycle_pids):
        if c_pid not in roots:
            mermaid_lines.append(f"  class {c_pid} cycle;")

    mermaid_str = "\n".join(mermaid_lines)

    return BlockingGraphReport(
        cycles=cycles,
        paths=paths,
        roots=roots,
        nodes=nodes,
        mermaid=mermaid_str,
    )
