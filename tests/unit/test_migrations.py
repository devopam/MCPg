"""Unit tests for the staged-migration workflow (ADR-0006)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver
from _mcp_test_helpers import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.migrations import (
    MigrationError,
    _check_identifier,
    _column_clause,
    _make_migration_id,
    _rewrite_index_definition,
    _rewrite_schema_reference,
    _shadow_name_for,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- pure helpers --------------------------------------------------


def test_check_identifier_accepts_plain_names() -> None:
    _check_identifier("widget", "table")  # no raise
    _check_identifier("_private", "table")
    _check_identifier("a1b2c3", "table")


def test_check_identifier_accepts_delimited_names_rejects_non_addressable() -> None:
    # Names needing delimited quoting are accepted (splices quote them via
    # _qi); only empty / NUL / overlong names are rejected.
    _check_identifier('w"; DROP', "table")
    _check_identifier("with space", "schema")
    _check_identifier("1starts_with_digit", "schema")
    with pytest.raises(MigrationError, match="invalid table"):
        _check_identifier("", "table")
    with pytest.raises(MigrationError, match="invalid schema"):
        _check_identifier("with\x00nul", "schema")


def test_make_migration_id_strips_unsafe_chars_and_appends_timestamp() -> None:
    mid = _make_migration_id("add user prefs!")
    # Spaces and ! collapse to underscores; trailing junk stripped.
    assert mid.startswith("add_user_prefs_")
    # Suffix is the millis timestamp — purely digits.
    suffix = mid.rsplit("_", 1)[-1]
    assert suffix.isdigit() and len(suffix) >= 10


def test_make_migration_id_falls_back_to_default_when_name_is_only_junk() -> None:
    mid = _make_migration_id("!!!")
    assert mid.startswith("migration_")


def test_shadow_name_starts_with_the_documented_prefix() -> None:
    name = _shadow_name_for("add_qty_123")
    assert name == "mcpg_shadow_add_qty_123"


def test_column_clause_builds_create_table_fragments() -> None:
    class _Col:
        def __init__(self, name: str, data_type: str, *, nullable: bool, default: str | None) -> None:
            self.name, self.data_type, self.nullable, self.default = name, data_type, nullable, default

    assert _column_clause(_Col("id", "integer", nullable=False, default=None)) == '"id" integer NOT NULL'
    assert _column_clause(_Col("name", "text", nullable=True, default=None)) == '"name" text'
    assert (
        _column_clause(_Col("flag", "boolean", nullable=False, default="false"))
        == '"flag" boolean NOT NULL DEFAULT false'
    )


def test_rewrite_schema_reference_rewrites_only_the_target_schema_in_fk_defs() -> None:
    # FK to the same schema gets rewritten so the intra-schema link
    # survives the clone.
    rewritten = _rewrite_schema_reference("FOREIGN KEY (a) REFERENCES app.parent(id)", "app", "shadow_x")
    assert "REFERENCES " in rewritten
    assert '"shadow_x".' in rewritten or "shadow_x." in rewritten
    # An FK targeting another schema is untouched — diff will surface
    # it as a remaining reference into the original.
    rewritten = _rewrite_schema_reference("FOREIGN KEY (a) REFERENCES other.parent(id)", "app", "shadow_x")
    assert "other." in rewritten
    assert "shadow_x" not in rewritten


def test_rewrite_index_definition_swaps_the_schema_qualifier() -> None:
    sql = "CREATE INDEX idx_x ON app.widget USING btree (name)"
    rewritten = _rewrite_index_definition(sql, "app", "shadow_x")
    assert "shadow_x" in rewritten
    assert "app.widget" not in rewritten


# --- tool wiring gate ---------------------------------------------


_UNRESTRICTED_NO_DDL = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)
_UNRESTRICTED_DDL = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_DDL": "true",
    }
)


async def test_migration_tools_hidden_in_read_only_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "prepare_migration" not in listed
    assert "complete_migration" not in listed
    assert "cancel_migration" not in listed
    assert "list_pending_migrations" not in listed


async def test_migration_tools_hidden_in_unrestricted_without_allow_ddl() -> None:
    # Defence-in-depth: unrestricted alone isn't enough; the underlying
    # ops are DDL so the migration family piggybacks on MCPG_ALLOW_DDL.
    server = create_server(_UNRESTRICTED_NO_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "prepare_migration" not in listed


async def test_migration_tools_registered_with_unrestricted_and_allow_ddl() -> None:
    server = create_server(_UNRESTRICTED_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {
        "prepare_migration",
        "validate_migration",
        "validate_migration_schema",
        "complete_migration",
        "cancel_migration",
        "list_pending_migrations",
    } <= listed


# --- input validation in prepare_migration ------------------------


async def test_prepare_migration_rejects_blank_name() -> None:
    from mcpg.migrations import prepare_migration

    with pytest.raises(MigrationError, match="name"):
        await prepare_migration(
            FakeDriver(),  # type: ignore[arg-type]
            name="   ",
            target_schema="app",
            candidate_sql="ALTER TABLE w ADD c int",
        )


async def test_prepare_migration_rejects_blank_candidate_sql() -> None:
    from mcpg.migrations import prepare_migration

    with pytest.raises(MigrationError, match="candidate_sql"):
        await prepare_migration(
            FakeDriver(),  # type: ignore[arg-type]
            name="bad",
            target_schema="app",
            candidate_sql="",
        )


async def test_prepare_migration_rejects_zero_ttl() -> None:
    from mcpg.migrations import prepare_migration

    with pytest.raises(MigrationError, match="ttl_minutes"):
        await prepare_migration(
            FakeDriver(),  # type: ignore[arg-type]
            name="bad",
            target_schema="app",
            candidate_sql="ALTER TABLE w ADD c int",
            ttl_minutes=0,
        )


async def test_prepare_migration_rejects_unsafe_target_schema() -> None:
    from mcpg.migrations import prepare_migration

    with pytest.raises(MigrationError, match="invalid target_schema"):
        await prepare_migration(
            FakeDriver(),  # type: ignore[arg-type]
            name="bad",
            target_schema="app\x00evil",
            candidate_sql="ALTER TABLE w ADD c int",
        )


class _FailingDropSchemaDriver:
    """FakeRoutingDriver-alike whose ``DROP SCHEMA`` statements always
    raise — used to exercise the "shadow cleanup itself fails" branch of
    ``prepare_migration``'s except handler, on top of an outer failure
    (a non-transactional candidate statement) that triggers the cleanup
    in the first place."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def execute_query(self, query: str, params: Any = None, *, force_readonly: bool = False) -> Any:
        self.calls.append((query, params, force_readonly))
        if "DROP SCHEMA" in query:
            raise RuntimeError("cannot drop schema: schema is being accessed by other users")
        return []


