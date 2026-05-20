"""Audit logging of tool invocations.

Every tool call is recorded to the ``mcpg.audit`` logger with the tool name,
its arguments (with secrets masked), and the outcome. How those records are
persisted or shipped is left to the deployment's logging configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import obfuscate_password

audit_logger = logging.getLogger("mcpg.audit")

# Argument names whose values are masked wholesale, regardless of content.
_SECRET_KEYS = frozenset({"password", "secret", "token", "database_url", "dsn", "conninfo"})
_MASK = "****"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A record of a single tool invocation."""

    tool: str
    arguments: dict[str, Any]
    status: str
    error: str | None = None


def redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of tool arguments with sensitive values masked.

    Arguments named like a credential are masked entirely; string arguments
    have any embedded connection-string password obfuscated.
    """
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        if key.lower() in _SECRET_KEYS:
            safe[key] = _MASK
        elif isinstance(value, str):
            safe[key] = obfuscate_password(value)
        else:
            safe[key] = value
    return safe


def record(event: AuditEvent) -> None:
    """Emit an audit event to the ``mcpg.audit`` logger."""
    safe_arguments = redact_arguments(event.arguments)
    if event.error is None:
        audit_logger.info("tool=%s status=%s arguments=%s", event.tool, event.status, safe_arguments)
    else:
        audit_logger.warning(
            "tool=%s status=%s arguments=%s error=%s",
            event.tool,
            event.status,
            safe_arguments,
            event.error,
        )
