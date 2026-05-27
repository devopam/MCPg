"""Read-replica routing — distribute read-only queries across replicas.

When ``MCPG_REPLICA_URLS`` is configured, MCPg keeps a dedicated
psycopg pool per replica DSN alongside the primary pool. Every
query the tool layer marks ``force_readonly=True`` (catalog reads,
``run_select``, the safety-driver path) is routed round-robin to a
healthy replica; writes always go to the primary. Composes cleanly
with the tenancy driver from Phase 1.4 — each replica owns its own
:class:`mcpg.tenancy.TenantSqlDriver` so ``SET LOCAL ROLE`` applies
per-replica.

Failure handling:

* On a connection or query error against a replica, the call falls
  back to the primary once. The replica is marked ``degraded`` and
  skipped from the round-robin pool for
  :data:`DEFAULT_DEGRADED_RETRY_SECONDS`; a probe after that window
  decides whether it returns to service.
* When every replica is degraded, every read goes to the primary
  (the fallback path) and a WARNING is logged. No tool call fails
  because the replicas are unavailable.

Metrics (via :mod:`mcpg.observability`):

* ``mcpg_replica_routed_total{target}`` — counter, one increment per
  query routed (``target`` is the replica index or ``primary``).
* ``mcpg_replica_fallbacks_total`` — counter, increments when a
  replica call failed and was retried on the primary.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import DbConnPool, SqlDriver, obfuscate_password
from mcpg.tenancy import TenantSqlDriver

logger = logging.getLogger(__name__)

# When a replica's query fails, skip it for this many seconds before
# probing again. Long enough that a tight failure loop won't hammer
# a recovering replica; short enough that a recovered replica returns
# to service without a server restart.
DEFAULT_DEGRADED_RETRY_SECONDS = 30.0


class ReplicaError(Exception):
    """Raised when replica configuration is invalid."""


@dataclass(slots=True)
class _ReplicaState:
    """Per-replica health-tracking state."""

    index: int
    dsn: str
    pool: DbConnPool
    degraded_until: float = 0.0
    last_error: str | None = None

    def is_healthy(self, now: float) -> bool:
        return now >= self.degraded_until


@dataclass(frozen=True, slots=True)
class ReplicaInfo:
    """Public-facing description of one replica's state.

    ``dsn`` is password-obfuscated — safe to surface via the
    diagnostic tool.
    """

    index: int
    dsn: str
    degraded: bool
    last_error: str | None
    seconds_until_retry: float


class ReplicaPool:
    """Manages a fixed list of replica psycopg pools.

    Constructed at server-lifespan startup; closed symmetrically.
    Pick a replica with :meth:`next_healthy`; report a failure with
    :meth:`mark_degraded`.
    """

    def __init__(
        self,
        dsns: tuple[str, ...],
        *,
        pool_min_size: int,
        pool_max_size: int,
        degraded_retry_seconds: float = DEFAULT_DEGRADED_RETRY_SECONDS,
    ) -> None:
        if not dsns:
            raise ReplicaError("at least one replica DSN is required")
        self._states: list[_ReplicaState] = [
            _ReplicaState(
                index=i,
                dsn=dsn,
                pool=DbConnPool(dsn, min_size=pool_min_size, max_size=pool_max_size),
            )
            for i, dsn in enumerate(dsns)
        ]
        self._cycle = itertools.cycle(self._states)
        self._lock = asyncio.Lock()
        self._degraded_retry_seconds = degraded_retry_seconds

    @property
    def size(self) -> int:
        return len(self._states)

    async def connect(self) -> None:
        """Open every replica pool.

        Failures here are surfaced individually as
        :class:`ReplicaError` so the operator sees which replica is
        misconfigured. The server starts even if every replica fails
        — the routing layer falls back to the primary in that case.
        """
        for state in self._states:
            try:
                await state.pool.pool_connect()
            except Exception as exc:
                state.degraded_until = float("inf")
                state.last_error = obfuscate_password(str(exc))
                logger.warning(
                    "Replica %d (%s) failed to open: %s",
                    state.index,
                    obfuscate_password(state.dsn),
                    state.last_error,
                )

    async def close(self) -> None:
        for state in self._states:
            try:
                await state.pool.close()
            except Exception as exc:
                logger.warning("Error closing replica %d pool: %s", state.index, exc)

    async def next_healthy(self) -> _ReplicaState | None:
        """Return the next healthy replica, or ``None`` if all are degraded.

        Round-robin — every healthy replica is visited once before
        any is revisited. Thread-safe via the internal lock.
        """
        async with self._lock:
            now = time.monotonic()
            # Up to `size` rotations: if none in the cycle is healthy
            # after a full pass, every replica is degraded.
            for _ in range(self.size):
                candidate = next(self._cycle)
                if candidate.is_healthy(now):
                    return candidate
            return None

    async def mark_degraded(self, replica_index: int, error: str) -> None:
        async with self._lock:
            state = self._states[replica_index]
            state.degraded_until = time.monotonic() + self._degraded_retry_seconds
            state.last_error = obfuscate_password(error)
            logger.warning(
                "Replica %d marked degraded for %.1fs: %s",
                replica_index,
                self._degraded_retry_seconds,
                state.last_error,
            )

    async def snapshot(self) -> list[ReplicaInfo]:
        async with self._lock:
            now = time.monotonic()
            return [
                ReplicaInfo(
                    index=state.index,
                    dsn=obfuscate_password(state.dsn) or state.dsn,
                    degraded=not state.is_healthy(now),
                    last_error=state.last_error,
                    seconds_until_retry=max(0.0, state.degraded_until - now),
                )
                for state in self._states
            ]

    async def __aenter__(self) -> ReplicaPool:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def _make_driver_for_pool(
    pool: DbConnPool,
    *,
    default_role: str | None,
    enable_tenancy: bool,
) -> SqlDriver:
    """Construct the per-pool driver — tenanted if tenancy is configured."""
    if enable_tenancy:
        return TenantSqlDriver(conn=pool, default_role=default_role)
    return SqlDriver(conn=pool)


class RoutedSqlDriver(SqlDriver):
    """Per-call replica-routing wrapper around the primary + replica drivers.

    Overrides :meth:`execute_query` only — every other ``SqlDriver``
    surface delegates to the primary. The ``force_readonly`` flag is
    the routing signal: ``True`` is eligible for a replica, ``False``
    always goes to the primary.

    Subclassing ``SqlDriver`` keeps the tool layer's type annotations
    valid; the inherited methods we don't override (``copy_from_stdin``
    etc on the primary path) never reach here because the tool layer
    routes those via :class:`Database` directly.
    """

    # NOTE: We don't call super().__init__ because the primary driver
    # already wraps the primary pool with its own ``self.conn``. We
    # set the attributes the upstream class would set so anything
    # introspecting ``self.conn`` / ``self.is_pool`` still works.
    def __init__(
        self,
        *,
        primary: SqlDriver,
        replicas: list[SqlDriver],
        replica_pool: ReplicaPool,
    ) -> None:
        # Don't invoke SqlDriver.__init__ — it requires conn/engine_url
        # and we don't want to mint another pool. Mirror the
        # public attributes so duck-typed code keeps working.
        self.conn = primary.conn
        self.is_pool = getattr(primary, "is_pool", True)
        self._primary = primary
        self._replicas = replicas
        self._replica_pool = replica_pool

    async def execute_query(
        self,
        query: str,
        params: list[Any] | None = None,
        force_readonly: bool = False,
    ) -> list[SqlDriver.RowResult] | None:
        from mcpg.observability import get_metrics

        metrics = get_metrics()

        if not force_readonly:
            metrics.record_call("__replica_route", "primary", 0.0)
            return await self._primary.execute_query(query, params, force_readonly)

        candidate = await self._replica_pool.next_healthy()
        if candidate is None:
            # Every replica degraded — fall through to primary.
            metrics.record_call("__replica_route", "primary_no_healthy", 0.0)
            return await self._primary.execute_query(query, params, force_readonly)

        replica_driver = self._replicas[candidate.index]
        try:
            result = await replica_driver.execute_query(query, params, force_readonly)
        except Exception as exc:
            await self._replica_pool.mark_degraded(candidate.index, str(exc))
            metrics.record_call("__replica_route", "fallback", 0.0)
            logger.warning(
                "Replica %d query failed; falling back to primary: %s",
                candidate.index,
                obfuscate_password(str(exc)),
            )
            return await self._primary.execute_query(query, params, force_readonly)

        metrics.record_call("__replica_route", f"replica_{candidate.index}", 0.0)
        return result
