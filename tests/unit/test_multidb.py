"""Tests for the multi-database selector (roadmap 13.1).

Covers:

* :class:`ReadOnlySqlDriver` — every query runs inside ``BEGIN TRANSACTION
  READ ONLY`` regardless of the caller's ``force_readonly`` flag, and a write
  is rejected by the (simulated) PostgreSQL read-only transaction.
* :class:`mcpg.database.Database` multi-pool construction, the
  ``driver(database_id=…)`` selector (primary unchanged, secondary read-only,
  unknown id error), ``database_ids()`` ordering, and ``describe_databases``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from _fakes import FakePool

from mcpg._vendor.sql import SqlDriver
from mcpg.config import load_settings
from mcpg.database import Database, DatabaseError
from mcpg.multidb import (
    PRIMARY_DATABASE_ID,
    DatabaseList,
    ReadOnlySqlDriver,
    make_read_only_driver,
)
from mcpg.replicas import TimeoutSqlDriver

_PRIMARY = "postgresql://u:p@localhost/db"


# ---------------------------------------------------------------------------
# Fake psycopg connection that simulates a read-only transaction.
# ---------------------------------------------------------------------------


class _ReadOnlyError(Exception):
    """Stand-in for psycopg's read-only-transaction error (SQLSTATE 25006)."""


class _FakeCursor:
    """Records statements; enforces read-only once BEGIN READ ONLY is seen."""

    def __init__(self, conn: _FakeConnection, *, rows: list[dict[str, object]]) -> None:
        self._conn = conn
        self._rows = rows
        self.description: object | None = None

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, query: str, params: object = None) -> None:
        self._conn.executed.append(query)
        upper = query.strip().upper()
        if upper.startswith("BEGIN TRANSACTION READ ONLY"):
            self._conn.read_only = True
            return
        if upper in {"COMMIT", "ROLLBACK"}:
            self._conn.read_only = False
            return
        if upper.startswith("SET "):
            return
        # A data-modifying statement inside a read-only tx is rejected,
        # exactly as PostgreSQL would (cannot execute … in a read-only
        # transaction).
        if self._conn.read_only and upper.split()[0] in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            raise _ReadOnlyError("cannot execute in a read-only transaction")
        # A SELECT returns rows.
        if upper.startswith("SELECT"):
            self.description = [("col",)]

    def nextset(self) -> bool:
        return False

    async def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _FakeConnection:
    def __init__(self, *, rows: list[dict[str, object]] | None = None) -> None:
        self.executed: list[str] = []
        self.read_only = False
        self._rows = rows or [{"col": 1}]

    def cursor(self, *args: object, **kwargs: object) -> _FakeCursor:
        return _FakeCursor(self, rows=self._rows)

    async def rollback(self) -> None:
        self.read_only = False


def _read_only_driver_over(conn: _FakeConnection) -> ReadOnlySqlDriver:
    """A ReadOnlySqlDriver whose execute path uses ``conn`` directly."""
    driver = ReadOnlySqlDriver(conn=conn)
    # Bypass the pool branch: point the driver at the fake connection.
    driver.conn = conn
    driver.is_pool = False
    return driver


# ---------------------------------------------------------------------------
# ReadOnlySqlDriver
# ---------------------------------------------------------------------------


async def test_read_only_driver_wraps_every_query_in_read_only_tx() -> None:
    conn = _FakeConnection()
    driver = _read_only_driver_over(conn)

    # Caller asks for a NON-readonly query — the driver must still pin it
    # to a read-only transaction.
    await driver.execute_query("SELECT 1", force_readonly=False)

    assert "BEGIN TRANSACTION READ ONLY" in conn.executed
    assert "ROLLBACK" in conn.executed  # read-only path rolls back, never commits


async def test_read_only_driver_rejects_a_write_against_a_secondary() -> None:
    conn = _FakeConnection()
    driver = _read_only_driver_over(conn)

    with pytest.raises(_ReadOnlyError, match="read-only"):
        await driver.execute_query("INSERT INTO t VALUES (1)", force_readonly=False)

    # The BEGIN READ ONLY was issued before the write was attempted.
    assert "BEGIN TRANSACTION READ ONLY" in conn.executed
    begin_idx = conn.executed.index("BEGIN TRANSACTION READ ONLY")
    write_idx = next(i for i, q in enumerate(conn.executed) if q.startswith("INSERT"))
    assert begin_idx < write_idx


async def test_make_read_only_driver_builds_a_read_only_driver() -> None:
    pool = FakePool()
    driver = make_read_only_driver(pool)  # type: ignore[arg-type]
    assert isinstance(driver, ReadOnlySqlDriver)


# ---------------------------------------------------------------------------
# Database multi-pool selector
# ---------------------------------------------------------------------------


