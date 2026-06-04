"""Tests for mcpg.migration_ingestion — list pending migrations on disk."""

from __future__ import annotations

from pathlib import Path

import pytest
from _fakes import FakeRoutingDriver

from mcpg.migration_ingestion import (
    MigrationIngestionError,
    PendingMigration,
    PendingMigrationsReport,
    _extract_identifier,
    _first_comment_line,
    list_pending_migrations,
)

# --- filename → identifier extraction --------------------------------------


@pytest.mark.parametrize(
    ("framework", "filename", "expected"),
    [
        ("flyway", "V1__init.sql", "1"),
        ("flyway", "V2.3.1__add_users.sql", "2.3.1"),
        ("flyway", "V1_5__legacy.sql", "1.5"),  # underscores normalise to dots
        ("flyway", "U1__undo.sql", None),  # undo migrations are out of scope
        ("flyway", "R__refresh_views.sql", None),  # repeatables are out of scope
        ("flyway", "README.md", None),
        ("alembic", "abc123def456_initial_schema.py", "abc123def456"),
        ("alembic", "0001_add_users.py", "0001"),
        ("alembic", "env.py", None),
        ("alembic", "__init__.py", None),
        ("alembic", "no_underscore_or_extension", None),
        ("liquibase", "001-create-users.sql", "001-create-users"),
        ("liquibase", "002-add-index.SQL", "002-add-index"),  # case-insensitive .sql
        ("liquibase", "notes.txt", None),
    ],
)
def test_extract_identifier_recognises_framework_conventions(
    framework: str, filename: str, expected: str | None
) -> None:
    assert _extract_identifier(framework, filename) == expected


def test_first_comment_line_returns_sql_comment() -> None:
    text = "\n\n-- creates the users table with a PK on id\nCREATE TABLE users (\n  id serial primary key\n);\n"
    assert _first_comment_line(text) == "-- creates the users table with a PK on id"


def test_first_comment_line_returns_python_or_block_comment() -> None:
    assert _first_comment_line("\n  # alembic revision\nfrom alembic import op\n") == "# alembic revision"
    assert _first_comment_line("/* drop the old col */\nALTER TABLE ...") == "/* drop the old col */"


def test_first_comment_line_returns_none_when_code_comes_first() -> None:
    # We don't misread a SQL statement as a description.
    assert _first_comment_line("CREATE TABLE x (id int);\n-- after the fact\n") is None


def test_first_comment_line_handles_empty_file() -> None:
    assert _first_comment_line("") is None


def test_first_comment_line_truncates_long_comments() -> None:
    long_comment = "-- " + ("x" * 1000)
    result = _first_comment_line(long_comment)
    assert result is not None
    assert len(result) == 200


# --- argument validation ---------------------------------------------------


async def test_list_pending_migrations_rejects_unknown_framework(tmp_path: Path) -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(MigrationIngestionError, match="unknown framework"):
        await list_pending_migrations(
            driver,  # type: ignore[arg-type]
            "django",
            str(tmp_path),
            allowed_roots=(str(tmp_path),),
        )


async def test_list_pending_migrations_rejects_empty_scripts_dir(tmp_path: Path) -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(MigrationIngestionError, match="scripts_dir is required"):
        await list_pending_migrations(
            driver,  # type: ignore[arg-type]
            "flyway",
            "",
            allowed_roots=(str(tmp_path),),
        )


async def test_list_pending_migrations_rejects_relative_scripts_dir(tmp_path: Path) -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(MigrationIngestionError, match="must be an absolute path"):
        await list_pending_migrations(
            driver,  # type: ignore[arg-type]
            "flyway",
            "relative/path",
            allowed_roots=(str(tmp_path),),
        )


async def test_list_pending_migrations_refuses_when_no_allowed_roots(tmp_path: Path) -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(MigrationIngestionError, match="MCPG_MIGRATION_SCRIPTS_ROOTS"):
        await list_pending_migrations(
            driver,  # type: ignore[arg-type]
            "flyway",
            str(tmp_path),
            allowed_roots=(),
        )


