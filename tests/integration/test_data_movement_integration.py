"""Integration tests for in-process CSV/JSON export against real PG."""

import json
import re
import shutil
import subprocess
from collections.abc import AsyncIterator

import pytest

from mcpg.data_movement import (
    copy_table_between_databases,
    dump_database,
    export_query,
    export_table,
    import_csv,
    import_json,
    restore_database,
)
from mcpg.database import Database


async def _skip_when_pg_dump_too_old_for_server(database: Database) -> None:
    """Skip the calling test when the runner's `pg_dump` is older than
    the connected server.

    pg_dump refuses with SQLSTATE-shaped "server version mismatch" when
    the client is older than the server. This is the exact failure we
    see on the `Tests (PG 19)` matrix entry when the Phase 1 image
    can't install `postgresql-client-19` (roadmap 14.1) — the runner
    falls back to its system-default pg_dump (currently 16.x) which
    cannot dump a PG 19 server.

    Centralised here so all four pg_dump-shelling tests get the same
    skip predicate; future server versions just bump the matrix and
    the gate keeps working.
    """
    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump is not on PATH on this runner")
    try:
        client_out = subprocess.check_output(["pg_dump", "--version"], text=True, timeout=5)
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"pg_dump --version probe failed: {exc}")
    client_match = re.search(r"\b(\d+)(?:\.\d+)?\b", client_out)
    if not client_match:
        pytest.skip(f"could not parse pg_dump --version output: {client_out!r}")
    client_major = int(client_match.group(1))
    rows = await database.driver().execute_query(
        "SELECT current_setting('server_version_num')::int AS ver_num",
        force_readonly=True,
    )
    if not rows:
        pytest.skip("server version probe returned no rows")
    server_major = int(rows[0].cells["ver_num"]) // 10000
    if client_major < server_major:
        pytest.skip(
            f"pg_dump {client_major} cannot dump a PG {server_major} server "
            "(matching client tools were not installable in this CI matrix entry; see roadmap 14.1)"
        )


_SCHEMA = "mcpg_data_movement_it"


