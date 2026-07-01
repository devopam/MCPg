"""Transactional DDL dry-run — roadmap 2.8.

``dry_run_ddl`` runs a candidate DDL statement inside a transaction with a
bounded ``lock_timeout``, measures what it would cost (lock modes held,
wall-clock duration, WAL bytes generated), then **always rolls back** so
nothing persists. It exists so an operator can see the blast radius of an
``ALTER TABLE`` before committing to it on a busy table.

The honesty caveat — stated in the tool description and worth repeating:
rollback undoes the *catalog* change, but a **rewriting** DDL (e.g. a type
change that rewrites every row, an ``ADD COLUMN ... DEFAULT`` on old PG)
still does the rewrite work — and holds the lock — for the duration before
the rollback. The dry run measures impact by incurring it. ``CONCURRENTLY``
/ non-transactional statements cannot run inside the wrapping transaction at
all, so they are rejected up front as ineligible.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

# Imported at runtime (not under TYPE_CHECKING) so the return/param
# annotations resolve via typing.get_type_hints — the return-shape contract
# test relies on that to classify dry_run_ddl as a typed dataclass return.
from mcpg.database import Database
from mcpg.migrations import _NON_TRANSACTIONAL_SQL

# PostgreSQL SQLSTATE for a statement that hit its lock_timeout.
_LOCK_NOT_AVAILABLE = "55P03"

# Default per-statement lock acquisition budget. Keeps the dry run from
# blocking behind a long-held lock on a busy table — if the DDL can't get
# its lock inside this window we report lock_timed_out rather than wait.
DEFAULT_LOCK_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class DdlLock:
    """A single lock the dry-run backend held while running the DDL."""

    locktype: str
    mode: str
    granted: bool
    relation: str | None


@dataclass(frozen=True)
class DdlDryRunResult:
    """The outcome of :func:`dry_run_ddl`.

    ``eligible`` is ``False`` for CONCURRENTLY / non-transactional DDL,
    which can't run inside the wrapping transaction. ``executed`` is
    ``True`` when the DDL actually ran (it was then rolled back, recorded
    in ``rolled_back``). ``lock_timed_out`` is set when acquisition hit the
    ``lock_timeout``. ``error`` carries any other database error message.
    """

    eligible: bool
    executed: bool
    rolled_back: bool
    lock_timed_out: bool
    locks: list[DdlLock]
    max_lock_mode: str | None
    duration_ms: float | None
    wal_bytes: int | None
    error: str | None
    detail: str


# Strength ordering of the lock modes a DDL can take, weakest → strongest.
# Used to pick ``max_lock_mode`` — the strongest lock the statement held,
# which is what determines how much concurrent traffic it would block.
_LOCK_MODE_RANK = {
    "AccessShareLock": 1,
    "RowShareLock": 2,
    "RowExclusiveLock": 3,
    "ShareUpdateExclusiveLock": 4,
    "ShareLock": 5,
    "ShareRowExclusiveLock": 6,
    "ExclusiveLock": 7,
    "AccessExclusiveLock": 8,
}


def _strongest(modes: list[str]) -> str | None:
    """Return the strongest lock mode by :data:`_LOCK_MODE_RANK`."""
    ranked = [(mode, _LOCK_MODE_RANK.get(mode, 0)) for mode in modes]
    if not ranked:
        return None
    return max(ranked, key=lambda pair: pair[1])[0]


# An acquirer is an async context manager factory yielding a raw psycopg
# connection. Defaulting it to the Database's pool keeps production simple;
# making it an explicit seam lets a unit test inject a fake connection
# without standing up a real pool. Behaviour is identical either way — the
# seam only changes where the connection comes from.
AcquireConnection = Callable[[], "AbstractAsyncContextManager[Any]"]


@asynccontextmanager
async def _default_acquire(database: Database) -> AsyncIterator[Any]:
    """Acquire a raw pooled connection from ``database``'s primary pool."""
    pool = await database._pool.pool_connect()
    async with pool.connection() as conn:
        yield conn


