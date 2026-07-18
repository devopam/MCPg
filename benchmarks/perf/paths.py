"""The measurement paths.

Each runner times **one execution** of a query with ``time.perf_counter_ns()``
and returns the elapsed nanoseconds; the runner (``runner.py``) calls it N
times. All paths share one asyncio event loop; the DB-touching paths point at
the same warmed pool / same DSN so event-loop and DB-work cost is common-mode
and cancels out of the *differences* we report.

* :class:`NativeRunner` — the fair floor. A single persistent ``psycopg``
  connection replicating the **exact** transaction envelope MCPg issues
  (``BEGIN TRANSACTION READ ONLY`` → execute → fetchall → ``[dict(row)]`` →
  ``ROLLBACK``) but with **no** pglast validation, no ``SafeSqlDriver``
  allocation, no pool checkout. Same DB work, none of MCPg's overhead — which
  is what makes ``t_db == native`` a meaningful assertion. (Not ``psql -c``,
  which would pay per-call process + connection startup.)
* :class:`ServerSideRunner` — MCPg in-process: the real ``run_select`` over the
  real ``SafeSqlDriver`` + real pool, bypassing the MCP transport. Includes the
  genuine per-call ``SafeSqlDriver`` allocation and result serialization — those
  are real MCPg cost and are never optimized away.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from mcpg.database import Database
from mcpg.query import run_select


class PathRunner(Protocol):
    """One measurement path. ``run_once`` returns elapsed nanoseconds."""

    async def run_once(self, sql: str, *, max_rows: int) -> int: ...


class NativeRunner:
    """Raw ``psycopg`` baseline replicating MCPg's transaction envelope."""

    def __init__(self, connection: psycopg.AsyncConnection[Any]) -> None:
        self._conn = connection

    @classmethod
    async def connect(cls, dsn: str) -> NativeRunner:
        # autocommit=True so the explicit BEGIN/ROLLBACK envelope (matching
        # MCPg's SqlDriver) is what drives the transaction, with no implicit
        # transaction wrapping to double-count.
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def run_once(self, sql: str, *, max_rows: int) -> int:
        conn = self._conn
        start = time.perf_counter_ns()
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("BEGIN TRANSACTION READ ONLY")
            try:
                await cur.execute(sql)
                rows = await cur.fetchall()
                # Match run_select's max_rows truncation + dict materialization
                # so the serialization work compared is like-for-like.
                _ = [dict(r) for r in rows[:max_rows]]
            finally:
                # Always close the transaction. A failing query on this
                # persistent connection would otherwise leave it in an aborted
                # state and poison every subsequent measurement. ROLLBACK is
                # valid (and the correct recovery) even after an error.
                await cur.execute("ROLLBACK")
        return time.perf_counter_ns() - start


class ServerSideRunner:
    """MCPg in-process: real ``run_select`` over the real driver + pool."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def run_once(self, sql: str, *, max_rows: int) -> int:
        driver = self._database.driver()
        start = time.perf_counter_ns()
        await run_select(driver, sql, max_rows=max_rows)
        return time.perf_counter_ns() - start
