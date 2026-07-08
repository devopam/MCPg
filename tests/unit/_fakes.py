"""Shared test doubles for unit tests."""

from __future__ import annotations

from typing import Any

from mcpg.sql import SqlDriver


class FakePool:
    """Stand-in for the vendored DbConnPool that records lifecycle calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.connect_calls = 0
        self.close_calls = 0
        self._is_valid = False

    async def pool_connect(self, connection_url: str | None = None) -> object:
        self.connect_calls += 1
        if self.fail:
            raise ValueError("connection refused")
        self._is_valid = True
        return object()

    async def close(self) -> None:
        self.close_calls += 1
        self._is_valid = False

    @property
    def is_valid(self) -> bool:
        return self._is_valid


class FakeDriver:
    """Stand-in for SqlDriver that returns canned rows and records calls."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self._rows = rows or []
        self.fail = fail
        self.calls: list[tuple[str, Any, bool]] = []

    async def execute_query(
        self, query: str, params: list[Any] | None = None, force_readonly: bool = False
    ) -> list[SqlDriver.RowResult]:
        self.calls.append((query, params, force_readonly))
        if self.fail:
            raise RuntimeError("execution failed")
        return [SqlDriver.RowResult(cells=dict(row)) for row in self._rows]


class FakeRoutingDriver:
    """SqlDriver double that returns rows based on a query-substring match."""

    def __init__(self, routes: dict[str, list[dict[str, Any]]]) -> None:
        self._routes = routes
        self.calls: list[tuple[str, Any, bool]] = []

    async def execute_query(
        self, query: str, params: list[Any] | None = None, force_readonly: bool = False
    ) -> list[SqlDriver.RowResult]:
        self.calls.append((query, params, force_readonly))
        for substring, rows in self._routes.items():
            if substring in query:
                return [SqlDriver.RowResult(cells=dict(row)) for row in rows]
        return []


class FakeParamRoutingDriver:
    """SqlDriver double routing by (query-substring, params tuple).

    Routes are tried in insertion order; the first whose substring matches
    the query AND whose params equal the tuple key wins. A trailing route
    keyed by ``(substring, None)`` acts as a default for that substring
    regardless of params.
    """

    def __init__(self, routes: dict[tuple[str, tuple[Any, ...] | None], list[dict[str, Any]]]) -> None:
        self._routes = routes
        self.calls: list[tuple[str, Any, bool]] = []

    async def execute_query(
        self, query: str, params: list[Any] | None = None, force_readonly: bool = False
    ) -> list[SqlDriver.RowResult]:
        self.calls.append((query, params, force_readonly))
        params_key = tuple(params) if params is not None else ()
        for (substring, route_params), rows in self._routes.items():
            if substring not in query:
                continue
            if route_params is None or route_params == params_key:
                return [SqlDriver.RowResult(cells=dict(row)) for row in rows]
        return []


class FakeDatabase:
    """Stand-in for Database whose driver() returns a supplied FakeDriver."""

    def __init__(
        self,
        driver: FakeDriver,
        *,
        unmanaged_fail: bool = False,
        copy_rowcount: int | None = None,
        execute_many_rowcount: int | None = None,
    ) -> None:
        self._driver = driver
        self.is_connected = False
        self.unmanaged_fail = unmanaged_fail
        self.unmanaged: list[str] = []
        # Recorders for the COPY / executemany code paths.
        self.copy_calls: list[tuple[str, bytes]] = []
        self.execute_many_calls: list[tuple[str, list[tuple[Any, ...]]]] = []
        self._copy_rowcount = copy_rowcount
        self._execute_many_rowcount = execute_many_rowcount

    async def connect(self) -> None:
        self.is_connected = True

    async def close(self) -> None:
        self.is_connected = False

    def driver(self, database_id: str | None = None) -> FakeDriver:
        # ``database_id`` mirrors the real ``Database.driver`` signature
        # (multi-database selector, roadmap 13.1). The fake ignores it and
        # always returns the single canned driver; tests asserting selector
        # behaviour use a real ``Database`` with injected pools instead.
        del database_id
        return self._driver

    async def run_unmanaged(self, sql: str) -> None:
        self.unmanaged.append(sql)
        if self.unmanaged_fail:
            raise RuntimeError("maintenance failed")

    async def copy_from_stdin(self, sql: str, data: bytes) -> int:
        self.copy_calls.append((sql, data))
        # Default: count newlines in payload (matches a plain CSV row count
        # for the common case; tests can override via the ctor arg).
        return self._copy_rowcount if self._copy_rowcount is not None else data.count(b"\n")

    async def execute_many(self, sql: str, params_seq: Any) -> int:
        rows = [tuple(p) for p in params_seq]
        self.execute_many_calls.append((sql, rows))
        return self._execute_many_rowcount if self._execute_many_rowcount is not None else len(rows)

    async def __aenter__(self) -> FakeDatabase:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
