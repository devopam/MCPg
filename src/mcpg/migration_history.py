"""PostgreSQL migration history table reader."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcpg.sql import SqlDriver


@dataclass(frozen=True)
class AlembicMigration:
    """A row from ``alembic_version``."""

    version_num: str


@dataclass(frozen=True)
class FlywayMigration:
    """A row from ``flyway_schema_history``."""

    installed_rank: int
    version: str | None
    description: str | None
    type: str
    script: str
    checksum: int | None
    installed_by: str
    installed_on: str
    execution_time: int
    success: bool


@dataclass(frozen=True)
class DieselMigration:
    """A row from ``__diesel_schema_migrations``."""

    version: str
    run_on: str


@dataclass(frozen=True)
class DjangoMigration:
    """A row from ``django_migrations``."""

    id: int
    app: str
    name: str
    applied: str


@dataclass(frozen=True)
class PrismaMigration:
    """A row from ``_prisma_migrations``."""

    id: str
    checksum: str
    finished_at: str | None
    migration_name: str
    logs: str | None
    rolled_back_at: str | None
    started_at: str
    applied_steps_count: int


@dataclass(frozen=True)
class GolangMigrateMigration:
    """A row from ``schema_migrations``."""

    version: int
    dirty: bool


@dataclass(frozen=True)
class GooseMigration:
    """A row from ``goose_db_version``."""

    id: int
    version_id: int
    is_applied: bool
    tstamp: str | None


@dataclass(frozen=True)
class SequelizeMigration:
    """A row from ``SequelizeMeta``."""

    name: str


@dataclass(frozen=True)
class MigrationHistoryReport:
    """Report summarizing discovered migration histories."""

    alembic: list[AlembicMigration] | None = None
    flyway: list[FlywayMigration] | None = None
    diesel: list[DieselMigration] | None = None
    django: list[DjangoMigration] | None = None
    prisma: list[PrismaMigration] | None = None
    golang_migrate: list[GolangMigrateMigration] | None = None
    goose: list[GooseMigration] | None = None
    sequelize: list[SequelizeMigration] | None = None


def _maybe_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)  # type: ignore[call-overload,no-any-return]


def _maybe_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _quote_ident(name: str) -> str:
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


async def read_migration_history(
    driver: SqlDriver,
    schema: str | None = None,
) -> MigrationHistoryReport:
    """Read migration bookkeeping history tables in the database.

    Scans the database (filtered optionally by ``schema``) for standard table names used by
    common migration frameworks (Alembic, Flyway, Diesel, Django, Prisma, Golang Migrate,
    Goose, Sequelize) and returns records found in them.
    """
    # 1. Discover which tables exist.
    query = (
        "SELECT table_schema, table_name "
        "FROM information_schema.tables "
        "WHERE table_name IN ("
        "  'alembic_version', "
        "  'flyway_schema_history', "
        "  '__diesel_schema_migrations', "
        "  'django_migrations', "
        "  '_prisma_migrations', "
        "  'schema_migrations', "
        "  'goose_db_version', "
        "  'SequelizeMeta'"
        ") AND table_schema NOT IN ('pg_catalog', 'information_schema')"
    )
    params: list[Any] = []
    if schema:
        query += " AND table_schema = %s"
        params.append(schema)

    rows = await driver.execute_query(query, params=params, force_readonly=True)
    if not rows:
        return MigrationHistoryReport()

    # Map discovered tables: (table_schema, table_name)
    discovered = [(str(row.cells["table_schema"]), str(row.cells["table_name"])) for row in rows]

    alembic: list[AlembicMigration] | None = None
    flyway: list[FlywayMigration] | None = None
    diesel: list[DieselMigration] | None = None
    django: list[DjangoMigration] | None = None
    prisma: list[PrismaMigration] | None = None
    golang_migrate: list[GolangMigrateMigration] | None = None
    goose: list[GooseMigration] | None = None
    sequelize: list[SequelizeMigration] | None = None

    for s, t in discovered:
        quoted_s = _quote_ident(s)
        quoted_t = _quote_ident(t)

        if t == "alembic_version":
            try:
                # Alembic has just a single version_num column (typically 1 row)
                alembic_rows = await driver.execute_query(
                    f"SELECT version_num::text AS version_num FROM {quoted_s}.{quoted_t}",
                    force_readonly=True,
                )
                if alembic is None:
                    alembic = []
                alembic.extend(
                    [AlembicMigration(version_num=str(row.cells["version_num"])) for row in alembic_rows or []]
                )
            except Exception:
                pass

        elif t == "flyway_schema_history":
            try:
                flyway_rows = await driver.execute_query(
                    f"SELECT installed_rank, version, description, type, script, "
                    f"       checksum, installed_by, installed_on::text AS installed_on, "
                    f"       execution_time, success "
                    f"FROM {quoted_s}.{quoted_t} ORDER BY installed_rank",
                    force_readonly=True,
                )
                if flyway is None:
                    flyway = []
                flyway.extend(
                    [
                        FlywayMigration(
                            installed_rank=int(row.cells["installed_rank"]),
                            version=_maybe_str(row.cells.get("version")),
                            description=_maybe_str(row.cells.get("description")),
                            type=str(row.cells["type"]),
                            script=str(row.cells["script"]),
                            checksum=_maybe_int(row.cells.get("checksum")),
                            installed_by=str(row.cells["installed_by"]),
                            installed_on=str(row.cells["installed_on"]),
                            execution_time=int(row.cells["execution_time"]),
                            success=bool(row.cells["success"]),
                        )
                        for row in flyway_rows or []
                    ]
                )
            except Exception:
                pass

        elif t == "__diesel_schema_migrations":
            try:
                diesel_rows = await driver.execute_query(
                    f"SELECT version, run_on::text AS run_on FROM {quoted_s}.{quoted_t} ORDER BY version",
                    force_readonly=True,
                )
                if diesel is None:
                    diesel = []
                diesel.extend(
                    [
                        DieselMigration(
                            version=str(row.cells["version"]),
                            run_on=str(row.cells["run_on"]),
                        )
                        for row in diesel_rows or []
                    ]
                )
            except Exception:
                pass

        elif t == "django_migrations":
            try:
                django_rows = await driver.execute_query(
                    f"SELECT id, app, name, applied::text AS applied FROM {quoted_s}.{quoted_t} ORDER BY id",
                    force_readonly=True,
                )
                if django is None:
                    django = []
                django.extend(
                    [
                        DjangoMigration(
                            id=int(row.cells["id"]),
                            app=str(row.cells["app"]),
                            name=str(row.cells["name"]),
                            applied=str(row.cells["applied"]),
                        )
                        for row in django_rows or []
                    ]
                )
            except Exception:
                pass

        elif t == "_prisma_migrations":
            try:
                prisma_rows = await driver.execute_query(
                    f"SELECT id, checksum, finished_at::text AS finished_at, migration_name, "
                    f"       logs, rolled_back_at::text AS rolled_back_at, started_at::text AS started_at, "
                    f"       applied_steps_count "
                    f"FROM {quoted_s}.{quoted_t} ORDER BY started_at",
                    force_readonly=True,
                )
                if prisma is None:
                    prisma = []
                prisma.extend(
                    [
                        PrismaMigration(
                            id=str(row.cells["id"]),
                            checksum=str(row.cells["checksum"]),
                            finished_at=_maybe_str(row.cells.get("finished_at")),
                            migration_name=str(row.cells["migration_name"]),
                            logs=_maybe_str(row.cells.get("logs")),
                            rolled_back_at=_maybe_str(row.cells.get("rolled_back_at")),
                            started_at=str(row.cells["started_at"]),
                            applied_steps_count=int(row.cells["applied_steps_count"]),
                        )
                        for row in prisma_rows or []
                    ]
                )
            except Exception:
                pass

        elif t == "schema_migrations":
            try:
                # golang-migrate
                gm_rows = await driver.execute_query(
                    f"SELECT version, dirty FROM {quoted_s}.{quoted_t} ORDER BY version",
                    force_readonly=True,
                )
                if golang_migrate is None:
                    golang_migrate = []
                golang_migrate.extend(
                    [
                        GolangMigrateMigration(
                            version=int(row.cells["version"]),
                            dirty=bool(row.cells["dirty"]),
                        )
                        for row in gm_rows or []
                    ]
                )
            except Exception:
                pass

        elif t == "goose_db_version":
            try:
                goose_rows = await driver.execute_query(
                    f"SELECT id, version_id, is_applied, tstamp::text AS tstamp FROM {quoted_s}.{quoted_t} ORDER BY id",
                    force_readonly=True,
                )
                if goose is None:
                    goose = []
                goose.extend(
                    [
                        GooseMigration(
                            id=int(row.cells["id"]),
                            version_id=int(row.cells["version_id"]),
                            is_applied=bool(row.cells["is_applied"]),
                            tstamp=_maybe_str(row.cells.get("tstamp")),
                        )
                        for row in goose_rows or []
                    ]
                )
            except Exception:
                pass

        elif t == "SequelizeMeta":
            try:
                seq_rows = await driver.execute_query(
                    f"SELECT name FROM {quoted_s}.{quoted_t} ORDER BY name",
                    force_readonly=True,
                )
                if sequelize is None:
                    sequelize = []
                sequelize.extend([SequelizeMigration(name=str(row.cells["name"])) for row in seq_rows or []])
            except Exception:
                pass

    return MigrationHistoryReport(
        alembic=alembic,
        flyway=flyway,
        diesel=diesel,
        django=django,
        prisma=prisma,
        golang_migrate=golang_migrate,
        goose=goose,
        sequelize=sequelize,
    )
