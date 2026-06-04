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
from os.path import isabs

from mcpg._vendor.sql import obfuscate_password
from mcpg.nl2sql import VENDOR_ENV_VAR_HINT
from mcpg.secrets import SecretsError, build_secrets_provider

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
    log_format: str = "text"
    allow_ddl: bool = False
    allow_shell: bool = False
    allow_listen: bool = False
    shell_timeout_sec: int = 60
    shell_max_output_bytes: int = 64 * 1024 * 1024
    # Subprocess hardening for the shell-gated PG binaries. All opt-in.
    # ``subprocess_bin_allowlist`` is a tuple of absolute directories the
    # resolved pg_dump / pg_restore / psql binary must live under (empty
    # = trust PATH). ``subprocess_cpu_seconds`` / ``subprocess_memory_mb``
    # apply RLIMIT_CPU / RLIMIT_AS to each child on POSIX (None = inherit).
    subprocess_bin_allowlist: tuple[str, ...] = ()
    subprocess_cpu_seconds: int | None = None
    subprocess_memory_mb: int | None = None
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
    # HTTP transport authentication mode. ``static`` (the default) does
    # constant-time comparison against ``http_auth_token``. ``oidc``
    # validates the bearer JWT against the configured OIDC provider's
    # JWKS — see ``mcpg.oidc`` for the verification flow.
    auth_mode: str = "static"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    # When set, the named claim's value becomes the per-request PG role
    # (composes with the Phase-1.4 tenancy driver). Typical claims:
    # ``pg_role``, ``preferred_username``.
    oidc_role_claim: str | None = None
    # Multi-tenancy via PG roles. ``default_role`` is applied to every
    # query when set. HTTP requests can override per-request by sending
    # ``X-MCPG-Role: <role>``; when ``allowed_roles`` is set the header
    # value must appear in it (otherwise a 403 is returned). Role names
    # are validated against ``[A-Za-z_][A-Za-z0-9_]*`` regardless.
    default_role: str | None = None
    allowed_roles: tuple[str, ...] = ()
    # Read-replica routing. When ``replica_urls`` is non-empty, every
    # ``force_readonly=True`` query is round-robin-routed to a
    # healthy replica; writes always go to the primary. On replica
    # failure the call falls back to the primary once and the
    # replica is degraded for 30s.
    replica_urls: tuple[str, ...] = ()
    # NL→SQL helper.
    # ``nl2sql_provider`` is the DEFAULT provider used when the
    # ``translate_nl_to_sql`` tool is called without an explicit ``provider``
    # argument. ``nl2sql_api_keys`` is the full set of (provider, key) pairs
    # MCPg discovered at startup — every configured provider is callable
    # via the tool's ``provider=`` argument, not just the default. Operators
    # set ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``GEMINI_API_KEY`` (or
    # ``GOOGLE_API_KEY``) in the env and MCPg makes each one available; when
    # ``MCPG_NL2SQL_PROVIDER`` is unset, MCPg picks a default in preference
    # order anthropic → openai → gemini. ``MCPG_NL2SQL_API_KEY``, when set,
    # supplies the key for the configured default provider (and requires
    # ``MCPG_NL2SQL_PROVIDER`` to be set so MCPg knows which provider to
    # assign it to). ``nl2sql_model`` / ``nl2sql_base_url`` apply only when
    # the call uses the default provider; an explicit ``provider=`` override
    # falls back to that provider's default model.
    nl2sql_provider: str | None = None
    nl2sql_api_keys: tuple[tuple[str, str], ...] = ()
    nl2sql_model: str | None = None
    nl2sql_base_url: str | None = None
    nl2sql_max_tokens: int = 2048
    rate_limit_enabled: bool = False
    rate_limit_max_requests: int = 60
    rate_limit_window_seconds: int = 60
    rate_limit_heavy_max: int = 5
    rate_limit_heavy_window: int = 60
    # When True, MCPg accepts a database / replica URL whose sslmode is
    # disable / allow / prefer even for non-loopback hosts. Off by default
    # so a misconfigured production deployment fails closed at startup.
    allow_insecure_tls: bool = False
    statement_timeout_ms: int = 30000
    lock_timeout_ms: int = 5000
    slow_call_threshold_ms: int = 1000
    # OpenTelemetry tracing — one span per `call_tool` invocation,
    # behind the ``mcpg[otel]`` extra. Disabled by default so the
    # baseline runtime has no OTel cost. ``otel_service_name`` only
    # takes effect when ``OTEL_RESOURCE_ATTRIBUTES`` doesn't already
    # supply a ``service.name``; every other setting (endpoint,
    # headers, sampler) comes from the standard ``OTEL_*`` env vars.
    otel_enabled: bool = False
    otel_service_name: str = "mcpg"
    # On-disk migration-script roots the `list_pending_migrations`
    # tool is allowed to read. Empty (default) = the tool refuses
    # every path; operators opt in by setting
    # ``MCPG_MIGRATION_SCRIPTS_ROOTS`` to a colon-separated list of
    # absolute directories. A requested ``scripts_dir`` is resolved
    # (symlinks dereferenced) and must live under one of these.
    migration_scripts_roots: tuple[str, ...] = ()
    cache_enabled: bool = True
    cache_ttl_seconds: int = 300
    cache_maxsize: int = 1024
    redis_url: str | None = None
    enable_heavy_diagnostics: bool = True
    http_max_body_bytes: int = 1048576
    http_allowed_origins: tuple[str, ...] = ()
    http_hsts_max_age: int = 31536000
    # Per-request wall-clock cap for the HTTP transports. 0 = disabled
    # (default), because a hard cap also severs long-lived SSE /
    # streamable-http streams. Set a positive value for plain
    # request/response deployments that want a DoS backstop.
    http_request_timeout_seconds: int = 0
    shutdown_drain_seconds: int = 30
    audit_hmac_key: str | None = None
    audit_integrity: bool = False
    # Which secrets backend resolved API keys / bearer token / HMAC key
    # at startup: "env" (default) or "file". Recorded for observability;
    # the actual values never appear here.
    secrets_backend: str = "env"

    def __repr__(self) -> str:
        # Never let credentials reach logs or tracebacks.
        http_auth_token_repr = "set" if self.http_auth_token else "unset"
        audit_hmac_key_repr = "set" if self.audit_hmac_key else "unset"
        return (
            f"Settings(database_url={obfuscate_password(self.database_url)!r}, "
            f"access_mode={self.access_mode.value!r}, "
            f"transport={self.transport.value!r}, "
            f"http_host={self.http_host!r}, http_port={self.http_port}, "
            f"log_level={self.log_level!r}, log_format={self.log_format!r}, "
            f"allow_ddl={self.allow_ddl}, "
            f"allow_shell={self.allow_shell}, "
            f"allow_listen={self.allow_listen}, "
            f"shell_timeout_sec={self.shell_timeout_sec}, "
            f"shell_max_output_bytes={self.shell_max_output_bytes}, "
            f"subprocess_bin_allowlist={self.subprocess_bin_allowlist!r}, "
            f"subprocess_cpu_seconds={self.subprocess_cpu_seconds}, "
            f"subprocess_memory_mb={self.subprocess_memory_mb}, "
            f"listen_queue_max={self.listen_queue_max}, "
            f"audit_persist={self.audit_persist}, "
            f"pool_min_size={self.pool_min_size}, pool_max_size={self.pool_max_size}, "
            f"http_auth_token={http_auth_token_repr!r}, "
            f"auth_mode={self.auth_mode!r}, "
            f"oidc_issuer={self.oidc_issuer!r}, "
            f"oidc_audience={self.oidc_audience!r}, "
            f"oidc_jwks_url={self.oidc_jwks_url!r}, "
            f"oidc_role_claim={self.oidc_role_claim!r}, "
            f"default_role={self.default_role!r}, "
            f"allowed_roles={self.allowed_roles!r}, "
            f"replica_urls={tuple(obfuscate_password(u) for u in self.replica_urls)!r}, "
            f"nl2sql_provider={self.nl2sql_provider!r}, "
            f"nl2sql_api_keys={sorted(p for p, _ in self.nl2sql_api_keys)!r}, "
            f"nl2sql_model={self.nl2sql_model!r}, "
            f"nl2sql_base_url={self.nl2sql_base_url!r}, "
            f"nl2sql_max_tokens={self.nl2sql_max_tokens}, "
            f"rate_limit_enabled={self.rate_limit_enabled}, "
            f"rate_limit_max_requests={self.rate_limit_max_requests}, "
            f"rate_limit_window_seconds={self.rate_limit_window_seconds}, "
            f"rate_limit_heavy_max={self.rate_limit_heavy_max}, "
            f"rate_limit_heavy_window={self.rate_limit_heavy_window}, "
            f"allow_insecure_tls={self.allow_insecure_tls}, "
            f"statement_timeout_ms={self.statement_timeout_ms}, "
            f"lock_timeout_ms={self.lock_timeout_ms}, "
            f"slow_call_threshold_ms={self.slow_call_threshold_ms}, "
            f"otel_enabled={self.otel_enabled}, otel_service_name={self.otel_service_name!r}, "
            f"migration_scripts_roots={self.migration_scripts_roots!r}, "
            f"cache_enabled={self.cache_enabled}, "
            f"cache_ttl_seconds={self.cache_ttl_seconds}, "
            f"cache_maxsize={self.cache_maxsize}, "
            f"redis_url={self.redis_url!r}, "
            f"enable_heavy_diagnostics={self.enable_heavy_diagnostics}, "
            f"http_max_body_bytes={self.http_max_body_bytes}, "
            f"http_allowed_origins={self.http_allowed_origins!r}, "
            f"http_hsts_max_age={self.http_hsts_max_age}, "
            f"http_request_timeout_seconds={self.http_request_timeout_seconds}, "
            f"shutdown_drain_seconds={self.shutdown_drain_seconds}, "
            f"audit_hmac_key={audit_hmac_key_repr!r}, "
            f"audit_integrity={self.audit_integrity}, "
            f"secrets_backend={self.secrets_backend!r})"
        )