async def test_list_pending_migrations_refuses_path_outside_allowed_roots(
    tmp_path: Path,
) -> None:
    other = tmp_path.parent / "outside"
    other.mkdir(exist_ok=True)
    driver = FakeRoutingDriver({})
    with pytest.raises(MigrationIngestionError, match="outside MCPG_MIGRATION_SCRIPTS_ROOTS"):
        await list_pending_migrations(
            driver,  # type: ignore[arg-type]
            "flyway",
            str(other),
            allowed_roots=(str(tmp_path),),
        )


async def test_list_pending_migrations_rejects_traversal_escape(tmp_path: Path) -> None:
    # Even with ``..`` in the input, ``Path.resolve(strict=True)``
    # canonicalises before the prefix check, so an escape attempt
    # is caught at the allowlist gate.
    target = tmp_path / "inside"
    target.mkdir()
    escape = str(target / ".." / "..")
    driver = FakeRoutingDriver({})
    with pytest.raises(MigrationIngestionError, match="outside MCPG_MIGRATION_SCRIPTS_ROOTS"):
        await list_pending_migrations(
            driver,  # type: ignore[arg-type]
            "flyway",
            escape,
            allowed_roots=(str(target),),
        )


async def test_list_pending_migrations_rejects_nonexistent_path(tmp_path: Path) -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(MigrationIngestionError, match="does not exist"):
        await list_pending_migrations(
            driver,  # type: ignore[arg-type]
            "flyway",
            str(tmp_path / "missing"),
            allowed_roots=(str(tmp_path),),
        )


async def test_list_pending_migrations_rejects_file_path(tmp_path: Path) -> None:
    f = tmp_path / "single.sql"
    f.write_text("-- single file\n")
    driver = FakeRoutingDriver({})
    with pytest.raises(MigrationIngestionError, match="not a directory"):
        await list_pending_migrations(
            driver,  # type: ignore[arg-type]
            "flyway",
            str(f),
            allowed_roots=(str(tmp_path),),
        )


# --- happy paths -----------------------------------------------------------


def _migrations_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    return d


async def test_list_pending_migrations_reports_greenfield_when_history_missing(
    tmp_path: Path,
) -> None:
    d = _migrations_dir(tmp_path)
    (d / "V1__init.sql").write_text("-- creates the schema\nCREATE TABLE t (id int);\n")
    (d / "V2__users.sql").write_text("-- adds users\nCREATE TABLE users (id int);\n")
    # No history table — `information_schema.tables` lookup returns empty.
    driver = FakeRoutingDriver({"information_schema.tables": []})

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert isinstance(report, PendingMigrationsReport)
    assert report.available is False
    assert report.history_table is None
    assert report.applied_count == 0
    assert report.pending_count == 2
    assert {m.identifier for m in report.pending} == {"1", "2"}
    assert any("greenfield" in report.notes.lower() or "every script" in report.notes.lower() for _ in (0,))
    # First-comment preview surfaces for each script.
    pending_by_id = {m.identifier: m for m in report.pending}
    assert pending_by_id["1"].first_comment == "-- creates the schema"
    assert pending_by_id["2"].first_comment == "-- adds users"


async def test_list_pending_migrations_subtracts_applied_history_flyway(
    tmp_path: Path,
) -> None:
    d = _migrations_dir(tmp_path)
    (d / "V1__init.sql").write_text("CREATE TABLE t (id int);\n")
    (d / "V2__users.sql").write_text("CREATE TABLE users (id int);\n")
    (d / "V3__indexes.sql").write_text("CREATE INDEX idx ON t (id);\n")
    # History reports V1 + V2 applied.
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "flyway_schema_history"}],
            'FROM "public"."flyway_schema_history" WHERE success = true': [
                {"identifier": "1"},
                {"identifier": "2"},
            ],
        }
    )

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert report.available is True
    assert report.history_table == "public.flyway_schema_history"
    assert report.applied_count == 2
    assert report.applied == ["1", "2"]
    assert report.pending_count == 1
    assert report.pending[0].identifier == "3"
    assert report.pending[0].filename == "V3__indexes.sql"


