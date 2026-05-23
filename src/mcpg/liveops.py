"""Live-operations introspection: in-flight queries, waits, and blocking.

Reads ``pg_stat_activity`` to report what the server is doing right now.
Every query is read-only and parameterless.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver


@dataclass(frozen=True, slots=True)
class BackendActionResult:
    """The outcome of cancelling a query or terminating a backend.

    ``succeeded`` is ``False`` when PostgreSQL could not act on the PID —
    most often because no such backend exists.
    """

    pid: int
    action: str
    succeeded: bool


@dataclass(frozen=True, slots=True)
class ActiveQuery:
    """A query currently executing on the server.

    ``wait_event`` is ``type:event`` when the backend is waiting, else
    ``None``. ``blocked_by`` lists the PIDs holding locks this backend
    waits on — empty when the query is not blocked.
    """

    pid: int
    username: str | None
    application: str | None
    state: str | None
    wait_event: str | None
    duration_seconds: float | None
    query: str
    blocked_by: list[int]


async def list_active_queries(driver: SqlDriver) -> list[ActiveQuery]:
    """List the queries currently running on the server.

    Idle connections, PostgreSQL's background processes, and MCPg's own
    backend are excluded; the result is the work other clients are doing.
    """
    rows = await driver.execute_query(
        "SELECT pid, usename AS username, application_name AS application, "
        "state, "
        "CASE WHEN wait_event_type IS NULL THEN NULL "
        "ELSE wait_event_type || ':' || COALESCE(wait_event, '') END AS wait_event, "
        "EXTRACT(EPOCH FROM (now() - query_start))::double precision AS duration_seconds, "
        "query, pg_blocking_pids(pid) AS blocked_by "
        "FROM pg_stat_activity "
        "WHERE backend_type = 'client backend' "
        "AND pid <> pg_backend_pid() "
        "AND state IS DISTINCT FROM 'idle' "
        "ORDER BY query_start NULLS LAST",
        force_readonly=True,
    )
    return [
        ActiveQuery(
            pid=row.cells["pid"],
            username=row.cells["username"],
            application=row.cells["application"],
            state=row.cells["state"],
            wait_event=row.cells["wait_event"],
            duration_seconds=row.cells["duration_seconds"],
            query=row.cells["query"],
            blocked_by=list(row.cells["blocked_by"]),
        )
        for row in rows or []
    ]


async def cancel_query(driver: SqlDriver, pid: int) -> BackendActionResult:
    """Cancel the query currently running on a backend.

    Sends a cancel signal via ``pg_cancel_backend``; the connection itself
    stays open. ``succeeded`` is ``False`` if no such backend exists.
    """
    rows = await driver.execute_query(
        "SELECT pg_cancel_backend(%s) AS ok",
        params=[pid],
        force_readonly=True,
    )
    succeeded = bool((rows or [])[0].cells["ok"])
    return BackendActionResult(pid=pid, action="cancel_query", succeeded=succeeded)


async def terminate_backend(driver: SqlDriver, pid: int) -> BackendActionResult:
    """Terminate a backend, closing its connection.

    Sends a terminate signal via ``pg_terminate_backend``. ``succeeded`` is
    ``False`` if no such backend exists.
    """
    rows = await driver.execute_query(
        "SELECT pg_terminate_backend(%s) AS ok",
        params=[pid],
        force_readonly=True,
    )
    succeeded = bool((rows or [])[0].cells["ok"])
    return BackendActionResult(pid=pid, action="terminate_backend", succeeded=succeeded)
