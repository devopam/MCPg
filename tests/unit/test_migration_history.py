"""Tests for the migration history integration."""

from __future__ import annotations

import json
from typing import Any

from _fakes import FakeDatabase, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg._vendor.sql import SqlDriver
from mcpg.config import load_settings
from mcpg.migration_history import (
    AlembicMigration,
    DieselMigration,
    DjangoMigration,
    FlywayMigration,
    GolangMigrateMigration,
    GooseMigration,
    MigrationHistoryReport,
    PrismaMigration,
    SequelizeMigration,
    read_migration_history,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_read_migration_history_empty() -> None:
    # No tables found in information_schema.tables
    driver = FakeRoutingDriver({"information_schema.tables": []})

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert isinstance(report, MigrationHistoryReport)
    assert report.alembic is None
    assert report.flyway is None
    assert report.diesel is None
    assert report.django is None
    assert report.prisma is None
    assert report.golang_migrate is None
    assert report.goose is None
    assert report.sequelize is None


async def test_read_migration_history_alembic() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "alembic_version"}],
            "alembic_version": [{"version_num": "ae12d34199f"}],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert report.alembic == [AlembicMigration(version_num="ae12d34199f")]
    assert report.flyway is None


async def test_read_migration_history_flyway() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "flyway_schema_history"}],
            "flyway_schema_history": [
                {
                    "installed_rank": 1,
                    "version": "1.0.0",
                    "description": "Initial Setup",
                    "type": "SQL",
                    "script": "V1__setup.sql",
                    "checksum": 123456,
                    "installed_by": "db_user",
                    "installed_on": "2026-06-03 10:00:00",
                    "execution_time": 45,
                    "success": True,
                }
            ],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert len(report.flyway or []) == 1
    f = (report.flyway or [])[0]
    assert isinstance(f, FlywayMigration)
    assert f.installed_rank == 1
    assert f.version == "1.0.0"
    assert f.description == "Initial Setup"
    assert f.type == "SQL"
    assert f.script == "V1__setup.sql"
    assert f.checksum == 123456
    assert f.installed_by == "db_user"
    assert f.installed_on == "2026-06-03 10:00:00"
    assert f.execution_time == 45
    assert f.success is True


async def test_read_migration_history_diesel() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "__diesel_schema_migrations"}],
            "__diesel_schema_migrations": [{"version": "20260603120000", "run_on": "2026-06-03 12:00:05"}],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert report.diesel == [DieselMigration(version="20260603120000", run_on="2026-06-03 12:00:05")]


async def test_read_migration_history_django() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "django_migrations"}],
            "django_migrations": [{"id": 42, "app": "auth", "name": "0001_initial", "applied": "2026-06-03 10:15:00"}],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert report.django == [DjangoMigration(id=42, app="auth", name="0001_initial", applied="2026-06-03 10:15:00")]


async def test_read_migration_history_prisma() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "_prisma_migrations"}],
            "_prisma_migrations": [
                {
                    "id": "e8e1919e-e81a-4ea9-b226-f7f1a3a3112a",
                    "checksum": "abc123checksum",
                    "finished_at": "2026-06-03 11:00:00",
                    "migration_name": "20260603110000_init",
                    "logs": "No logs",
                    "rolled_back_at": None,
                    "started_at": "2026-06-03 10:59:59",
                    "applied_steps_count": 1,
                }
            ],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert len(report.prisma or []) == 1
    p = (report.prisma or [])[0]
    assert isinstance(p, PrismaMigration)
    assert p.id == "e8e1919e-e81a-4ea9-b226-f7f1a3a3112a"
    assert p.checksum == "abc123checksum"
    assert p.finished_at == "2026-06-03 11:00:00"
    assert p.migration_name == "20260603110000_init"
    assert p.logs == "No logs"
    assert p.rolled_back_at is None
    assert p.started_at == "2026-06-03 10:59:59"
    assert p.applied_steps_count == 1


async def test_read_migration_history_golang_migrate() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "schema_migrations"}],
            "schema_migrations": [{"version": 3, "dirty": False}],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert report.golang_migrate == [GolangMigrateMigration(version=3, dirty=False)]


async def test_read_migration_history_goose() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "goose_db_version"}],
            "goose_db_version": [
                {"id": 1, "version_id": 2026060300, "is_applied": True, "tstamp": "2026-06-03 12:30:00"}
            ],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert report.goose == [GooseMigration(id=1, version_id=2026060300, is_applied=True, tstamp="2026-06-03 12:30:00")]


async def test_read_migration_history_sequelize() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "SequelizeMeta"}],
            "SequelizeMeta": [{"name": "20260603120000-init.js"}],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert report.sequelize == [SequelizeMigration(name="20260603120000-init.js")]


async def test_read_migration_history_schema_filter() -> None:
    driver = FakeRoutingDriver({"information_schema.tables": []})

    await read_migration_history(driver, schema="my_custom_schema")  # type: ignore[arg-type]

    # Verify that the schema name was passed as parameter in the information_schema query
    assert len(driver.calls) == 1
    assert "AND table_schema = %s" in driver.calls[0][0]
    assert driver.calls[0][1] == ["my_custom_schema"]


async def test_read_migration_history_resilient_to_errors() -> None:
    class FailingRoutingDriver(FakeRoutingDriver):
        async def execute_query(
            self, query: str, params: list[Any] | None = None, force_readonly: bool = False
        ) -> list[SqlDriver.RowResult]:
            if "alembic_version" in query and "version_num" in query:
                raise RuntimeError("database error")
            return await super().execute_query(query, params, force_readonly)

    driver = FailingRoutingDriver(
        {
            "information_schema.tables": [
                {"table_schema": "public", "table_name": "alembic_version"},
                {"table_schema": "public", "table_name": "SequelizeMeta"},
            ],
            "SequelizeMeta": [{"name": "20260603120000-init.js"}],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    # alembic fails query and falls back to None, but sequelize succeeds
    assert report.alembic is None
    assert report.sequelize == [SequelizeMigration(name="20260603120000-init.js")]


# --- MCP tool registration & execution test ---


async def test_read_migration_history_tool() -> None:
    fake_driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "alembic_version"}],
            "alembic_version": [{"version_num": "ae12d34199f"}],
        }
    )
    server = create_server(_SETTINGS, database=FakeDatabase(fake_driver))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools
        names = {tool.name for tool in tools}
        assert "read_migration_history" in names

        res = await client.call_tool("read_migration_history", {"schema": "public"})
        assert res.isError is False
        assert res.content[0].text is not None
        data = json.loads(res.content[0].text)
        assert data["alembic"] == [{"version_num": "ae12d34199f"}]
        assert data["flyway"] is None


async def test_read_migration_history_multi_schema_accumulation() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [
                {"table_schema": "public", "table_name": "alembic_version"},
                {"table_schema": "tenant1", "table_name": "alembic_version"},
            ],
            '"public"."alembic_version"': [{"version_num": "public_version"}],
            '"tenant1"."alembic_version"': [{"version_num": "tenant1_version"}],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert report.alembic == [
        AlembicMigration(version_num="public_version"),
        AlembicMigration(version_num="tenant1_version"),
    ]


async def test_read_migration_history_escaped_identifiers() -> None:
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": 'tenant"1', "table_name": "alembic_version"}],
            'tenant""1': [{"version_num": "escaped_version"}],
        }
    )

    report = await read_migration_history(driver)  # type: ignore[arg-type]

    assert len(driver.calls) == 2
    assert '"tenant""1"."alembic_version"' in driver.calls[1][0]
    assert report.alembic == [AlembicMigration(version_num="escaped_version")]