# sslmode values that don't enforce a TLS-protected connection.
# Per psycopg / libpq docs: `disable` accepts plaintext, `allow` and
# `prefer` try TLS but fall back to plaintext on failure. Only
# `require` / `verify-ca` / `verify-full` actually guarantee TLS.
_INSECURE_SSLMODES = frozenset({"disable", "allow", "prefer"})
# Hosts that don't require TLS — a connection to one of these can't
# leave the box. An *empty* host (i.e. unset in the DSN) is NOT in
# this set: libpq falls back to ``PGHOST`` then to a default that
# may not be loopback, so we refuse to second-guess it and demand
# an explicit ``MCPG_ALLOW_INSECURE_TLS=true`` override instead.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_tls_or_loopback(var: str, dsn: str) -> None:
    """Refuse a non-loopback DSN that doesn't enforce TLS.

    Uses ``psycopg.conninfo.conninfo_to_dict`` (rather than
    ``urllib.parse``) so the check handles every connection-string
    format libpq itself does — key-value strings
    (``host=db sslmode=disable``) and multi-host URIs
    (``postgresql://h1,h2/db``) included.

    Bypassable via ``MCPG_ALLOW_INSECURE_TLS=true``; the caller
    consults that knob before invoking this validator.
    """
    from psycopg.conninfo import conninfo_to_dict

    try:
        params = conninfo_to_dict(dsn)
    except Exception as exc:
        raise ConfigError(f"{var} is not a valid PostgreSQL connection string: {exc}") from None

    # ``host`` may be a comma-separated list of failover candidates;
    # libpq treats each entry independently. We treat the DSN as
    # remote if ANY listed host is non-loopback — partial coverage
    # would be a footgun.
    raw_host = str(params.get("host", "")).strip()
    hosts = [h.strip().lower().strip("[]") for h in raw_host.split(",") if h.strip()]
    if not hosts:
        raise ConfigError(
            f"{var} has no explicit host; libpq will fall back to PGHOST / a default "
            f"that may not be loopback. Set host=localhost / 127.0.0.1 in the DSN, "
            f"or pass MCPG_ALLOW_INSECURE_TLS=true to override."
        )
    remote_hosts = [h for h in hosts if h not in _LOOPBACK_HOSTS]
    if not remote_hosts:
        return
    sslmode = str(params.get("sslmode", "prefer")).strip().lower()
    if sslmode in _INSECURE_SSLMODES:
        raise ConfigError(
            f"{var} points at a remote host ({', '.join(remote_hosts)}) but its sslmode "
            f"is {sslmode!r}; set sslmode=require (or verify-ca / verify-full) in the "
            f"connection string, or pass MCPG_ALLOW_INSECURE_TLS=true to override."
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

    # Secret *values* (API keys, bearer token, audit HMAC key) resolve
    # through the configured secrets backend; non-secret config still
    # comes straight from ``env``. A SecretsError is surfaced as a
    # ConfigError so startup fails with one consistent error type. The
    # builder returns the normalised backend name so we don't re-parse
    # MCPG_SECRETS_BACKEND ourselves.
    try:
        secrets, secrets_backend = build_secrets_provider(env)
    except SecretsError as exc:
        raise ConfigError(str(exc)) from exc

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

    log_format = "text"
    if (raw := env.get("MCPG_LOG_FORMAT")) is not None:
        candidate = raw.strip().lower()
        if candidate not in {"text", "json"}:
            raise ConfigError(f"MCPG_LOG_FORMAT must be one of: text, json (got {raw!r})")
        log_format = candidate

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

    subprocess_bin_allowlist: tuple[str, ...] = ()
    if (raw := env.get("MCPG_SUBPROCESS_BIN_ALLOWLIST")) is not None:
        dirs = tuple(d.strip() for d in raw.split(",") if d.strip())
        for d in dirs:
            if not isabs(d):
                raise ConfigError(f"MCPG_SUBPROCESS_BIN_ALLOWLIST entries must be absolute paths (got {d!r})")
        subprocess_bin_allowlist = dirs

    subprocess_cpu_seconds: int | None = None
    if (raw := env.get("MCPG_SUBPROCESS_CPU_SECONDS")) is not None:
        subprocess_cpu_seconds = _parse_positive_int("MCPG_SUBPROCESS_CPU_SECONDS", raw)

    subprocess_memory_mb: int | None = None
    if (raw := env.get("MCPG_SUBPROCESS_MEMORY_MB")) is not None:
        subprocess_memory_mb = _parse_positive_int("MCPG_SUBPROCESS_MEMORY_MB", raw)

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
    if (raw := secrets.get("MCPG_HTTP_AUTH_TOKEN")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_HTTP_AUTH_TOKEN must not be blank when set")
        http_auth_token = stripped

    auth_mode = "static"
    if (raw := env.get("MCPG_AUTH_MODE")) is not None:
        candidate = raw.strip().lower()
        if candidate not in {"static", "oidc"}:
            raise ConfigError(f"MCPG_AUTH_MODE must be one of: static, oidc (got {raw!r})")
        auth_mode = candidate

    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_role_claim: str | None = None
    if (raw := env.get("MCPG_OIDC_ISSUER")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_OIDC_ISSUER must not be blank when set")
        oidc_issuer = stripped
    if (raw := env.get("MCPG_OIDC_AUDIENCE")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_OIDC_AUDIENCE must not be blank when set")
        oidc_audience = stripped
    if (raw := env.get("MCPG_OIDC_JWKS_URL")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_OIDC_JWKS_URL must not be blank when set")
        oidc_jwks_url = stripped
    if (raw := env.get("MCPG_OIDC_ROLE_CLAIM")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_OIDC_ROLE_CLAIM must not be blank when set")
        oidc_role_claim = stripped

    if auth_mode == "oidc":
        if oidc_issuer is None:
            raise ConfigError("MCPG_AUTH_MODE=oidc requires MCPG_OIDC_ISSUER")
        if oidc_audience is None:
            raise ConfigError("MCPG_AUTH_MODE=oidc requires MCPG_OIDC_AUDIENCE")

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

    replica_urls: tuple[str, ...] = ()
    if (raw := env.get("MCPG_REPLICA_URLS")) is not None:
        urls = tuple(u.strip() for u in raw.split(",") if u.strip())
        if not urls:
            raise ConfigError("MCPG_REPLICA_URLS must not be blank when set")
        replica_urls = urls

    nl2sql_provider: str | None = None
    if (raw := env.get("MCPG_NL2SQL_PROVIDER")) is not None:
        candidate = raw.strip().lower()
        if not candidate:
            raise ConfigError("MCPG_NL2SQL_PROVIDER must not be blank when set")
        if candidate not in {"anthropic", "openai", "gemini"}:
            raise ConfigError(f"MCPG_NL2SQL_PROVIDER must be one of: anthropic, openai, gemini (got {raw!r})")
        nl2sql_provider = candidate

    # Discover every provider whose conventional vendor env var is set.
    # Each becomes callable via ``translate_nl_to_sql(provider=...)``,
    # not just the configured default. Operator can also point one
    # provider at an explicit key via ``MCPG_NL2SQL_API_KEY`` —
    # which always wins over the vendor-conventional fallback.
    api_keys: dict[str, str] = {}
    if anthropic_key := (secrets.get("ANTHROPIC_API_KEY") or "").strip():
        api_keys["anthropic"] = anthropic_key
    if openai_key := (secrets.get("OPENAI_API_KEY") or "").strip():
        api_keys["openai"] = openai_key
    gemini_key = (secrets.get("GEMINI_API_KEY") or "").strip() or (secrets.get("GOOGLE_API_KEY") or "").strip()
    if gemini_key:
        api_keys["gemini"] = gemini_key

    if (raw := secrets.get("MCPG_NL2SQL_API_KEY")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_NL2SQL_API_KEY must not be blank when set")
        if nl2sql_provider is None:
            raise ConfigError(
                "MCPG_NL2SQL_API_KEY is set but MCPG_NL2SQL_PROVIDER is not — "
                "MCPg can't tell which provider this key is for. Either set "
                "MCPG_NL2SQL_PROVIDER (anthropic|openai|gemini) too, or drop "
                "MCPG_NL2SQL_API_KEY and use the vendor-conventional env var "
                "(ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY) so the "
                "provider is implicit."
            )
        # Explicit key targets the configured default provider.
        api_keys[nl2sql_provider] = stripped

    # Auto-pick a default when MCPG_NL2SQL_PROVIDER is unset but at
    # least one vendor key is present. Preference order is documented
    # in README + docs/user-guide.md.
    if nl2sql_provider is None and api_keys:
        for candidate in ("anthropic", "openai", "gemini"):
            if candidate in api_keys:
                nl2sql_provider = candidate
                break

    if nl2sql_provider is not None and nl2sql_provider not in api_keys:
        raise ConfigError(
            f"MCPG_NL2SQL_PROVIDER={nl2sql_provider!r} but no API key found; "
            f"set {VENDOR_ENV_VAR_HINT[nl2sql_provider]} in the environment, "
            "or set MCPG_NL2SQL_API_KEY explicitly."
        )

    # Settings expects a stable, immutable order so the repr is stable
    # too — sort by provider name.
    nl2sql_api_keys = tuple(sorted(api_keys.items()))

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

    rate_limit_enabled = False
    if (raw := env.get("MCPG_RATE_LIMIT_ENABLED")) is not None:
        rate_limit_enabled = _parse_bool("MCPG_RATE_LIMIT_ENABLED", raw)

    rate_limit_max_requests = 60
    if (raw := env.get("MCPG_RATE_LIMIT_MAX_REQUESTS")) is not None:
        rate_limit_max_requests = _parse_positive_int("MCPG_RATE_LIMIT_MAX_REQUESTS", raw)

    rate_limit_window_seconds = 60
    if (raw := env.get("MCPG_RATE_LIMIT_WINDOW_SECONDS")) is not None:
        rate_limit_window_seconds = _parse_positive_int("MCPG_RATE_LIMIT_WINDOW_SECONDS", raw)

    rate_limit_heavy_max = 5
    if (raw := env.get("MCPG_RATE_LIMIT_HEAVY_MAX")) is not None:
        rate_limit_heavy_max = _parse_positive_int("MCPG_RATE_LIMIT_HEAVY_MAX", raw)

    rate_limit_heavy_window = 60
    if (raw := env.get("MCPG_RATE_LIMIT_HEAVY_WINDOW")) is not None:
        rate_limit_heavy_window = _parse_positive_int("MCPG_RATE_LIMIT_HEAVY_WINDOW", raw)

    allow_insecure_tls = False
    if (raw := env.get("MCPG_ALLOW_INSECURE_TLS")) is not None:
        allow_insecure_tls = _parse_bool("MCPG_ALLOW_INSECURE_TLS", raw)

    # PG TLS enforcement: refuse plaintext to remote hosts unless the
    # operator has explicitly opted in. Loopback ("localhost",
    # "127.0.0.1", "::1") is exempt — local sockets / Unix-domain
    # sockets are a different threat model and require their own
    # operator opt-in to be reachable from outside the box anyway.
    if not allow_insecure_tls:
        _require_tls_or_loopback("MCPG_DATABASE_URL", database_url)
        for idx, replica_dsn in enumerate(replica_urls):
            _require_tls_or_loopback(f"MCPG_REPLICA_URLS[{idx}]", replica_dsn)

    statement_timeout_ms = 30000
    if (raw := env.get("MCPG_STATEMENT_TIMEOUT_MS")) is not None:
        try:
            val = int(raw)
            if val < 0:
                raise ValueError()
            statement_timeout_ms = val
        except ValueError:
            raise ConfigError(f"MCPG_STATEMENT_TIMEOUT_MS must be a non-negative integer (got {raw!r})") from None

    lock_timeout_ms = 5000
    if (raw := env.get("MCPG_LOCK_TIMEOUT_MS")) is not None:
        try:
            val = int(raw)
            if val < 0:
                raise ValueError()
            lock_timeout_ms = val
        except ValueError:
            raise ConfigError(f"MCPG_LOCK_TIMEOUT_MS must be a non-negative integer (got {raw!r})") from None

    slow_call_threshold_ms = 1000
    if (raw := env.get("MCPG_SLOW_CALL_THRESHOLD_MS")) is not None:
        try:
            slow_call_threshold_ms = int(raw)
        except ValueError:
            raise ConfigError(f"MCPG_SLOW_CALL_THRESHOLD_MS must be an integer (got {raw!r})") from None

    otel_enabled = False
    if (raw := env.get("MCPG_OTEL_ENABLED")) is not None:
        candidate = raw.strip().lower()
        if candidate not in {"true", "false", "1", "0", "yes", "no"}:
            raise ConfigError(f"MCPG_OTEL_ENABLED must be a boolean (got {raw!r})")
        otel_enabled = candidate in {"true", "1", "yes"}

    otel_service_name = env.get("MCPG_OTEL_SERVICE_NAME", "mcpg").strip() or "mcpg"

    migration_scripts_roots: tuple[str, ...] = ()
    if (raw := env.get("MCPG_MIGRATION_SCRIPTS_ROOTS")) is not None:
        # Colon-separated absolute directories. Validation deeper down
        # (path resolution + existence check) happens at tool call
        # time; here we only verify the syntactic shape so a malformed
        # entry is surfaced at boot rather than the first tool call.
        parts = [piece.strip() for piece in raw.split(":") if piece.strip()]
        for part in parts:
            if not isabs(part):
                raise ConfigError(f"MCPG_MIGRATION_SCRIPTS_ROOTS entries must be absolute paths (got {part!r})")
        migration_scripts_roots = tuple(parts)

    # Re-arm the audit-trail redaction pattern with the operator's
    # MCPG_AUDIT_REDACT_KEYS extension (if any) so the very first audit
    # event the server records honours the configured list.
    from mcpg.audit import configure_log_format, configure_redaction

    configure_redaction(env)
    configure_log_format(log_format)

    cache_enabled = True
    if (raw := env.get("MCPG_CACHE_ENABLED")) is not None:
        cache_enabled = _parse_bool("MCPG_CACHE_ENABLED", raw)

    cache_ttl_seconds = 300
    if (raw := env.get("MCPG_CACHE_TTL_SECONDS")) is not None:
        cache_ttl_seconds = _parse_positive_int("MCPG_CACHE_TTL_SECONDS", raw)

    cache_maxsize = 1024
    if (raw := env.get("MCPG_CACHE_MAXSIZE")) is not None:
        cache_maxsize = _parse_positive_int("MCPG_CACHE_MAXSIZE", raw)

    redis_url = env.get("MCPG_REDIS_URL", "").strip() or None

    enable_heavy_diagnostics = True
    if (raw := env.get("MCPG_ENABLE_HEAVY_DIAGNOSTICS")) is not None:
        enable_heavy_diagnostics = _parse_bool("MCPG_ENABLE_HEAVY_DIAGNOSTICS", raw)

    http_max_body_bytes = 1048576
    if (raw := env.get("MCPG_HTTP_MAX_BODY_BYTES")) is not None:
        http_max_body_bytes = _parse_positive_int("MCPG_HTTP_MAX_BODY_BYTES", raw)

    http_allowed_origins: tuple[str, ...] = ()
    if (raw := env.get("MCPG_HTTP_ALLOWED_ORIGINS")) is not None:
        http_allowed_origins = tuple(o.strip() for o in raw.split(",") if o.strip())

    http_hsts_max_age = 31536000
    if (raw := env.get("MCPG_HTTP_HSTS_MAX_AGE")) is not None:
        try:
            val = int(raw)
            if val < 0:
                raise ValueError()
            http_hsts_max_age = val
        except ValueError:
            raise ConfigError(f"MCPG_HTTP_HSTS_MAX_AGE must be a non-negative integer (got {raw!r})") from None

    http_request_timeout_seconds = 0
    if (raw := env.get("MCPG_HTTP_REQUEST_TIMEOUT_SECONDS")) is not None:
        try:
            val = int(raw)
            if val < 0:
                raise ValueError()
            http_request_timeout_seconds = val
        except ValueError:
            raise ConfigError(
                f"MCPG_HTTP_REQUEST_TIMEOUT_SECONDS must be a non-negative integer (got {raw!r})"
            ) from None

    shutdown_drain_seconds = 30
    if (raw := env.get("MCPG_SHUTDOWN_DRAIN_SECONDS")) is not None:
        shutdown_drain_seconds = _parse_positive_int("MCPG_SHUTDOWN_DRAIN_SECONDS", raw)

    audit_hmac_key: str | None = None
    if (raw := secrets.get("MCPG_AUDIT_HMAC_KEY")) is not None:
        stripped = raw.strip()
        if not stripped:
            raise ConfigError("MCPG_AUDIT_HMAC_KEY must not be blank when set")
        audit_hmac_key = stripped

    audit_integrity = False
    if (raw := env.get("MCPG_AUDIT_INTEGRITY")) is not None:
        audit_integrity = _parse_bool("MCPG_AUDIT_INTEGRITY", raw)

    if audit_integrity and audit_hmac_key is None:
        raise ConfigError("MCPG_AUDIT_INTEGRITY=true requires MCPG_AUDIT_HMAC_KEY")

    return Settings(
        database_url=database_url,
        access_mode=access_mode,
        transport=transport,
        http_host=env.get("MCPG_HTTP_HOST", "127.0.0.1"),
        http_port=http_port,
        log_level=log_level,
        log_format=log_format,
        allow_ddl=allow_ddl,
        allow_shell=allow_shell,
        allow_listen=allow_listen,
        shell_timeout_sec=shell_timeout_sec,
        shell_max_output_bytes=shell_max_output_bytes,
        subprocess_bin_allowlist=subprocess_bin_allowlist,
        subprocess_cpu_seconds=subprocess_cpu_seconds,
        subprocess_memory_mb=subprocess_memory_mb,
        listen_queue_max=listen_queue_max,
        audit_persist=audit_persist,
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
        http_auth_token=http_auth_token,
        auth_mode=auth_mode,
        oidc_issuer=oidc_issuer,
        oidc_audience=oidc_audience,
        oidc_jwks_url=oidc_jwks_url,
        oidc_role_claim=oidc_role_claim,
        default_role=default_role,
        allowed_roles=allowed_roles,
        replica_urls=replica_urls,
        nl2sql_provider=nl2sql_provider,
        nl2sql_api_keys=nl2sql_api_keys,
        nl2sql_model=nl2sql_model,
        nl2sql_base_url=nl2sql_base_url,
        nl2sql_max_tokens=nl2sql_max_tokens,
        rate_limit_enabled=rate_limit_enabled,
        rate_limit_max_requests=rate_limit_max_requests,
        rate_limit_window_seconds=rate_limit_window_seconds,
        rate_limit_heavy_max=rate_limit_heavy_max,
        rate_limit_heavy_window=rate_limit_heavy_window,
        allow_insecure_tls=allow_insecure_tls,
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        slow_call_threshold_ms=slow_call_threshold_ms,
        otel_enabled=otel_enabled,
        otel_service_name=otel_service_name,
        migration_scripts_roots=migration_scripts_roots,
        cache_enabled=cache_enabled,
        cache_ttl_seconds=cache_ttl_seconds,
        cache_maxsize=cache_maxsize,
        redis_url=redis_url,
        enable_heavy_diagnostics=enable_heavy_diagnostics,
        http_max_body_bytes=http_max_body_bytes,
        http_allowed_origins=http_allowed_origins,
        http_hsts_max_age=http_hsts_max_age,
        http_request_timeout_seconds=http_request_timeout_seconds,
        shutdown_drain_seconds=shutdown_drain_seconds,
        audit_hmac_key=audit_hmac_key,
        audit_integrity=audit_integrity,
        secrets_backend=secrets_backend,
    )
