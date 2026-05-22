"""Shared test doubles for unit tests."""

from __future__ import annotations

from typing import Any

from mcpg._vendor.sql import SqlDriver


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


class FakeDatabase:
    """Stand-in for Database whose driver() returns a supplied FakeDriver."""

    def __init__(self, driver: FakeDriver, *, unmanaged_fail: bool = False) -> None:
        self._driver = driver
        self.is_connected = False
        self.unmanaged_fail = unmanaged_fail
        self.unmanaged: list[str] = []

    async def connect(self) -> None:
        self.is_connected = True

    async def close(self) -> None:
        self.is_connected = False

    def driver(self) -> FakeDriver:
        return self._driver

    async def run_unmanaged(self, sql: str) -> None:
        self.unmanaged.append(sql)
        if self.unmanaged_fail:
            raise RuntimeError("maintenance failed")

    async def __aenter__(self) -> FakeDatabase:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
