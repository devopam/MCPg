"""Tests for the PG 19 DDL helpers module."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver

from mcpg.pg19_ddl import (
    ObjectDdlResult,
    Pg19DdlError,
    Pg19DdlStatus,
    ValidateCheckConstraintResult,
    get_database_ddl,
    get_pg19_ddl_status,
    get_role_ddl,
    get_tablespace_ddl,
    validate_check_constraint,
)


def _version_route(num: int, ver: str) -> dict[str, list[dict[str, object]]]:
    return {"current_setting('server_version_num')": [{"ver_num": num, "ver": ver}]}


# --- get_pg19_ddl_status --------------------------------------------------


async def test_status_available_on_pg19_all_functions_present() -> None:
    routes = _version_route(190001, "19beta1")
    # All three pg_get_*def() probes hit the same pg_proc query.
    routes["pg_proc p JOIN pg_namespace"] = [{"present": 1}]
    driver = FakeRoutingDriver(routes)
    status = await get_pg19_ddl_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, Pg19DdlStatus)
    assert status.available is True
    assert status.has_pg_get_roledef is True
    assert status.has_pg_get_databasedef is True
    assert status.has_pg_get_tablespacedef is True
    assert "available" in status.detail.lower()


async def test_status_unavailable_on_pg18() -> None:
    routes = _version_route(180003, "18.3")
    driver = FakeRoutingDriver(routes)
    status = await get_pg19_ddl_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.has_pg_get_roledef is False
    assert "pg_dumpall" in status.detail


async def test_status_pg19_with_no_functions_reports_unavailable() -> None:
    """A PG 19 build that stripped the pg_get_*def() family still reports cleanly."""
    routes = _version_route(190001, "19beta1")
    # pg_proc probe returns no rows.
    routes["pg_proc p JOIN pg_namespace"] = []
    driver = FakeRoutingDriver(routes)
    status = await get_pg19_ddl_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert status.has_pg_get_roledef is False
    assert "pg_dumpall" in status.detail


async def test_status_never_raises_on_driver_failure() -> None:
    driver = FakeDriver(fail=True)
    status = await get_pg19_ddl_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "version probe failed" in status.detail


# --- validate_check_constraint --------------------------------------------


class _ValidateDriver:
    """Custom driver: routes the convalidated read by query substring;
    every other query (the ALTER TABLE) gets recorded into ``executed``."""

    def __init__(self, *, convalidated: bool | None, alter_fails: bool = False) -> None:
        self._convalidated = convalidated
        self._alter_fails = alter_fails
        self.executed: list[str] = []
        self.calls: list[tuple[str, object, bool]] = []

    async def execute_query(self, query, params=None, force_readonly=False):  # type: ignore[no-untyped-def]
        from mcpg._vendor.sql import SqlDriver

        self.calls.append((query, params, force_readonly))
        if "pg_constraint" in query:
            if self._convalidated is None:
                return []
            return [SqlDriver.RowResult(cells={"convalidated": self._convalidated})]
        # ALTER TABLE path.
        self.executed.append(query)
        if self._alter_fails:
            raise RuntimeError("lock timeout")
        return []


async def test_validate_constraint_emits_alter_table_when_not_valid() -> None:
    driver = _ValidateDriver(convalidated=False)
    db = FakeDatabase(driver)  # type: ignore[arg-type]
    result = await validate_check_constraint(
        db,  # type: ignore[arg-type]
        schema="public",
        table="orders",
        constraint_name="orders_total_nonneg",
    )
    assert isinstance(result, ValidateCheckConstraintResult)
    assert result.was_valid is False
    assert result.now_valid is True
    assert result.changed is True
    assert driver.executed == ['ALTER TABLE "public"."orders" VALIDATE CONSTRAINT "orders_total_nonneg"']
    assert "ALTER TABLE" in result.validate_sql


async def test_validate_constraint_is_noop_when_already_valid() -> None:
    driver = _ValidateDriver(convalidated=True)
    db = FakeDatabase(driver)  # type: ignore[arg-type]
    result = await validate_check_constraint(
        db,  # type: ignore[arg-type]
        schema="public",
        table="orders",
        constraint_name="orders_total_nonneg",
    )
    assert result.was_valid is True
    assert result.now_valid is True
    assert result.changed is False
    assert driver.executed == []
    assert "no-op" in result.validate_sql


async def test_validate_constraint_raises_when_not_found() -> None:
    driver = _ValidateDriver(convalidated=None)
    db = FakeDatabase(driver)  # type: ignore[arg-type]
    with pytest.raises(Pg19DdlError, match="not found"):
        await validate_check_constraint(
            db,  # type: ignore[arg-type]
            schema="public",
            table="orders",
            constraint_name="missing",
        )
    assert driver.executed == []


async def test_validate_constraint_wraps_alter_failure() -> None:
    driver = _ValidateDriver(convalidated=False, alter_fails=True)
    db = FakeDatabase(driver)  # type: ignore[arg-type]
    with pytest.raises(Pg19DdlError, match="VALIDATE CONSTRAINT"):
        await validate_check_constraint(
            db,  # type: ignore[arg-type]
            schema="public",
            table="orders",
            constraint_name="orders_total_nonneg",
        )


async def test_validate_constraint_quotes_identifiers_against_injection() -> None:
    """Embedded double-quotes get escaped; the SQL is still safe."""
    driver = _ValidateDriver(convalidated=False)
    db = FakeDatabase(driver)  # type: ignore[arg-type]
    result = await validate_check_constraint(
        db,  # type: ignore[arg-type]
        schema='evil"schema',
        table='evil"table',
        constraint_name='evil"name',
    )
    assert driver.executed == ['ALTER TABLE "evil""schema"."evil""table" VALIDATE CONSTRAINT "evil""name"']
    assert result.changed is True


# --- get_role_ddl / get_database_ddl / get_tablespace_ddl -----------------


async def test_get_role_ddl_returns_ddl_on_pg19() -> None:
    routes = _version_route(190001, "19beta1")
    routes["pg_get_roledef"] = [{"ddl": "CREATE ROLE app_user LOGIN;"}]
    driver = FakeRoutingDriver(routes)
    result = await get_role_ddl(driver, "app_user")  # type: ignore[arg-type]
    assert isinstance(result, ObjectDdlResult)
    assert result.object_type == "role"
    assert result.object_name == "app_user"
    assert result.found is True
    assert "CREATE ROLE" in result.ddl


async def test_get_role_ddl_not_found_returns_empty() -> None:
    routes = _version_route(190001, "19beta1")
    routes["pg_get_roledef"] = []  # no rows
    driver = FakeRoutingDriver(routes)
    result = await get_role_ddl(driver, "ghost")  # type: ignore[arg-type]
    assert result.found is False
    assert result.ddl == ""


async def test_get_role_ddl_raises_on_pg18() -> None:
    routes = _version_route(180003, "18.3")
    driver = FakeRoutingDriver(routes)
    with pytest.raises(Pg19DdlError, match="PostgreSQL 19"):
        await get_role_ddl(driver, "app_user")  # type: ignore[arg-type]


async def test_get_database_ddl_returns_ddl_on_pg19() -> None:
    routes = _version_route(190001, "19beta1")
    routes["pg_get_databasedef"] = [{"ddl": "CREATE DATABASE analytics;"}]
    driver = FakeRoutingDriver(routes)
    result = await get_database_ddl(driver, "analytics")  # type: ignore[arg-type]
    assert result.object_type == "database"
    assert result.found is True
    assert "CREATE DATABASE" in result.ddl


async def test_get_database_ddl_raises_on_pg18() -> None:
    routes = _version_route(180003, "18.3")
    driver = FakeRoutingDriver(routes)
    with pytest.raises(Pg19DdlError, match="PostgreSQL 19"):
        await get_database_ddl(driver, "analytics")  # type: ignore[arg-type]


async def test_get_tablespace_ddl_returns_ddl_on_pg19() -> None:
    routes = _version_route(190001, "19beta1")
    routes["pg_get_tablespacedef"] = [{"ddl": "CREATE TABLESPACE fast_ssd LOCATION '/mnt/ssd';"}]
    driver = FakeRoutingDriver(routes)
    result = await get_tablespace_ddl(driver, "fast_ssd")  # type: ignore[arg-type]
    assert result.object_type == "tablespace"
    assert result.found is True
    assert "CREATE TABLESPACE" in result.ddl


async def test_get_tablespace_ddl_raises_on_pg18() -> None:
    routes = _version_route(180003, "18.3")
    driver = FakeRoutingDriver(routes)
    with pytest.raises(Pg19DdlError, match="PostgreSQL 19"):
        await get_tablespace_ddl(driver, "fast_ssd")  # type: ignore[arg-type]


# --- Dataclass shapes -----------------------------------------------------


def test_dataclass_shapes() -> None:
    status = Pg19DdlStatus(
        available=True,
        server_version_num=190001,
        server_version="19beta1",
        has_pg_get_roledef=True,
        has_pg_get_databasedef=True,
        has_pg_get_tablespacedef=True,
        detail="ok",
    )
    assert status.available is True
    vc = ValidateCheckConstraintResult(
        schema="public",
        table="t",
        constraint_name="c",
        was_valid=False,
        now_valid=True,
        changed=True,
        validate_sql='ALTER TABLE "public"."t" VALIDATE CONSTRAINT "c"',
    )
    assert vc.changed is True
    obj = ObjectDdlResult(object_type="role", object_name="x", found=False, ddl="")
    assert obj.found is False
