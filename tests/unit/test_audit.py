"""Tests for audit logging of tool invocations."""

import logging

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.audit import AuditEvent, record, redact_arguments
from mcpg.config import load_settings
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- argument redaction ----------------------------------------------------


def test_redact_masks_secret_named_arguments() -> None:
    redacted = redact_arguments({"password": "hunter2", "database_url": "postgres://x", "schema": "app"})

    assert redacted == {"password": "****", "database_url": "****", "schema": "app"}


def test_redact_is_case_insensitive_for_secret_keys() -> None:
    assert redact_arguments({"Password": "hunter2"}) == {"Password": "****"}


def test_redact_obfuscates_passwords_embedded_in_string_values() -> None:
    redacted = redact_arguments({"sql": "postgresql://user:secret@host/db"})

    assert "secret" not in redacted["sql"]


def test_redact_leaves_non_string_values_untouched() -> None:
    assert redact_arguments({"max_rows": 100, "include_system": True}) == {
        "max_rows": 100,
        "include_system": True,
    }


# --- record ----------------------------------------------------------------


def test_record_emits_ok_events_at_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="mcpg.audit"):
        record(AuditEvent(tool="list_schemas", arguments={}, status="ok"))

    assert "tool=list_schemas" in caplog.text
    assert "status=ok" in caplog.text


def test_record_emits_error_events_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="mcpg.audit"):
        record(AuditEvent(tool="run_select", arguments={}, status="error", error="rejected"))

    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "error=rejected" in caplog.text


def test_record_never_logs_secret_values(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="mcpg.audit"):
        record(AuditEvent(tool="connect", arguments={"password": "hunter2"}, status="ok"))

    assert "hunter2" not in caplog.text


# --- integration with the server -------------------------------------------


async def test_successful_tool_call_emits_an_ok_audit_event(caplog: pytest.LogCaptureFixture) -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger="mcpg.audit"):
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("get_server_info", {})

    assert "tool=get_server_info" in caplog.text
    assert "status=ok" in caplog.text


async def test_failing_tool_call_emits_an_error_audit_event(caplog: pytest.LogCaptureFixture) -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger="mcpg.audit"):
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("run_select", {"sql": "DROP TABLE users"})

    assert "tool=run_select" in caplog.text
    assert "status=error" in caplog.text
