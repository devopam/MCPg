"""Observability Logging — Structured JSON logging and setup for MCPg loggers."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mcpg.sql import obfuscate_password

if TYPE_CHECKING:
    from mcpg.config import Settings


class RedactionFilter(logging.Filter):
    """Backstop redaction: scrubs a password-bearing connection string out of a log call's
    message or %-style arguments when a call site reaches a handler without having passed
    it through obfuscate_password() first.

    Not a replacement for calling obfuscate_password() explicitly where a value is known to
    carry credentials — that per-call-site discipline still matters for accuracy (this filter
    only recognizes the same connection-string shapes obfuscate_password() already does). This
    is the centralized enforcement layer for the case a future call site forgets.

    Scope: covers only `record.msg` / `record.args` (the log call's own message and its
    lazy-formatting arguments). It does NOT cover `record.exc_info` — an exception's traceback
    (e.g. from `logger.exception(...)`) is rendered separately by
    `logging.Formatter.formatException()`, which never passes through this filter, so a
    password embedded in an exception's message can still leak via a formatter's "exception"
    output even with this filter attached.

    Runs before formatting (logging.Filter operates on the LogRecord, not rendered output), so
    it renders any lazy %-style args via getMessage() up front and clears record.args — this
    both avoids leaving unredacted args sitting on the record and prevents a formatter that
    calls getMessage() again (e.g. JSONFormatter) from re-applying % substitution to the
    already-rendered, now-static message string.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = obfuscate_password(record.getMessage())
        record.args = ()
        return True


class JSONFormatter(logging.Formatter):
    """Formatter that outputs structured JSON for log events.

    For audit events (from the ``mcpg.audit`` logger), it parses the message
    JSON payload and merges it into the top-level keys of the log dictionary.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Standardised RFC 3339 UTC timestamp with millisecond precision
        dt = datetime.fromtimestamp(record.created, UTC)
        timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z"

        log_data = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Merge fields directly if this is an audit event
        if record.name == "mcpg.audit":
            try:
                audit_payload = json.loads(record.getMessage())
                if isinstance(audit_payload, dict):
                    log_data.update(audit_payload)
            except (json.JSONDecodeError, TypeError):
                pass

        return json.dumps(log_data)


def setup_logging(settings: Settings) -> None:
    """Configure the level and format for the package-level ``mcpg`` logger.

    Clears any existing handlers on the ``mcpg`` logger to prevent duplicate logs
    in re-entrant test environments, sets the configured log level, and sets
    up either standard text or structured JSON formatting on stderr.
    """
    from mcpg.audit import configure_log_format

    configure_log_format(settings.log_format)

    logger = logging.getLogger("mcpg")
    logger.setLevel(settings.log_level)

    # De-duplicate handlers to prevent duplicate output in tests/re-initialisation
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)

    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    handler.setFormatter(formatter)
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)

    # Disable propagation to prevent double-logging if root has handlers
    logger.propagate = False
