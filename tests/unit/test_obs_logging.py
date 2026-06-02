"""Unit tests for the structured logging module (obs_logging.py)."""

from __future__ import annotations

import json
import logging

from mcpg.config import load_settings
from mcpg.obs_logging import JSONFormatter, setup_logging


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
