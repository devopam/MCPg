"""Env-driven, typed configuration for the MCPg server.

Settings are loaded from environment variables prefixed with ``MCPG_``.
``load_settings`` accepts an explicit mapping so it can be tested without
mutating the process environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from os import environ

from mcpg._vendor.sql import obfuscate_password

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


class ConfigError(Exception):
    """Raised when the environment configuration is missing or invalid."""


class AccessMode(StrEnum):
    """How much the server is allowed to do to the database."""

    READ_ONLY = "read-only"
    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"


class Transport(StrEnum):
    """MCP transport the server listens on."""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"
    SSE = "sse"


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, immutable server configuration."""

    database_url: str
    access_mode: AccessMode = AccessMode.READ_ONLY
    transport: Transport = Transport.STDIO
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    log_level: str = "INFO"
    allow_ddl: bool = False
    audit_persist: bool = False
    pool_min_size: int = 1
    pool_max_size: int = 5

    def __repr__(self) -> str:
        # Never let credentials reach logs or tracebacks.
        return (
            f"Settings(database_url={obfuscate_password(self.database_url)!r}, "
            f"access_mode={self.access_mode.value!r}, "
            f"transport={self.transport.value!r}, "
            f"http_host={self.http_host!r}, http_port={self.http_port}, "
            f"log_level={self.log_level!r}, allow_ddl={self.allow_ddl}, "
            f"audit_persist={self.audit_persist}, "
            f"pool_min_size={self.pool_min_size}, pool_max_size={self.pool_max_size})"
        )


def _parse_enum[E: StrEnum](var: str, raw: str, enum: type[E]) -> E:
    try:
        return enum(raw.strip().lower())
    except ValueError:
        valid = ", ".join(member.value for member in enum)
        raise ConfigError(f"{var} must be one of: {valid} (got {raw!r})") from None


def _parse_bool(var: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(f"{var} must be a boolean (got {raw!r})")


def _parse_port(var: str, raw: str) -> int:
    try:
        port = int(raw)
    except ValueError:
        raise ConfigError(f"{var} must be an integer (got {raw!r})") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"{var} must be between 1 and 65535 (got {port})")
    return port


def _parse_positive_int(var: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{var} must be an integer (got {raw!r})") from None
    if value < 1:
        raise ConfigError(f"{var} must be at least 1 (got {value})")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from environment variables.

    Args:
        env: Mapping to read from; defaults to the process environment.

    Raises:
        ConfigError: If a required variable is missing or any value is invalid.
    """
    env = environ if env is None else env

    database_url = env.get("MCPG_DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("MCPG_DATABASE_URL is required")

    access_mode = AccessMode.READ_ONLY
    if (raw := env.get("MCPG_ACCESS_MODE")) is not None:
        access_mode = _parse_enum("MCPG_ACCESS_MODE", raw, AccessMode)

    transport = Transport.STDIO
    if (raw := env.get("MCPG_TRANSPORT")) is not None:
        transport = _parse_enum("MCPG_TRANSPORT", raw, Transport)

    http_port = 8000
    if (raw := env.get("MCPG_HTTP_PORT")) is not None:
        http_port = _parse_port("MCPG_HTTP_PORT", raw)

    log_level = env.get("MCPG_LOG_LEVEL", "INFO").strip().upper()
    if log_level not in _LOG_LEVELS:
        valid = ", ".join(sorted(_LOG_LEVELS))
        raise ConfigError(f"MCPG_LOG_LEVEL must be one of: {valid} (got {log_level!r})")

    allow_ddl = False
    if (raw := env.get("MCPG_ALLOW_DDL")) is not None:
        allow_ddl = _parse_bool("MCPG_ALLOW_DDL", raw)

    audit_persist = False
    if (raw := env.get("MCPG_AUDIT_PERSIST")) is not None:
        audit_persist = _parse_bool("MCPG_AUDIT_PERSIST", raw)

    pool_min_size = 1
    if (raw := env.get("MCPG_POOL_MIN_SIZE")) is not None:
        pool_min_size = _parse_positive_int("MCPG_POOL_MIN_SIZE", raw)

    pool_max_size = 5
    if (raw := env.get("MCPG_POOL_MAX_SIZE")) is not None:
        pool_max_size = _parse_positive_int("MCPG_POOL_MAX_SIZE", raw)

    if pool_max_size < pool_min_size:
        raise ConfigError(f"MCPG_POOL_MAX_SIZE ({pool_max_size}) must be >= MCPG_POOL_MIN_SIZE ({pool_min_size})")

    return Settings(
        database_url=database_url,
        access_mode=access_mode,
        transport=transport,
        http_host=env.get("MCPG_HTTP_HOST", "127.0.0.1"),
        http_port=http_port,
        log_level=log_level,
        allow_ddl=allow_ddl,
        audit_persist=audit_persist,
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
    )
