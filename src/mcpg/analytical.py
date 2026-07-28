"""Long-running analytical read path — an isolated pool, capped concurrency.

``run_select`` runs on the main connection pool with a short timeout so an agent
can't pin a connection with a runaway query. Genuine analytical queries (large
aggregations, multi-table joins, window functions) need more time — but giving
*every* read a long budget would let a handful of slow queries exhaust the small
main pool (``MCPG_POOL_MAX_SIZE``, default 5) and starve the fast-path tools.

:class:`AnalyticalRunner` isolates that. It owns a **separate, small connection
pool** sized to ``MCPG_ANALYTICAL_MAX_CONCURRENCY`` with an elevated
``statement_timeout`` ceiling. Each analytical query runs through the *same*
driver stack as ``run_select`` — the ``pglast`` allowlist (``SafeSqlDriver``),
the per-request ``SET LOCAL ROLE`` tenancy (``TenantSqlDriver``), and the
read-only transaction — so RLS / role scoping / validation are **identical**;
only the pool and the timeout differ. Because the pool holds at most
``max_concurrency`` connections, that many analytical queries can run at once and
they consume **zero** slots of the main pool.

The per-call ``timeout_ms`` is clamped to ``[1, MCPG_ANALYTICAL_MAX_TIMEOUT_MS]``
and enforced as the client-side wall-clock cap (``SafeSqlDriver``), with the
pool's ``statement_timeout`` as the Postgres-side ceiling. Primary database only
for now (secondary/replica routing is a follow-up).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mcpg import query
from mcpg.database import Database
from mcpg.query import DEFAULT_MAX_ROWS

if TYPE_CHECKING:
    from mcpg.config import Settings
    from mcpg.query import QueryResult


class AnalyticalRunner:
    """Owns an isolated pool for long-running read-only analytical queries."""

    def __init__(self, settings: Settings) -> None:
        self._default_timeout_ms = settings.analytical_timeout_ms
        self._max_timeout_ms = settings.analytical_max_timeout_ms
        # A dedicated, primary-only pool sized to the concurrency cap, with the
        # analytical timeout ceiling. Replicas/secondaries are stripped — the
        # analytical path targets the primary. Tenancy (default_role /
        # allowed_roles) is preserved so RLS / SET LOCAL ROLE still apply.
        analytical_settings = replace(
            settings,
            pool_min_size=1,
            pool_max_size=settings.analytical_max_concurrency,
            statement_timeout_ms=settings.analytical_max_timeout_ms,
            replica_urls=(),
            secondary_database_urls=(),
        )
        self._db = Database(analytical_settings)

    async def start(self) -> None:
        await self._db.connect()

    async def close(self) -> None:
        await self._db.close()

    def _resolve_timeout_s(self, requested_ms: int | None) -> float:
        ms = self._default_timeout_ms if requested_ms is None else int(requested_ms)
        ms = max(1, min(ms, self._max_timeout_ms))
        return ms / 1000.0

    async def run(
        self,
        sql: str,
        *,
        timeout_ms: int | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        work_mem: str | None = None,
    ) -> QueryResult:
        """Validate + run ``sql`` on the isolated pool with the clamped budget.

        Delegates to :func:`mcpg.query.run_select` (or ``run_select_tuned`` when
        ``work_mem`` is given) so validation, tenancy, and result shaping are
        exactly the fast-path behaviour — the isolated pool provides the
        concurrency cap and the elevated timeout does the rest.
        """
        timeout_s = self._resolve_timeout_s(timeout_ms)
        driver = self._db.driver()
        if work_mem:
            return await query.run_select_tuned(driver, sql, work_mem=work_mem, timeout=timeout_s, max_rows=max_rows)
        return await query.run_select(driver, sql, timeout=timeout_s, max_rows=max_rows)
