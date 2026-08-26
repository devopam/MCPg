"""Unit tests for the structured logging module (obs_logging.py)."""

from __future__ import annotations

import json
import logging

import pytest

from mcpg.config import load_settings
from mcpg.obs_logging import JSONFormatter, RedactionFilter, setup_logging


def test_json_formatter_formats_standard_record() -> None:
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="mcpg.server",
        level=logging.INFO,
        pathname="server.py",
        lineno=42,
        msg="Starting server on port %d",
        args=(8000,),
        exc_info=None,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert "timestamp" in data
    assert data["timestamp"].endswith("Z")
    assert data["level"] == "INFO"
    assert data["logger"] == "mcpg.server"
    assert data["message"] == "Starting server on port 8000"
    assert "exception" not in data


def test_json_formatter_includes_exceptions() -> None:
    formatter = JSONFormatter()
    try:
        raise ValueError("Something went wrong")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="mcpg.server",
        level=logging.ERROR,
        pathname="server.py",
        lineno=42,
        msg="An error occurred",
        args=(),
        exc_info=exc_info,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "ERROR"
    assert "exception" in data
    assert "ValueError: Something went wrong" in data["exception"]


def test_json_formatter_merges_audit_payload() -> None:
    formatter = JSONFormatter()
    audit_msg = json.dumps(
        {
            "tool": "list_tables",
            "status": "ok",
            "arguments": {"schema": "public"},
        }
    )
    record = logging.LogRecord(
        name="mcpg.audit",
        level=logging.INFO,
        pathname="audit.py",
        lineno=100,
        msg=audit_msg,
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "INFO"
    assert data["logger"] == "mcpg.audit"
    # Merged keys
    assert data["tool"] == "list_tables"
    assert data["status"] == "ok"
    assert data["arguments"] == {"schema": "public"}


def test_json_formatter_handles_malformed_audit_payload() -> None:
    formatter = JSONFormatter()
    # Non-JSON or malformed payload in mcpg.audit should not crash the formatter
    record = logging.LogRecord(
        name="mcpg.audit",
        level=logging.INFO,
        pathname="audit.py",
        lineno=100,
        msg="not-valid-json",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "INFO"
    assert data["logger"] == "mcpg.audit"
    assert data["message"] == "not-valid-json"


def test_setup_logging_configures_logger() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_LOG_LEVEL": "DEBUG",
            "MCPG_LOG_FORMAT": "json",
        }
    )

    logger = logging.getLogger("mcpg")
    # Reset logger state to mock
    logger.handlers.clear()
    logger.propagate = True

    setup_logging(settings)

    assert logger.level == logging.DEBUG
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0].formatter, JSONFormatter)
    assert logger.propagate is False


def test_setup_logging_synchronizes_audit_format() -> None:
    from mcpg.audit import configure_log_format

    configure_log_format("text")

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_LOG_FORMAT": "json",
        }
    )
    setup_logging(settings)

    from mcpg.audit import _log_format as current_format

    assert current_format == "json"


def test_redaction_filter_scrubs_a_connection_string_even_when_a_call_site_forgot(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The centralized filter catches a password-bearing log line even without obfuscate_password."""
    # Other tests in this module (e.g. test_setup_logging_configures_logger) mutate the shared
    # "mcpg" logger's propagate flag; force it True here so caplog (attached to the root logger)
    # reliably observes records regardless of test order.
    monkeypatch.setattr(logging.getLogger("mcpg"), "propagate", True)
    logger = logging.getLogger("mcpg.test_redaction")
    logger.addFilter(RedactionFilter())
    with caplog.at_level(logging.INFO, logger="mcpg.test_redaction"):
        logger.info("connecting to postgresql://user:hunter2@host/db")
    assert "hunter2" not in caplog.text
    assert "****" in caplog.text


def test_redaction_filter_correctly_renders_percent_style_args(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A %-style lazy-formatted log call must render its args exactly once, not double-substitute.

    RedactionFilter renders record.msg % record.args via getMessage(), then clears record.args
    so a later formatter call (which also invokes getMessage()) doesn't re-apply substitution.
    """
    monkeypatch.setattr(logging.getLogger("mcpg"), "propagate", True)
    logger = logging.getLogger("mcpg.test_redaction_args")
    logger.addFilter(RedactionFilter())
    with caplog.at_level(logging.INFO, logger="mcpg.test_redaction_args"):
        logger.info("connecting to %s as %s", "postgresql://user:hunter2@host/db", "app_user")
    record = caplog.records[0]

    # The password must be redacted in the fully rendered message.
    assert "hunter2" not in record.getMessage()
    assert "****" in record.getMessage()
    # The user value substituted correctly (no leftover %s placeholders, no literal tuple repr).
    assert "app_user" in record.getMessage()
    assert "%s" not in record.getMessage()
    # args must be cleared so a second getMessage() call (as JSONFormatter performs) does not
    # attempt to re-substitute against the (now redacted, %-containing-free) rendered string.
    assert record.args == ()
    assert record.getMessage() == record.getMessage()


def test_redaction_filter_passes_through_already_obfuscated_message(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message that already went through obfuscate_password() at the call site is unchanged."""
    from mcpg.sql import obfuscate_password

    monkeypatch.setattr(logging.getLogger("mcpg"), "propagate", True)
    logger = logging.getLogger("mcpg.test_redaction_preobfuscated")
    logger.addFilter(RedactionFilter())
    already_safe = obfuscate_password("connecting to postgresql://user:hunter2@host/db")
    with caplog.at_level(logging.INFO, logger="mcpg.test_redaction_preobfuscated"):
        logger.info(already_safe)
    assert "hunter2" not in caplog.text
    assert caplog.records[0].getMessage() == already_safe


def test_setup_logging_attaches_redaction_filter_to_its_handler() -> None:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_LOG_LEVEL": "DEBUG",
            "MCPG_LOG_FORMAT": "json",
        }
    )

    logger = logging.getLogger("mcpg")
    logger.handlers.clear()
    logger.propagate = True

    setup_logging(settings)

    assert len(logger.handlers) == 1
    handler = logger.handlers[0]
    assert any(isinstance(f, RedactionFilter) for f in handler.filters)
