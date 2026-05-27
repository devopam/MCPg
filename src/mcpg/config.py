"""Env-driven, typed configuration for the MCPg server.

Settings are loaded from environment variables prefixed with ``MCPG_``.
``load_settings`` accepts an explicit mapping so it can be tested without
mutating the process environment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from os import environ

from mcpg._vendor.sql import obfuscate_password

# PG role names must be safe identifiers — we inline them into
# ``SET ROLE "<name>"`` so anything outside ``[A-Za-z_][A-Za-z0-9_]*``
# is rejected up front rather than allowed to inject.
_ROLE_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

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
    allow_shell: bool = False
    allow_listen: bool = False
    shell_timeout_sec: int = 60
    shell_max_output_bytes: int = 64 * 1024 * 1024
    listen_queue_max: int = 1000
    audit_persist: bool = False
    pool_min_size: int = 1
    pool_max_size: int = 5
    # HTTP-transport bearer token. When set and the active transport is
    # streamable-http or sse, every request must carry
    # ``Authorization: Bearer <token>``; missing/wrong token returns 401.
    # When unset, the HTTP transport runs without auth (current
    # behaviour). stdio is never gated.
    http_auth_token: str | None = None
    # Multi-tenancy via PG roles. ``default_role`` is applied to every
    # query when set. HTTP requests can override per-request by sending
    # ``X-MCPG-Role: <role>``; when ``allowed_roles`` is set the header
    # value must appear in it (otherwise a 403 is returned). Role names
    # are validated against ``[A-Za-z_][A-Za-z0-9_]*`` regardless.
    default_role: str | None = None
    allowed_roles: tuple[str, ...] = ()
    # NL→SQL helper. ``nl2sql_provider`` is one of "anthropic", "openai",
    # "gemini"; unset means the tool reports unavailable. ``nl2sql_api_key``
    # falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY /
    # GOOGLE_API_KEY when not set explicitly. ``nl2sql_base_url`` lets the
    # OpenAI path target a self-hosted endpoint (Ollama, vLLM, OpenRouter).
    nl2sql_provider: str | None = None
    nl2sql_api_key: str | None = None
    nl2sql_model: str | None = None
    nl2sql_base_url: str | None = None
    nl2sql_max_tokens: int = 2048

    def __repr__(self) -> str:
        # Never let credentials reach logs or tracebacks.
        return (
            f"Settings(database_url={obfuscate_password(self.database_url)!r}, "
            f"access_mode={self.access_mode.value!r}, "
            f"transport={self.transport.value!r}, "
            f"http_host={self.http_host!r}, http_port={self.http_port}, "
            f"log_level={self.log_level!r}, allow_ddl={self.allow_ddl}, "
            f"allow_shell={self.allow_shell}, "
            f"allow_listen={self.allow_listen}, "
            f"shell_timeout_sec={self.shell_timeout_sec}, "
            f"shell_max_output_bytes={self.shell_max_output_bytes}, "
            f"listen_queue_max={self.listen_queue_max}, "
            f"audit_persist={self.audit_persist}, "
            f"pool_min_size={self.pool_min_size}, pool_max_size={self.pool_max_size}, "
            f"http_auth_token={'set' if self.http_auth_token else 'unset'!r}, "
            f"default_role={self.default_role!r}, "
            f"allowed_roles={self.allowed_roles!r}, "
            f"nl2sql_provider={self.nl2sql_provider!r}, "
            f"nl2sql_api_key={'set' if self.nl2sql_api_key else 'unset'!r}, "
            f"nl2sql_model={self.nl2sql_model!r}, "
            f"nl2sql_base_url={self.nl2sql_base_url!r}, "
            f"nl2sql_max_tokens={self.nl2sql_max_tokens})"
        )


def _validate_role_identifier(var: str, value: str) -> None:
    if not _ROLE_IDENTIFIER.match(value):
        raise ConfigError(f"{var} role name {value!r} must match [A-Za-z_][A-Za-z0-9_]* (no quotes / spaces / dashes)")


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

    allow_shell = False
    if (raw := env.get("MCPG_ALLOW_SHELL")) is not None:
        allow_shell = _parse_bool("MCPG_ALLOW_SHELL", raw)

    shell_timeout_sec = 60
    if (raw := env.get("MCPG_SHELL_TIMEOUT_SEC")) is not None:
        shell_timeout_sec = _parse_positive_int("MCPG_SHELL_TIMEOUT_SEC", raw)

    shell_max_output_bytes = 64 * 1024 * 1024
    if (raw := env.get("MCPG_SHELL_MAX_OUTPUT_BYTES")) is not None:
        shell_max_output_bytes = _parse_positive_int("MCPG_SHELL_MAX_OUTPUT_BYTES", raw)

    allow_listen = False
    if (raw := env.get("MCPG_ALLOW_LISTEN")) is not None:
        allow_listen = _parse_bool("MCPG_ALLOW_LISTEN", raw)

    listen_queue_max = 1000
    if (raw := env.get("MCPG_LISTEN_QUEUE_MAX")) is not None:
        listen_queue_max = _parse_positive_int("MCPG_LISTEN_QUEUE_MAX", raw)

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

    http_auth_token: str | None = None
    if (raw := env.get("MCPG_HTTP_AUTH_TOKEN")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_HTTP_AUTH_TOKEN must not be blank when set")
        http_auth_token = stripped

    default_role: str | None = None
    if (raw := env.get("MCPG_DEFAULT_ROLE")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_DEFAULT_ROLE must not be blank when set")
        _validate_role_identifier("MCPG_DEFAULT_ROLE", stripped)
        default_role = stripped

    allowed_roles: tuple[str, ...] = ()
    if (raw := env.get("MCPG_ALLOWED_ROLES")) is not None:
        roles = tuple(r.strip() for r in raw.split(",") if r.strip())
        for role in roles:
            _validate_role_identifier("MCPG_ALLOWED_ROLES", role)
        allowed_roles = roles
        if default_role is not None and default_role not in allowed_roles:
            raise ConfigError(
                f"MCPG_DEFAULT_ROLE ({default_role!r}) is not in MCPG_ALLOWED_ROLES ({list(allowed_roles)!r})"
            )

    nl2sql_provider: str | None = None
    if (raw := env.get("MCPG_NL2SQL_PROVIDER")) is not None:
        candidate = raw.strip().lower()
        if not candidate:
            raise ConfigError("MCPG_NL2SQL_PROVIDER must not be blank when set")
        if candidate not in {"anthropic", "openai", "gemini"}:
            raise ConfigError(f"MCPG_NL2SQL_PROVIDER must be one of: anthropic, openai, gemini (got {raw!r})")
        nl2sql_provider = candidate

    # API key falls back to the vendor's conventional env var so users
    # don't have to duplicate it. Order matters when multiple are set;
    # the explicit MCPG_NL2SQL_API_KEY always wins.
    nl2sql_api_key: str | None = None
    if (raw := env.get("MCPG_NL2SQL_API_KEY")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_NL2SQL_API_KEY must not be blank when set")
        nl2sql_api_key = stripped
    elif nl2sql_provider == "anthropic":
        nl2sql_api_key = (env.get("ANTHROPIC_API_KEY") or "").strip() or None
    elif nl2sql_provider == "openai":
        nl2sql_api_key = (env.get("OPENAI_API_KEY") or "").strip() or None
    elif nl2sql_provider == "gemini":
        # Either GEMINI_API_KEY or the more common GOOGLE_API_KEY.
        nl2sql_api_key = (env.get("GEMINI_API_KEY") or "").strip() or (env.get("GOOGLE_API_KEY") or "").strip() or None

    if nl2sql_provider is not None and nl2sql_api_key is None:
        raise ConfigError(
            f"MCPG_NL2SQL_PROVIDER={nl2sql_provider!r} but no API key found; "
            "set MCPG_NL2SQL_API_KEY (or the vendor's conventional env var)"
        )

    nl2sql_model: str | None = None
    if (raw := env.get("MCPG_NL2SQL_MODEL")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_NL2SQL_MODEL must not be blank when set")
        nl2sql_model = stripped

    nl2sql_base_url: str | None = None
    if (raw := env.get("MCPG_NL2SQL_BASE_URL")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_NL2SQL_BASE_URL must not be blank when set")
        nl2sql_base_url = stripped

    nl2sql_max_tokens = 2048
    if (raw := env.get("MCPG_NL2SQL_MAX_TOKENS")) is not None:
        nl2sql_max_tokens = _parse_positive_int("MCPG_NL2SQL_MAX_TOKENS", raw)
        if nl2sql_max_tokens > 16_384:
            raise ConfigError(f"MCPG_NL2SQL_MAX_TOKENS ({nl2sql_max_tokens}) exceeds the hard cap of 16384")

    return Settings(
        database_url=database_url,
        access_mode=access_mode,
        transport=transport,
        http_host=env.get("MCPG_HTTP_HOST", "127.0.0.1"),
        http_port=http_port,
        log_level=log_level,
        allow_ddl=allow_ddl,
        allow_shell=allow_shell,
        allow_listen=allow_listen,
        shell_timeout_sec=shell_timeout_sec,
        shell_max_output_bytes=shell_max_output_bytes,
        listen_queue_max=listen_queue_max,
        audit_persist=audit_persist,
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
        http_auth_token=http_auth_token,
        default_role=default_role,
        allowed_roles=allowed_roles,
        nl2sql_provider=nl2sql_provider,
        nl2sql_api_key=nl2sql_api_key,
        nl2sql_model=nl2sql_model,
        nl2sql_base_url=nl2sql_base_url,
        nl2sql_max_tokens=nl2sql_max_tokens,
    )