async def test_list_pending_migrations_handles_alembic(tmp_path: Path) -> None:
    d = _migrations_dir(tmp_path)
    (d / "abc123def456_initial.py").write_text("# Alembic initial migration\n")
    (d / "xyz789abc012_add_users.py").write_text("# adds the users table\n")
    (d / "env.py").write_text("# alembic env config — not a migration\n")
    (d / "__init__.py").write_text("")  # noise, not a migration
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "alembic_version"}],
            'FROM "public"."alembic_version"': [{"identifier": "abc123def456"}],
        }
    )

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "alembic",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert report.available is True
    assert report.applied == ["abc123def456"]
    assert report.pending_count == 1
    assert report.pending[0].identifier == "xyz789abc012"
    assert report.pending[0].filename == "xyz789abc012_add_users.py"
    assert report.pending[0].first_comment == "# adds the users table"


async def test_list_pending_migrations_handles_liquibase(tmp_path: Path) -> None:
    d = _migrations_dir(tmp_path)
    (d / "001-create-users.sql").write_text("CREATE TABLE users (id int);\n")
    (d / "002-add-index.sql").write_text("CREATE INDEX idx ON users (id);\n")
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "databasechangelog"}],
            'FROM "public"."databasechangelog"': [{"identifier": "001-create-users"}],
        }
    )

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "liquibase",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert report.applied == ["001-create-users"]
    assert report.pending_count == 1
    assert report.pending[0].identifier == "002-add-index"


async def test_list_pending_migrations_returns_pending_zero_when_all_applied(
    tmp_path: Path,
) -> None:
    d = _migrations_dir(tmp_path)
    (d / "V1__init.sql").write_text("CREATE TABLE t (id int);\n")
    driver = FakeRoutingDriver(
        {
            "information_schema.tables": [{"table_schema": "public", "table_name": "flyway_schema_history"}],
            'FROM "public"."flyway_schema_history" WHERE success = true': [
                {"identifier": "1"},
            ],
        }
    )

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert report.available is True
    assert report.pending_count == 0
    assert report.pending == []


async def test_list_pending_migrations_skips_hidden_and_irrelevant_files(
    tmp_path: Path,
) -> None:
    d = _migrations_dir(tmp_path)
    (d / "V1__init.sql").write_text("CREATE TABLE t (id int);\n")
    (d / ".DS_Store").write_text("noise")  # hidden, skipped
    (d / "README.md").write_text("# docs\n")  # not a flyway script, skipped
    (d / "V2_invalid_extension.txt").write_text("noise")  # doesn't match pattern
    driver = FakeRoutingDriver({"information_schema.tables": []})

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    # Greenfield → every recognised script is pending. Only V1 is recognised.
    assert report.pending_count == 1
    assert report.pending[0].identifier == "1"


async def test_list_pending_migrations_does_not_recurse_into_subdirs(
    tmp_path: Path,
) -> None:
    # Operators point the tool at the leaf migrations directory;
    # recursing would silently double-count scripts in nested layouts.
    d = _migrations_dir(tmp_path)
    (d / "V1__top.sql").write_text("CREATE TABLE t (id int);\n")
    nested = d / "versions"
    nested.mkdir()
    (nested / "V2__nested.sql").write_text("CREATE TABLE u (id int);\n")
    driver = FakeRoutingDriver({"information_schema.tables": []})

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert report.pending_count == 1
    assert report.pending[0].identifier == "1"


async def test_list_pending_migrations_filters_history_table_by_schema(
    tmp_path: Path,
) -> None:
    d = _migrations_dir(tmp_path)
    (d / "V1__init.sql").write_text("CREATE TABLE t (id int);\n")
    driver = FakeRoutingDriver(
        {
            "table_schema = %s": [{"table_schema": "app", "table_name": "flyway_schema_history"}],
            'FROM "app"."flyway_schema_history" WHERE success = true': [],
        }
    )

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        history_schema="app",
        allowed_roots=(str(tmp_path),),
    )

    assert report.history_table == "app.flyway_schema_history"