def _settings_with_secondaries() -> object:
    return load_settings(
        {
            "MCPG_DATABASE_URL": _PRIMARY,
            "MCPG_SECONDARY_DATABASE_URLS": (
                "analytics=postgresql://u:p@localhost/an,reporting=postgresql://u:p@localhost/rep"
            ),
        }
    )


async def test_connect_opens_primary_and_secondary_pools() -> None:
    settings = _settings_with_secondaries()
    primary = FakePool()
    analytics = FakePool()
    reporting = FakePool()
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=primary,  # type: ignore[arg-type]
        secondary_pools={"analytics": analytics, "reporting": reporting},  # type: ignore[arg-type]
    )
    await db.connect()

    assert primary.connect_calls == 1
    assert analytics.connect_calls == 1
    assert reporting.connect_calls == 1
    assert db.is_connected is True


async def test_secondary_connect_failure_does_not_abort_startup() -> None:
    settings = _settings_with_secondaries()
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool(fail=True), "reporting": FakePool()},  # type: ignore[arg-type]
    )
    # Must NOT raise even though one secondary fails to open.
    await db.connect()
    assert db.is_connected is True


async def test_database_ids_lists_primary_first() -> None:
    settings = _settings_with_secondaries()
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool(), "reporting": FakePool()},  # type: ignore[arg-type]
    )
    assert db.database_ids() == [PRIMARY_DATABASE_ID, "analytics", "reporting"]


async def test_driver_for_secondary_is_read_only() -> None:
    settings = _settings_with_secondaries()
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()},  # type: ignore[arg-type]
    )
    await db.connect()
    driver = db.driver("analytics")
    assert isinstance(driver, ReadOnlySqlDriver)


async def test_driver_for_primary_is_unchanged_path() -> None:
    settings = _settings_with_secondaries()
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()},  # type: ignore[arg-type]
    )
    await db.connect()
    # None and "primary" both resolve to the primary driver — NOT read-only.
    for db_id in (None, "primary"):
        driver = db.driver(db_id)
        assert isinstance(driver, TimeoutSqlDriver)
        assert not isinstance(driver, ReadOnlySqlDriver)


async def test_driver_unknown_id_raises_listing_valid_ids() -> None:
    settings = _settings_with_secondaries()
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()},  # type: ignore[arg-type]
    )
    await db.connect()
    with pytest.raises(DatabaseError, match=r"unknown database id 'nope'.*analytics"):
        db.driver("nope")


async def test_primary_only_has_no_secondaries() -> None:
    """Zero behaviour change when MCPG_SECONDARY_DATABASE_URLS is unset."""
    settings = load_settings({"MCPG_DATABASE_URL": _PRIMARY})
    db = Database(settings, pool=FakePool())  # type: ignore[arg-type]
    await db.connect()
    assert db.database_ids() == [PRIMARY_DATABASE_ID]
    assert not isinstance(db.driver(), ReadOnlySqlDriver)


# ---------------------------------------------------------------------------
# describe_databases / list_databases payload
# ---------------------------------------------------------------------------


async def test_describe_databases_probes_each_db() -> None:
    settings = _settings_with_secondaries()
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()},  # type: ignore[arg-type]
    )
    await db.connect()

    # Patch driver() so the SELECT 1 probe returns cleanly without a real pool.
    class _OkDriver:
        async def execute_query(self, *a: object, **k: object) -> list[SqlDriver.RowResult]:
            return [SqlDriver.RowResult(cells={"?column?": 1})]

    db.driver = lambda database_id=None: _OkDriver()  # type: ignore[assignment,return-value]

    rows = await db.describe_databases()
    ids = [r[0] for r in rows]
    assert ids == [PRIMARY_DATABASE_ID, "analytics"]
    primary_row = rows[0]
    assert primary_row[1] is True  # is_primary
    assert primary_row[2] is False  # read_only
    secondary_row = rows[1]
    assert secondary_row[1] is False  # is_primary
    assert secondary_row[2] is True  # read_only (secondary)
    assert all(r[3] is True for r in rows)  # reachable


async def test_probe_surfaces_unreachable_detail() -> None:
    settings = _settings_with_secondaries()
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()},  # type: ignore[arg-type]
    )
    await db.connect()

    class _FailDriver:
        async def execute_query(self, *a: object, **k: object) -> list[SqlDriver.RowResult]:
            raise RuntimeError("connection refused for host=secret")

    db.driver = lambda database_id=None: _FailDriver()  # type: ignore[assignment,return-value]
    reachable, detail = await db.probe("analytics")
    assert reachable is False
    assert detail is not None


def test_database_list_is_a_frozen_dataclass() -> None:
    payload = DatabaseList(primary_id="primary", database_ids=["primary"], databases=[])
    with pytest.raises(FrozenInstanceError):
        payload.primary_id = "x"  # type: ignore[misc]