async def test_prepare_migration_logs_debug_when_shadow_cleanup_also_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the candidate SQL fails to apply AND the shadow-schema cleanup
    itself fails, the cleanup failure is now logged at debug (not
    silently), and the original error still propagates via the trailing
    ``raise``."""
    import logging

    from mcpg.migrations import prepare_migration

    driver = _FailingDropSchemaDriver()

    root_logger = logging.getLogger("mcpg")
    old_propagate = root_logger.propagate
    root_logger.propagate = True
    try:
        caplog.set_level(logging.DEBUG, logger="mcpg.migrations")

        # VACUUM cannot run inside a transaction block — _execute_in_schema
        # rejects it immediately, entering prepare_migration's cleanup path
        # without needing a real connection pool.
        with pytest.raises(MigrationError, match="cannot run inside a transaction"):
            await prepare_migration(
                driver,  # type: ignore[arg-type]
                name="bad_vacuum",
                target_schema="app",
                candidate_sql="VACUUM;",
            )
    finally:
        root_logger.propagate = old_propagate

    matches = [
        r
        for r in caplog.records
        if r.name == "mcpg.migrations" and "shadow schema" in r.message.lower() and r.levelno == logging.DEBUG
    ]
    assert len(matches) == 1
    # exc_info=True must actually attach a traceback, not just the message.
    assert matches[0].exc_info is not None


# --- validate_migration (Phase 9.2) -------------------------------


async def test_validate_migration_rejects_blank_candidate_sql() -> None:
    from mcpg.migrations import validate_migration

    with pytest.raises(MigrationError, match="candidate_sql"):
        await validate_migration(
            FakeDriver(),  # type: ignore[arg-type]
            target_schema="app",
            candidate_sql="",
        )


async def test_validate_migration_rejects_unsafe_target_schema() -> None:
    from mcpg.migrations import validate_migration

    with pytest.raises(MigrationError, match="invalid target_schema"):
        await validate_migration(
            FakeDriver(),  # type: ignore[arg-type]
            target_schema="app\x00evil",
            candidate_sql="ALTER TABLE w ADD c int",
        )


async def test_validate_migration_rejects_negative_sample_size() -> None:
    from mcpg.migrations import validate_migration

    with pytest.raises(MigrationError, match="sample_rows_per_table"):
        await validate_migration(
            FakeDriver(),  # type: ignore[arg-type]
            target_schema="app",
            candidate_sql="ALTER TABLE w ADD c int",
            sample_rows_per_table=-1,
        )


# --- MigrationRecord dataclass shape ------------------------------


def test_migration_record_completed_at_is_optional() -> None:
    from mcpg.migrations import MigrationRecord

    now = datetime.now(UTC)
    rec = MigrationRecord(
        id="x",
        prepared_at=now,
        target_schema="app",
        shadow_schema="mcpg_shadow_x",
        candidate_sql="x",
        status="prepared",
        ttl_expires_at=now + timedelta(minutes=5),
    )
    assert rec.completed_at is None


# --- validate_migration_schema (E3) -------------------------------


async def test_validate_migration_schema_rejects_blank_candidate_sql() -> None:
    from mcpg.migrations import validate_migration_schema

    with pytest.raises(MigrationError, match="candidate_sql"):
        await validate_migration_schema(
            FakeDriver(),  # type: ignore[arg-type]
            target_schema="app",
            reference_schema="ref",
            candidate_sql="",
        )


async def test_validate_migration_schema_rejects_unsafe_target_schema() -> None:
    from mcpg.migrations import validate_migration_schema

    with pytest.raises(MigrationError, match="invalid target_schema"):
        await validate_migration_schema(
            FakeDriver(),  # type: ignore[arg-type]
            target_schema="app\x00evil",
            reference_schema="ref",
            candidate_sql="ALTER TABLE w ADD c int",
        )


async def test_validate_migration_schema_rejects_unsafe_reference_schema() -> None:
    from mcpg.migrations import validate_migration_schema

    with pytest.raises(MigrationError, match="invalid reference_schema"):
        await validate_migration_schema(
            FakeDriver(),  # type: ignore[arg-type]
            target_schema="app",
            reference_schema="ref\x00evil",
            candidate_sql="ALTER TABLE w ADD c int",
        )