async def dry_run_ddl(
    database: Database,
    ddl_sql: str,
    *,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    acquire: AcquireConnection | None = None,
) -> DdlDryRunResult:
    """Run ``ddl_sql`` in a rolled-back transaction and measure its impact.

    Rejects CONCURRENTLY / non-transactional DDL up front (``eligible=False``)
    since it can't run inside the wrapping transaction. Otherwise: sets a
    bounded ``lock_timeout``, runs the DDL, records the lock modes the
    backend holds, the wall-clock duration, and the WAL bytes generated,
    then **rolls back**. Lock-timeout (SQLSTATE 55P03) and other database
    errors are returned as structured results — this never raises out.

    Nothing persists. But note a *rewriting* DDL still performs the rewrite
    (and holds its lock) before the rollback — the cost is incurred to be
    measured.

    Args:
        database: The Database whose primary pool supplies the connection.
        ddl_sql: The single DDL statement to dry-run.
        lock_timeout_ms: Lock-acquisition budget, in milliseconds.
        acquire: Optional connection-acquirer override (test seam). When
            ``None`` a connection is taken from ``database``'s primary pool.
    """
    if not ddl_sql.strip():
        return DdlDryRunResult(
            eligible=False,
            executed=False,
            rolled_back=False,
            lock_timed_out=False,
            locks=[],
            max_lock_mode=None,
            duration_ms=None,
            wal_bytes=None,
            error=None,
            detail="ddl_sql must not be empty",
        )

    if _NON_TRANSACTIONAL_SQL.search(ddl_sql):
        return DdlDryRunResult(
            eligible=False,
            executed=False,
            rolled_back=False,
            lock_timed_out=False,
            locks=[],
            max_lock_mode=None,
            duration_ms=None,
            wal_bytes=None,
            error=None,
            detail=(
                "DDL uses a CONCURRENTLY / non-transactional statement (e.g. "
                "CREATE INDEX CONCURRENTLY, VACUUM, ALTER SYSTEM) that cannot "
                "run inside the wrapping transaction; it cannot be dry-run. "
                "Use monitor_index_build for concurrent builds, or run it "
                "directly via run_ddl."
            ),
        )

    acquirer = acquire if acquire is not None else (lambda: _default_acquire(database))

    locks: list[DdlLock] = []
    max_lock_mode: str | None = None
    duration_ms: float | None = None
    wal_bytes: int | None = None
    executed = False
    rolled_back = False
    lock_timed_out = False
    error: str | None = None

    async with acquirer() as conn:
        try:
            async with conn.cursor() as cur:
                # set_config(..., is_local=true) is the parameterized equivalent
                # of `SET LOCAL lock_timeout` — `SET` itself can't take a bind
                # parameter, so this avoids building SQL from the value at all.
                await cur.execute("SELECT set_config('lock_timeout', %s, true)", [f"{int(lock_timeout_ms)}ms"])
                await cur.execute("SELECT pg_current_wal_lsn() AS lsn")
                baseline_lsn = (await cur.fetchone())[0]

                started = time.monotonic()
                try:
                    await cur.execute(ddl_sql)
                    duration_ms = (time.monotonic() - started) * 1000.0
                    executed = True
                except Exception as exc:
                    duration_ms = (time.monotonic() - started) * 1000.0
                    sqlstate = getattr(exc, "sqlstate", None) or getattr(getattr(exc, "diag", None), "sqlstate", None)
                    if sqlstate == _LOCK_NOT_AVAILABLE:
                        lock_timed_out = True
                    else:
                        error = str(exc)

                # Even on a lock-timeout / error the backend may hold (or
                # have tried for) locks worth reporting. pg_locks reflects
                # the current backend's locks for this transaction.
                if executed:
                    await cur.execute(
                        "SELECT l.locktype, l.mode, l.granted, c.relname AS relation "
                        "FROM pg_locks l "
                        "LEFT JOIN pg_class c ON c.oid = l.relation "
                        "WHERE l.pid = pg_backend_pid() AND l.locktype <> 'virtualxid'"
                    )
                    for row in await cur.fetchall():
                        locks.append(
                            DdlLock(
                                locktype=row[0],
                                mode=row[1],
                                granted=bool(row[2]),
                                relation=row[3],
                            )
                        )
                    max_lock_mode = _strongest([lock.mode for lock in locks])

                    await cur.execute("SELECT pg_current_wal_lsn() AS lsn")
                    now_lsn = (await cur.fetchone())[0]
                    await cur.execute(
                        "SELECT pg_wal_lsn_diff(%s, %s) AS bytes",
                        (now_lsn, baseline_lsn),
                    )
                    wal_bytes = int((await cur.fetchone())[0])
        except Exception as exc:
            if error is None and not lock_timed_out:
                error = str(exc)
        finally:
            try:
                await conn.rollback()
                rolled_back = True
            except Exception:
                rolled_back = False

    if not executed and lock_timed_out:
        detail = (
            f"DDL could not acquire its lock within {lock_timeout_ms}ms "
            "(lock_timeout); rolled back. The table is likely busy."
        )
    elif not executed and error is not None:
        detail = f"DDL failed before commit: {error}; rolled back"
    else:
        detail = (
            f"DDL executed in {duration_ms:.1f}ms holding up to "
            f"{max_lock_mode or 'no'} lock, generated {wal_bytes or 0} WAL bytes, "
            "then rolled back (nothing persisted)"
        )

    return DdlDryRunResult(
        eligible=True,
        executed=executed,
        rolled_back=rolled_back,
        lock_timed_out=lock_timed_out,
        locks=locks,
        max_lock_mode=max_lock_mode,
        duration_ms=duration_ms,
        wal_bytes=wal_bytes,
        error=error,
        detail=detail,
    )