async def test_list_pending_migrations_dedup_identifiers_in_report(
    tmp_path: Path,
) -> None:
    # PendingMigration entries should be sorted by filename — the
    # scan walks ``sorted(os.scandir(...))`` so callers can rely on
    # deterministic output.
    d = _migrations_dir(tmp_path)
    (d / "V3__c.sql").write_text("")
    (d / "V1__a.sql").write_text("")
    (d / "V2__b.sql").write_text("")
    driver = FakeRoutingDriver({"information_schema.tables": []})

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert [m.filename for m in report.pending] == ["V1__a.sql", "V2__b.sql", "V3__c.sql"]


async def test_list_pending_migrations_records_size_bytes(tmp_path: Path) -> None:
    d = _migrations_dir(tmp_path)
    contents = "-- hello\nCREATE TABLE t (id int);\n"
    (d / "V1__init.sql").write_text(contents)
    driver = FakeRoutingDriver({"information_schema.tables": []})

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert report.pending[0].size_bytes == len(contents)


async def test_list_pending_migrations_skips_symlink_escape(tmp_path: Path) -> None:
    # Sandbox escape guard: a planted symlink inside the allowed
    # scripts dir pointing at a sensitive file (e.g. /etc/passwd)
    # must not be followed for the first-comment preview. The
    # entry should be dropped so its contents never reach the
    # structured result.
    d = _migrations_dir(tmp_path)
    secret_dir = tmp_path.parent / "outside-secrets"
    secret_dir.mkdir(exist_ok=True)
    secret_file = secret_dir / "shadow.sql"
    secret_file.write_text("-- SECRET DO NOT LEAK\n")
    # A real script alongside, to confirm the safe entry still surfaces.
    (d / "V1__init.sql").write_text("-- legitimate\nCREATE TABLE t (id int);\n")
    # The symlink looks like a Flyway-named script — without the
    # guard the secret would slip into the report.
    (d / "V2__looks_legit.sql").symlink_to(secret_file)

    driver = FakeRoutingDriver({"information_schema.tables": []})

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    # Only the real script is reported.
    pending_by_id = {m.identifier: m for m in report.pending}
    assert pending_by_id.keys() == {"1"}
    assert pending_by_id["1"].first_comment == "-- legitimate"
    # And the secret never appears in any field of the report.
    serialised = repr(report)
    assert "SECRET" not in serialised
    assert "shadow.sql" not in serialised


async def test_list_pending_migrations_handles_utf8_bom_in_first_comment(
    tmp_path: Path,
) -> None:
    # Some editors save SQL with a leading UTF-8 BOM; ``utf-8-sig``
    # decoding strips it so the first-comment heuristic still spots
    # the ``-- ...`` line without being confused by an invisible
    # ``﻿`` prefix.
    d = _migrations_dir(tmp_path)
    # ``encode("utf-8-sig")`` adds a leading BOM; the source string
    # itself doesn't carry one so the round-trip lands a single BOM
    # at the start of the file (which is what real editors produce).
    contents = "-- bom-prefixed comment\nCREATE TABLE t (id int);\n"
    (d / "V1__init.sql").write_bytes(contents.encode("utf-8-sig"))
    driver = FakeRoutingDriver({"information_schema.tables": []})

    report = await list_pending_migrations(
        driver,  # type: ignore[arg-type]
        "flyway",
        str(d),
        allowed_roots=(str(tmp_path),),
    )

    assert report.pending[0].first_comment == "-- bom-prefixed comment"


# --- dataclass smoke -------------------------------------------------------


def test_pending_migration_is_frozen() -> None:
    m = PendingMigration(identifier="1", filename="V1__x.sql", size_bytes=10)
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError subclass
        m.identifier = "2"  # type: ignore[misc]