@pytest.fixture
async def export_schema(connected_database: Database) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget (id integer PRIMARY KEY, name text NOT NULL, created_at timestamptz)"
    )
    await driver.execute_query(
        f"INSERT INTO {_SCHEMA}.widget (id, name, created_at) "
        "VALUES (1, 'alpha', '2026-05-24T12:00:00Z'), "
        "       (2, 'beta',  '2026-05-24T12:01:00Z'), "
        "       (3, 'gamma, with comma', '2026-05-24T12:02:00Z')"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_export_query_returns_csv_for_a_real_query(connected_database: Database, export_schema: str) -> None:
    result = await export_query(
        connected_database.driver(),
        f"SELECT id, name FROM {export_schema}.widget ORDER BY id",
        format="csv",
    )

    lines = result.content.splitlines()
    assert lines[0] == "id,name"
    assert result.row_count == 3
    assert result.truncated is False
    # The "gamma, with comma" row must be quoted so the CSV is parseable.
    assert '"gamma, with comma"' in result.content


async def test_export_table_returns_json_with_timestamps_stringified(
    connected_database: Database, export_schema: str
) -> None:
    result = await export_table(connected_database.driver(), export_schema, "widget", format="json")

    rows = json.loads(result.content)
    assert {row["name"] for row in rows} == {"alpha", "beta", "gamma, with comma"}
    # The default=str pass means datetime values appear as ISO-ish strings,
    # not objects.
    assert all(isinstance(row["created_at"], str) for row in rows)


async def test_export_query_truncates_at_limit_against_a_large_real_result(
    connected_database: Database, export_schema: str
) -> None:
    result = await export_query(
        connected_database.driver(),
        f"SELECT id FROM {export_schema}.widget ORDER BY id",
        format="csv",
        limit=2,
    )

    assert result.row_count == 2
    assert result.truncated is True
    # The header + 2 data rows == 3 lines.
    assert len(result.content.splitlines()) == 3


# --- dump_database (real pg_dump via the ADR-0004 subprocess gate) -------


async def test_dump_database_runs_real_pg_dump_against_a_live_schema(
    connected_database: Database, export_schema: str
) -> None:
    await _skip_when_pg_dump_too_old_for_server(connected_database)

    settings = connected_database._settings
    result = await dump_database(
        settings.database_url,
        timeout_sec=settings.shell_timeout_sec,
        max_output_bytes=settings.shell_max_output_bytes,
        format="plain",
        schema_only=True,
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.output_truncated is False
    # pg_dump's plain SQL preamble always contains this comment; if it
    # made it into our output, the subprocess + env-var path works end-
    # to-end against a real PG.
    assert "PostgreSQL database dump" in result.content
    # The widget table we seeded earlier should appear in the schema dump.
    assert "widget" in result.content


@pytest.fixture
async def empty_import_schema(connected_database: Database) -> AsyncIterator[str]:
    """A fresh table the import tests can fill, isolated from the export fixture."""
    schema = "mcpg_data_movement_imp_it"
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {schema}")
    await driver.execute_query(
        f"CREATE TABLE {schema}.widget (id integer PRIMARY KEY, name text NOT NULL, config jsonb, tags text[])"
    )
    try:
        yield schema
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


async def test_import_csv_loads_rows_via_copy_from_stdin(
    connected_database: Database, empty_import_schema: str
) -> None:
    payload = "id,name\n1,alpha\n2,beta\n3,gamma\n"
    result = await import_csv(connected_database, empty_import_schema, "widget", payload, columns=["id", "name"])

    assert result.rows_imported == 3

    rows = await connected_database.driver().execute_query(
        f"SELECT id, name FROM {empty_import_schema}.widget ORDER BY id",
        force_readonly=True,
    )
    assert rows is not None
    assert [(r.cells["id"], r.cells["name"]) for r in rows] == [(1, "alpha"), (2, "beta"), (3, "gamma")]


async def test_import_json_loads_rows_with_nested_jsonb_via_executemany(
    connected_database: Database, empty_import_schema: str
) -> None:
    payload = json.dumps(
        [
            {"id": 1, "name": "alpha", "config": {"weight": 5}},
            {"id": 2, "name": "beta", "config": {"weight": 7}},
        ]
    )
    result = await import_json(
        connected_database, empty_import_schema, "widget", payload, columns=["id", "name", "config"]
    )

    assert result.rows_imported == 2

    rows = await connected_database.driver().execute_query(
        f"SELECT id, name, config FROM {empty_import_schema}.widget ORDER BY id",
        force_readonly=True,
    )
    assert rows is not None
    assert rows[0].cells["name"] == "alpha"
    # The jsonb column survived the round-trip as a dict, not a stringified payload.
    assert rows[0].cells["config"] == {"weight": 5}
    assert rows[1].cells["config"] == {"weight": 7}


async def test_dump_then_restore_round_trip_against_real_pg(connected_database: Database, export_schema: str) -> None:
    if shutil.which("psql") is None:
        pytest.skip("psql is not on PATH on this runner")
    await _skip_when_pg_dump_too_old_for_server(connected_database)

    settings = connected_database._settings
    # Dump the seeded test schema (--schema-only is enough; data isn't
    # the point of the round-trip).
    dump = await dump_database(
        settings.database_url,
        timeout_sec=settings.shell_timeout_sec,
        max_output_bytes=settings.shell_max_output_bytes,
        format="plain",
        schema_only=True,
    )
    assert dump.exit_code == 0
    assert "widget" in dump.content

    # Drop the schema, then restore from the dump.
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {export_schema} CASCADE")

    restored = await restore_database(
        settings.database_url,
        dump.content,
        timeout_sec=settings.shell_timeout_sec,
        max_output_bytes=settings.shell_max_output_bytes,
        format="plain",
    )
    assert restored.exit_code == 0
    assert restored.timed_out is False

    # The widget table should be back; the data was dropped with the
    # schema but the structure has been re-created.
    rows = await driver.execute_query(
        f"SELECT count(*) AS n FROM {export_schema}.widget",
        force_readonly=True,
    )
    assert rows is not None
    assert rows[0].cells["n"] == 0


async def test_copy_table_between_databases_round_trips_against_real_pg(
    connected_database: Database, export_schema: str
) -> None:
    """End-to-end: pg_dump source DB, pipe into pg_restore on a fresh dest DB.

    Spins up a throwaway target database on the same server so the dump
    archive's hard-coded schema/table names land cleanly without PK
    collisions. Exercises the full subprocess pipeline (two separate
    libpq envs, stdin handoff between dump and restore) end-to-end.
    """
    if shutil.which("pg_restore") is None:
        pytest.skip("pg_restore is not on PATH on this runner")
    await _skip_when_pg_dump_too_old_for_server(connected_database)

    from urllib.parse import urlparse, urlunparse

    settings = connected_database._settings
    parsed = urlparse(settings.database_url)
    dest_db = "mcpg_copy_target"
    # CREATE/DROP DATABASE can't run inside a transaction, so they go
    # through Database.run_unmanaged.
    await connected_database.run_unmanaged(f"DROP DATABASE IF EXISTS {dest_db}")
    await connected_database.run_unmanaged(f"CREATE DATABASE {dest_db}")
    try:
        dest_url = urlunparse(parsed._replace(path=f"/{dest_db}"))
        # pg_dump --table emits the table's CREATE but not its enclosing
        # schema. Pre-create the schema on the destination so the
        # restore-with-schema path lands without a "schema does not
        # exist" error.
        from mcpg.config import load_settings as _load_settings

        dest_bootstrap_settings = _load_settings({"MCPG_DATABASE_URL": dest_url})
        bootstrap = Database(dest_bootstrap_settings)
        await bootstrap.connect()
        try:
            await bootstrap.driver().execute_query(f"CREATE SCHEMA {export_schema}")
        finally:
            await bootstrap.close()

        result = await copy_table_between_databases(
            settings.database_url,
            dest_url,
            export_schema,
            "widget",
            include_schema=True,
            include_data=True,
            timeout_sec=settings.shell_timeout_sec,
            max_output_bytes=settings.shell_max_output_bytes,
        )
        assert result.dump_exit_code == 0, result.dump_stderr_tail
        assert result.restore_exit_code == 0, result.restore_stderr_tail
        assert result.timed_out is False
        assert result.schema_copied is True
        assert result.data_copied is True

        # Verify the rows landed in the destination database by opening
        # a second pool against dest_url.
        dest = Database(dest_bootstrap_settings)
        await dest.connect()
        try:
            rows = await dest.driver().execute_query(
                f"SELECT id, name FROM {export_schema}.widget ORDER BY id",
                force_readonly=True,
            )
        finally:
            await dest.close()
        assert rows is not None
        assert [(r.cells["id"], r.cells["name"]) for r in rows] == [
            (1, "alpha"),
            (2, "beta"),
            (3, "gamma, with comma"),
        ]
    finally:
        # FORCE drop the target DB even if connections lingered (the
        # test pool's been closed above, but pg_dump/pg_restore may have
        # left backends in TERMINATING). FORCE is PG 13+.
        await connected_database.run_unmanaged(f"DROP DATABASE IF EXISTS {dest_db} WITH (FORCE)")
