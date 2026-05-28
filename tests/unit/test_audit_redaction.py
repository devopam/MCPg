"""Tests for the tool-invocation audit redactor in :mod:`mcpg.audit`.

Covers the secret-name regex (default patterns + the
``MCPG_AUDIT_REDACT_KEYS`` extension knob), recursive walking of
nested arguments, and the wiring from ``load_settings`` into the
process-level redaction pattern.
"""

from __future__ import annotations

import pytest

from mcpg import audit
from mcpg.config import load_settings

_DB_URL = "postgresql://u:p@localhost/db"


@pytest.fixture(autouse=True)
def _reset_pattern() -> None:
    """Reset the module pattern between tests so extensions don't leak."""
    audit.configure_redaction({})


def test_redact_arguments_masks_default_secret_keys() -> None:
    redacted = audit.redact_arguments({"sql": "SELECT 1", "password": "hunter2", "api_key": "sk-abc", "token": "tk"})
    assert redacted["password"] == "****"
    assert redacted["api_key"] == "****"
    assert redacted["token"] == "****"
    # Non-credential keys pass through.
    assert redacted["sql"] == "SELECT 1"


def test_redact_arguments_matches_keys_case_insensitively() -> None:
    redacted = audit.redact_arguments({"PGPASSWORD": "hunter2", "Bearer": "abc", "AUTHORIZATION": "xyz"})
    assert redacted["PGPASSWORD"] == "****"
    assert redacted["Bearer"] == "****"
    assert redacted["AUTHORIZATION"] == "****"


def test_redact_arguments_matches_keys_as_substrings() -> None:
    # ``user_password`` and ``app.api_key`` both contain a default
    # pattern as a substring.
    redacted = audit.redact_arguments({"user_password": "hunter2", "app.api_key": "sk-abc"})
    assert redacted["user_password"] == "****"
    assert redacted["app.api_key"] == "****"


def test_redact_arguments_walks_nested_dicts_lists_tuples() -> None:
    payload = {
        "rows": [
            {"id": 1, "password": "hunter2"},
            {"id": 2, "token": "tk_abc"},
        ],
        "creds": ({"api_key": "sk-x"},),
        "schema": "app",
    }
    redacted = audit.redact_arguments(payload)
    assert redacted["rows"][0]["password"] == "****"
    assert redacted["rows"][1]["token"] == "****"
    assert redacted["creds"][0]["api_key"] == "****"
    # Tuple type is preserved through the walk.
    assert isinstance(redacted["creds"], tuple)
    # Non-sensitive scalars pass through.
    assert redacted["schema"] == "app"
    assert redacted["rows"][0]["id"] == 1


def test_redact_arguments_obfuscates_dsn_password_in_string_leaves() -> None:
    redacted = audit.redact_arguments({"url": "postgresql://u:hunter2@host/db"})
    assert "hunter2" not in redacted["url"]
    assert "****" in redacted["url"]


def test_redact_arguments_passes_through_non_string_scalars() -> None:
    redacted = audit.redact_arguments({"limit": 100, "active": True, "result": None})
    assert redacted == {"limit": 100, "active": True, "result": None}


def test_configure_redaction_picks_up_extra_keys_from_env_var() -> None:
    audit.configure_redaction({"MCPG_AUDIT_REDACT_KEYS": "session_id,csrf"})
    redacted = audit.redact_arguments({"session_id": "abc123", "csrf": "xyz", "password": "hunter2"})
    assert redacted["session_id"] == "****"
    assert redacted["csrf"] == "****"
    # Defaults still apply on top of the extensions.
    assert redacted["password"] == "****"


def test_configure_redaction_accepts_regex_fragments() -> None:
    # The extension knob takes regex fragments, so the operator can
    # match a family of keys with one entry.
    audit.configure_redaction({"MCPG_AUDIT_REDACT_KEYS": "user_id_\\d+"})
    redacted = audit.redact_arguments({"user_id_42": "alice", "user_id_99": "bob"})
    assert redacted["user_id_42"] == "****"
    assert redacted["user_id_99"] == "****"


def test_load_settings_arms_the_audit_redaction_pattern() -> None:
    # ``load_settings`` is the canonical configuration entry point; it
    # must propagate MCPG_AUDIT_REDACT_KEYS into the audit module so
    # the very first tool call honours the extended pattern.
    load_settings({"MCPG_DATABASE_URL": _DB_URL, "MCPG_AUDIT_REDACT_KEYS": "tenant_secret"})
    redacted = audit.redact_arguments({"tenant_secret": "abc", "ordinary": "value"})
    assert redacted["tenant_secret"] == "****"
    assert redacted["ordinary"] == "value"
