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


@dataclass(frozen=True, slots=True)
class ConnectionEncryption:
    """TLS status of the MCPg→PostgreSQL connection (+ cluster overview).

    The per-connection fields describe *this* backend's link, read from
    ``pg_stat_ssl`` for ``pg_backend_pid()``. ``cipher`` / ``version`` /
    ``bits`` are ``None`` when the connection is not encrypted. The
    ``*_connections`` counts summarise every backend visible in
    ``pg_stat_ssl`` — a non-superuser may only see its own row, so the
    counts are a lower bound under restricted privileges.
    """

    ssl: bool
    version: str | None
    cipher: str | None
    bits: int | None
    total_connections: int
    encrypted_connections: int
    unencrypted_connections: int


async def verify_connection_encryption(driver: SqlDriver) -> ConnectionEncryption:
    """Report whether the active connection is TLS-encrypted, plus a cluster tally.

    Composes with the startup TLS-enforcement check (which refuses
    insecure DSNs): this confirms, at runtime, that the negotiated
    connection actually came up encrypted and surfaces the protocol +
    cipher for an auditor.
    """
    own = await driver.execute_query(
        "SELECT ssl, version, cipher, bits FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
        force_readonly=True,
    )
    cell = own[0].cells if own else {}
    ssl_on = bool(cell.get("ssl"))

    tally = await driver.execute_query(
        "SELECT count(*) AS total, count(*) FILTER (WHERE ssl) AS encrypted FROM pg_stat_ssl",
        force_readonly=True,
    )
    counts = tally[0].cells if tally else {}
    total = int(counts.get("total") or 0)
    encrypted = int(counts.get("encrypted") or 0)

    # Normalise bits to int — keep the dataclass's int | None contract
    # even if a driver hands back Decimal/str for the key size.
    bits_value = cell.get("bits")
    bits = int(bits_value) if ssl_on and bits_value is not None else None

    return ConnectionEncryption(
        ssl=ssl_on,
        version=cell.get("version") if ssl_on else None,
        cipher=cell.get("cipher") if ssl_on else None,
        bits=bits,
        total_connections=total,
        encrypted_connections=encrypted,
        unencrypted_connections=total - encrypted,
    )


@dataclass(frozen=True, slots=True)
class IndexBuildProgress:
    """Progress snapshot for one in-flight ``CREATE INDEX`` operation.

    Populated from ``pg_stat_progress_create_index`` (PG12+, no
    extension required). ``phase`` is one of the labels PostgreSQL
    surfaces ("building index: scanning table", "loading tuples in
    tree", "merging partitions", etc.) — useful for human-readable
    status. ``progress_pct`` is computed from ``blocks_done /
    blocks_total`` when the planner provided a block count, otherwise
    from ``tuples_done / tuples_total``, otherwise ``None``: not every
    phase reports a meaningful denominator.

    ``schema``, ``relation``, and ``index_name`` are resolved by joining
    the progress view's relids with ``pg_class`` / ``pg_namespace``.
    They may be ``None`` for an index being built on a relation in a
    schema the caller can't see (rare, but possible with restricted
    privileges).
    """

    pid: int
    schema: str | None
    relation: str | None
    index_name: str | None
    command: str
    phase: str | None
    progress_pct: float | None
    blocks_done: int
    blocks_total: int
    tuples_done: int
    tuples_total: int
    partitions_done: int
    partitions_total: int


def _safe_pct(done: int, total: int) -> float | None:
    """Return ``done/total * 100`` capped to [0, 100], or ``None``."""
    if total <= 0:
        return None
    pct = (done / total) * 100.0
    if pct < 0.0:
        return 0.0
    if pct > 100.0:
        return 100.0
    return pct


async def monitor_index_build(driver: SqlDriver) -> list[IndexBuildProgress]:
    """Surface every active ``CREATE INDEX`` and its progress.

    Reads :pgview:`pg_stat_progress_create_index` and joins the relids
    with the catalog to render human-readable names. Useful next to
    :func:`list_active_queries` when an HNSW / IVFFlat build on a big
    table is taking longer than expected — agents can see which phase
    the build is in and how far it's come without dropping into
    ``psql``. Read-only.
    """
    rows = await driver.execute_query(
        "SELECT p.pid, "
        "       n.nspname AS schema, "
        "       c.relname AS relation, "
        "       ic.relname AS index_name, "
        "       p.command, "
        "       p.phase, "
        "       p.blocks_done, p.blocks_total, "
        "       p.tuples_done, p.tuples_total, "
        "       p.partitions_done, p.partitions_total "
        "FROM pg_stat_progress_create_index p "
        "LEFT JOIN pg_class c ON c.oid = p.relid "
        "LEFT JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_class ic ON ic.oid = p.index_relid "
        "ORDER BY p.pid",
        force_readonly=True,
    )
    out: list[IndexBuildProgress] = []
    for row in rows or []:
        cells = row.cells
        blocks_done = int(cells.get("blocks_done") or 0)
        blocks_total = int(cells.get("blocks_total") or 0)
        tuples_done = int(cells.get("tuples_done") or 0)
        tuples_total = int(cells.get("tuples_total") or 0)
        # Prefer block-level progress when PG populated it; fall through
        # to tuple-level otherwise. Returns None if neither is meaningful
        # (some early phases don't carry a denominator).
        progress_pct = _safe_pct(blocks_done, blocks_total)
        if progress_pct is None:
            progress_pct = _safe_pct(tuples_done, tuples_total)
        out.append(
            IndexBuildProgress(
                pid=int(cells["pid"]),
                schema=cells.get("schema"),
                relation=cells.get("relation"),
                index_name=cells.get("index_name"),
                command=str(cells.get("command") or ""),
                phase=cells.get("phase"),
                progress_pct=progress_pct,
                blocks_done=blocks_done,
                blocks_total=blocks_total,
                tuples_done=tuples_done,
                tuples_total=tuples_total,
                partitions_done=int(cells.get("partitions_done") or 0),
                partitions_total=int(cells.get("partitions_total") or 0),
            )
        )
    return out
