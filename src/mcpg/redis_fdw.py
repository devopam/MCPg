"""``redis_fdw`` coverage — catalog filters, DDL helpers, cache stats, advisor.

``redis_fdw`` is a foreign-data wrapper that exposes a Redis instance as
SQL-queryable foreign tables. Operators reach for it as a drop-in cache
front for expensive Postgres queries, as a landing zone for ephemeral
session / rate-limit / pub-sub state queried alongside relational data,
and as a migration aid when moving between Redis and Postgres.

This module groups everything redis_fdw-shaped behind one import surface
so the tools layer stays uniform with the other FDW + cache-and-foreign-data
buckets:

* Read tools — filter ``pg_foreign_server`` / ``pg_foreign_table`` to the
  redis_fdw subset, describe a single cache foreign table, and best-effort
  cache-stats reporting.
* DDL tools — wrap ``CREATE SERVER`` / ``CREATE USER MAPPING`` /
  ``CREATE FOREIGN TABLE`` with parameterised identifier validation and
  secrets-backend credential plumbing.
* Advisor — analyse ``pg_stat_user_tables`` + ``pg_stat_statements`` for
  read-heavy, low-write tables that would benefit from Redis-fronted
  caching, and emit ready-to-run ``CREATE FOREIGN TABLE`` stubs.

Security posture
----------------
``redis_fdw`` runs in-process inside Postgres. Every DDL tool here flows
through the regular allowlist + RBAC / unrestricted gates. The user-mapping
helper never accepts a raw password as a tool argument — it takes a
secret reference and resolves it through ``MCPG_SECRETS_BACKEND``. TLS to
non-loopback Redis hosts is required unless ``allow_insecure_tls=True`` is
passed explicitly.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed
from mcpg.introspection import _parse_options
from mcpg.secrets import SecretsProvider

# redis_fdw identifies itself in pg_foreign_data_wrapper as "redis_fdw" —
# we use that as the discriminator on every catalog filter below.
_REDIS_FDW_NAME = "redis_fdw"

# Redis-side key structures the FDW recognises. The strings match the
# ``tabletype`` option the FDW expects on ``CREATE FOREIGN TABLE``.
_VALID_KEY_TYPES = frozenset({"hash", "list", "string", "set", "zset"})

# Loopback families: TLS may be disabled here without the explicit
# ``allow_insecure_tls`` flag. Everything else must pass through TLS.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

# Identifier validator — Postgres unquoted identifier shape. We use this
# pattern (vs psycopg's Identifier escaping) because foreign-server /
# user-mapping DDL is built up out of identifiers we trust *after this
# check*; rejecting hostile inputs at the boundary is simpler than
# escape-correctness everywhere downstream.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RedisFdwError(Exception):
    """Raised when a redis_fdw operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RedisForeignServer:
    """A ``CREATE SERVER ... FOREIGN DATA WRAPPER redis_fdw`` row.

    ``address`` is the host the FDW connects to (taken from the
    ``address`` option). ``port``, ``database`` and ``password_configured``
    are extracted from the server options dict so callers don't have to
    re-parse it. The full ``options`` dict is preserved for completeness.
    """

    name: str
    address: str | None
    port: int | None
    database: int | None
    tls: bool
    password_configured: bool
    options: dict[str, str]


@dataclass(frozen=True, slots=True)
class RedisCacheTableInfo:
    """The shape of one redis-backed foreign table.

    ``key_type`` is the Redis-side structure (``hash`` / ``list`` /
    ``string`` / ``set`` / ``zset``). ``ttl_seconds`` is the configured
    expiration if the FDW exposes it; ``None`` when keys live until
    explicit eviction.
    """

    schema: str
    name: str
    server: str
    key_type: str | None
    key_prefix: str | None
    ttl_seconds: int | None
    columns: list[dict[str, str]]
    options: dict[str, str]


@dataclass(frozen=True, slots=True)
class RedisCacheStats:
    """Best-effort metrics for a redis_fdw server.

    redis_fdw versions vary in what they expose. When the FDW or
    auxiliary functions are missing we still want a deterministic
    response — ``available=False`` with everything else zeroed.
    """

    server: str
    available: bool
    key_count: int | None
    used_memory_bytes: int | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class CreateRedisServerResult:
    name: str
    address: str
    port: int
    database: int
    tls: bool
    created: bool


@dataclass(frozen=True, slots=True)
class CreateRedisUserMappingResult:
    server: str
    user: str
    secret_ref: str
    created: bool


@dataclass(frozen=True, slots=True)
class CreateRedisCacheTableResult:
    schema: str
    name: str
    server: str
    key_type: str
    columns: tuple[str, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class RedisCacheRecommendation:
    """One advisor recommendation row.

    ``reason`` is a stable identifier the agent can react to: e.g.
    ``read_heavy_low_write``, ``small_hot_relation``, ``frequent_pk_lookup``.
    ``ready_to_run_sql`` is the foreign-table stub the operator can paste.
    """

    schema: str
    table: str
    reads: int
    writes: int
    read_write_ratio: float
    estimated_row_count: int
    reason: str
    ready_to_run_sql: str


@dataclass(frozen=True, slots=True)
class RecommendRedisCacheTargetsResult:
    server: str | None
    candidates: list[RedisCacheRecommendation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _parse_int(options: Mapping[str, str], key: str) -> int | None:
    raw = options.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"true", "t", "yes", "y", "on", "1"}


async def list_redis_foreign_servers(driver: SqlDriver) -> list[RedisForeignServer]:
    """List the ``CREATE SERVER`` rows backed by ``redis_fdw``.

    Joins ``pg_foreign_server`` to ``pg_foreign_data_wrapper`` and keeps
    only the rows where ``fdwname = 'redis_fdw'``. Returns an empty list
    when ``redis_fdw`` is not installed.
    """
    if not await extension_installed(driver, _REDIS_FDW_NAME):
        return []
    rows = await driver.execute_query(
        "SELECT s.srvname AS name, s.srvoptions AS options "
        "FROM pg_foreign_server s "
        "JOIN pg_foreign_data_wrapper fdw ON fdw.oid = s.srvfdw "
        "WHERE fdw.fdwname = %s "
        "ORDER BY s.srvname",
        params=[_REDIS_FDW_NAME],
        force_readonly=True,
    )
    results: list[RedisForeignServer] = []
    for row in rows or []:
        options = _parse_options(row.cells["options"])
        results.append(
            RedisForeignServer(
                name=row.cells["name"],
                address=options.get("address") or options.get("host"),
                port=_parse_int(options, "port"),
                database=_parse_int(options, "database"),
                tls=_is_truthy(options.get("tls")),
                # We can never read the password back from the catalog —
                # ``password_configured`` is True iff a user mapping is
                # present for this server. The dedicated lookup keeps
                # ``list_redis_foreign_servers`` to a single round-trip.
                password_configured=False,
                options=options,
            )
        )
    if not results:
        return results
    # Second probe — flag servers that have any user mapping configured.
    server_names = [r.name for r in results]
    mapping_rows = await driver.execute_query(
        "SELECT DISTINCT srvname FROM pg_user_mappings WHERE srvname = ANY(%s)",
        params=[server_names],
        force_readonly=True,
    )
    configured = {row.cells["srvname"] for row in mapping_rows or []}
    return [
        RedisForeignServer(
            name=r.name,
            address=r.address,
            port=r.port,
            database=r.database,
            tls=r.tls,
            password_configured=r.name in configured,
            options=r.options,
        )
        for r in results
    ]


async def describe_redis_cache_table(driver: SqlDriver, schema: str, table: str) -> RedisCacheTableInfo:
    """Describe one redis-backed foreign table.

    Raises ``RedisFdwError`` when the table doesn't exist or isn't backed
    by ``redis_fdw``.
    """
    rows = await driver.execute_query(
        "SELECT c.relname AS name, s.srvname AS server, ft.ftoptions AS options "
        "FROM pg_foreign_table ft "
        "JOIN pg_class c ON c.oid = ft.ftrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_foreign_server s ON s.oid = ft.ftserver "
        "JOIN pg_foreign_data_wrapper fdw ON fdw.oid = s.srvfdw "
        "WHERE n.nspname = %s AND c.relname = %s AND fdw.fdwname = %s",
        params=[schema, table, _REDIS_FDW_NAME],
        force_readonly=True,
    )
    if not rows:
        raise RedisFdwError(f"no redis_fdw foreign table {schema}.{table}")
    row = rows[0]
    options = _parse_options(row.cells["options"])
    col_rows = await driver.execute_query(
        "SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS data_type "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        params=[schema, table],
        force_readonly=True,
    )
    columns = [{"name": r.cells["name"], "data_type": r.cells["data_type"]} for r in col_rows or []]
    return RedisCacheTableInfo(
        schema=schema,
        name=row.cells["name"],
        server=row.cells["server"],
        key_type=options.get("tabletype"),
        key_prefix=options.get("keyprefix") or options.get("key_prefix"),
        ttl_seconds=_parse_int(options, "ttl") or _parse_int(options, "expiry"),
        columns=columns,
        options=options,
    )


async def get_redis_cache_stats(driver: SqlDriver, server: str) -> RedisCacheStats:
    """Best-effort metrics for one redis_fdw server.

    redis_fdw doesn't ship a uniform stats SQL function across versions.
    We probe ``pg_foreign_server`` to confirm the server exists, then
    return ``available=False`` with a descriptive ``detail`` when no
    stats surface is available. This is deliberately a soft failure —
    callers can layer their own Redis-side probes on top.
    """
    rows = await driver.execute_query(
        "SELECT s.srvname FROM pg_foreign_server s "
        "JOIN pg_foreign_data_wrapper fdw ON fdw.oid = s.srvfdw "
        "WHERE s.srvname = %s AND fdw.fdwname = %s",
        params=[server, _REDIS_FDW_NAME],
        force_readonly=True,
    )
    if not rows:
        raise RedisFdwError(f"no redis_fdw foreign server {server!r}")
    # No stable cross-version SQL surface — report unavailable rather
    # than fabricating zeros.
    return RedisCacheStats(
        server=server,
        available=False,
        key_count=None,
        used_memory_bytes=None,
        detail="redis_fdw does not expose uniform stats SQL — query Redis directly (INFO / DBSIZE) for live metrics",
    )


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------


def _validate_identifier(label: str, value: str) -> str:
    """Reject everything outside the ``[A-Za-z_][A-Za-z0-9_]*`` shape.

    ``CREATE SERVER`` / ``CREATE USER MAPPING`` / ``CREATE FOREIGN TABLE``
    DDL is built up of identifiers we can't bind. The allowlist is the
    injection guard.
    """
    if not _IDENT_RE.match(value):
        raise RedisFdwError(f"{label} {value!r} is not a valid unquoted SQL identifier")
    return value


def _validate_address(address: str) -> str:
    """Reject obvious shell metacharacters; permit IP addresses + hostnames."""
    cleaned = address.strip()
    if not cleaned:
        raise RedisFdwError("address must be a non-empty hostname or IP")
    if any(ch in cleaned for ch in "'\";\\\n\r"):
        raise RedisFdwError("address contains characters that are not allowed in a server option")
    return cleaned


def _is_loopback(address: str) -> bool:
    if address in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


async def create_redis_cache_server(
    driver: SqlDriver,
    *,
    name: str,
    address: str,
    port: int = 6379,
    database: int = 0,
    tls: bool = True,
    allow_insecure_tls: bool = False,
) -> CreateRedisServerResult:
    """Run ``CREATE SERVER name FOREIGN DATA WRAPPER redis_fdw OPTIONS (...)``.

    The default posture is TLS-on. Refusing ``tls=False`` against a
    non-loopback host without ``allow_insecure_tls=True`` mirrors the
    HTTP-runtime TLS-enforcement story already used elsewhere in MCPg.
    Idempotent via ``IF NOT EXISTS``.
    """
    _validate_identifier("server name", name)
    address = _validate_address(address)
    if port <= 0 or port > 65535:
        raise RedisFdwError("port must be in 1..65535")
    if database < 0:
        raise RedisFdwError("database must be a non-negative integer")
    if not tls and not _is_loopback(address) and not allow_insecure_tls:
        raise RedisFdwError(
            f"refusing tls=False against non-loopback Redis host {address!r}; pass allow_insecure_tls=True to override"
        )
    if not await extension_installed(driver, _REDIS_FDW_NAME):
        raise RedisFdwError("redis_fdw extension is not installed; call enable_extension('redis_fdw') first")
    options = f"address '{address}', port '{port}', database '{database}', tls '{'true' if tls else 'false'}'"
    await driver.execute_query(
        f'CREATE SERVER IF NOT EXISTS "{name}" FOREIGN DATA WRAPPER redis_fdw OPTIONS ({options})',
        force_readonly=False,
    )
    return CreateRedisServerResult(
        name=name,
        address=address,
        port=port,
        database=database,
        tls=tls,
        created=True,
    )


async def create_redis_user_mapping(
    driver: SqlDriver,
    *,
    server: str,
    user: str,
    secret_ref: str,
    secrets: SecretsProvider,
) -> CreateRedisUserMappingResult:
    """Run ``CREATE USER MAPPING FOR user SERVER server OPTIONS (password '…')``.

    The Redis password is **never** accepted as an argument — instead the
    caller passes ``secret_ref``, the name of an entry in the configured
    secrets backend (``MCPG_SECRETS_BACKEND``). We resolve it here and
    interpolate the value into the OPTIONS list.

    The password value is escaped by doubling single quotes inside the
    SQL literal. The secret reference itself is logged-as-name only — the
    raw value never round-trips through any tool argument boundary.
    """
    _validate_identifier("server name", server)
    if user.lower() == "public":
        user_clause = "PUBLIC"
    else:
        _validate_identifier("user name", user)
        user_clause = f'"{user}"'
    if not secret_ref or not secret_ref.strip():
        raise RedisFdwError("secret_ref must reference a name in the configured secrets backend")
    password = secrets.get(secret_ref)
    if password is None or not password:
        raise RedisFdwError(f"secret_ref {secret_ref!r} did not resolve to a value via MCPG_SECRETS_BACKEND")
    if not await extension_installed(driver, _REDIS_FDW_NAME):
        raise RedisFdwError("redis_fdw extension is not installed; call enable_extension('redis_fdw') first")
    # Escape ' by doubling it; reject characters that would break out
    # of the OPTIONS list. Defense in depth — secrets-backend values
    # are not adversarial by default, but we still guard the SQL
    # boundary.
    if any(ch in password for ch in ("\n", "\r", "\x00")):
        raise RedisFdwError("resolved Redis password contains forbidden control characters")
    escaped = password.replace("'", "''")
    await driver.execute_query(
        f"CREATE USER MAPPING IF NOT EXISTS FOR {user_clause} SERVER \"{server}\" OPTIONS (password '{escaped}')",
        force_readonly=False,
    )
    return CreateRedisUserMappingResult(
        server=server,
        user=user,
        secret_ref=secret_ref,
        created=True,
    )


def _validate_column_decls(columns: list[dict[str, str]]) -> list[tuple[str, str]]:
    """Reject column declarations that aren't simple ``name type`` pairs."""
    out: list[tuple[str, str]] = []
    for col in columns:
        name = col.get("name", "")
        col_type = col.get("type", "")
        _validate_identifier("column name", name)
        if not col_type or any(ch in col_type for ch in "'\";\\\n\r"):
            raise RedisFdwError(
                f"column {name!r} type {col_type!r} is not a simple Postgres type — provide a bare type name"
            )
        out.append((name, col_type.strip()))
    if not out:
        raise RedisFdwError("at least one column is required")
    return out


async def create_redis_cache_table(
    driver: SqlDriver,
    *,
    schema: str,
    name: str,
    server: str,
    key_type: str,
    columns: list[dict[str, str]],
    key_prefix: str | None = None,
    ttl_seconds: int | None = None,
) -> CreateRedisCacheTableResult:
    """Run ``CREATE FOREIGN TABLE schema.name (...) SERVER server OPTIONS (tabletype 'hash', …)``.

    ``key_type`` must be one of ``hash`` / ``list`` / ``string`` / ``set``
    / ``zset``. ``columns`` is a list of ``{name, type}`` dicts. Optional
    ``key_prefix`` and ``ttl_seconds`` are passed through as redis_fdw
    options.
    """
    _validate_identifier("schema", schema)
    _validate_identifier("table name", name)
    _validate_identifier("server name", server)
    if key_type not in _VALID_KEY_TYPES:
        raise RedisFdwError(f"key_type must be one of {sorted(_VALID_KEY_TYPES)}, got {key_type!r}")
    decls = _validate_column_decls(columns)
    if ttl_seconds is not None and ttl_seconds < 0:
        raise RedisFdwError("ttl_seconds must be a non-negative integer")
    if not await extension_installed(driver, _REDIS_FDW_NAME):
        raise RedisFdwError("redis_fdw extension is not installed; call enable_extension('redis_fdw') first")
    columns_sql = ", ".join(f'"{n}" {t}' for n, t in decls)
    options_parts = [f"tabletype '{key_type}'"]
    if key_prefix is not None:
        if any(ch in key_prefix for ch in "'\";\\\n\r"):
            raise RedisFdwError("key_prefix contains characters that are not allowed in a foreign-table option")
        options_parts.append(f"keyprefix '{key_prefix}'")
    if ttl_seconds is not None:
        options_parts.append(f"ttl '{ttl_seconds}'")
    options_sql = ", ".join(options_parts)
    await driver.execute_query(
        f'CREATE FOREIGN TABLE IF NOT EXISTS "{schema}"."{name}" ({columns_sql}) '
        f'SERVER "{server}" OPTIONS ({options_sql})',
        force_readonly=False,
    )
    return CreateRedisCacheTableResult(
        schema=schema,
        name=name,
        server=server,
        key_type=key_type,
        columns=tuple(n for n, _ in decls),
        created=True,
    )


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------


# Thresholds — exposed as module constants so the docs/plans page can
# reference them by name and the tests can flex one without flipping
# others.
_DEFAULT_MIN_READ_WRITE_RATIO = 10.0
_DEFAULT_MIN_READS_PER_DAY = 1_000
_DEFAULT_MAX_ROWS = 1_000_000


async def recommend_redis_cache_targets(
    driver: SqlDriver,
    *,
    server: str | None = None,
    min_read_write_ratio: float = _DEFAULT_MIN_READ_WRITE_RATIO,
    min_reads_per_day: int = _DEFAULT_MIN_READS_PER_DAY,
    max_rows: int = _DEFAULT_MAX_ROWS,
    limit: int = 20,
) -> RecommendRedisCacheTargetsResult:
    """Recommend tables that would benefit from a Redis cache layer.

    The heuristic favours tables where:

    1. Read traffic dominates write traffic — ``seq_scan + idx_scan``
       relative to ``n_tup_ins + n_tup_upd + n_tup_del`` must exceed
       ``min_read_write_ratio``.
    2. Absolute read volume is meaningful — ``min_reads_per_day`` filters
       out cold tables that just happen to have zero writes.
    3. The working set fits comfortably in Redis — ``max_rows`` caps
       recommendations at relations whose ``pg_class.reltuples`` is
       below the threshold.

    When ``server`` is provided the generated ``ready_to_run_sql`` stub
    targets that server name; otherwise the stub uses a placeholder
    operators must substitute.

    The advisor is read-only. It never touches Redis itself.
    """
    if min_read_write_ratio <= 0:
        raise RedisFdwError("min_read_write_ratio must be positive")
    if min_reads_per_day < 0:
        raise RedisFdwError("min_reads_per_day must be non-negative")
    if max_rows <= 0:
        raise RedisFdwError("max_rows must be positive")
    if limit <= 0:
        raise RedisFdwError("limit must be positive")

    rows = await driver.execute_query(
        "SELECT s.schemaname AS schema, s.relname AS table_name, "
        "  COALESCE(s.seq_scan, 0) + COALESCE(s.idx_scan, 0) AS reads, "
        "  COALESCE(s.n_tup_ins, 0) + COALESCE(s.n_tup_upd, 0) + COALESCE(s.n_tup_del, 0) AS writes, "
        "  c.reltuples::bigint AS est_rows "
        "FROM pg_stat_user_tables s "
        "JOIN pg_class c ON c.oid = s.relid "
        "WHERE c.relkind IN ('r', 'p')",
        force_readonly=True,
    )
    candidates: list[RedisCacheRecommendation] = []
    target_server = server or "<configure-redis-server>"
    for row in rows or []:
        reads = int(row.cells["reads"])
        writes = int(row.cells["writes"])
        est_rows = max(int(row.cells["est_rows"]), 0)
        if reads < min_reads_per_day:
            continue
        if est_rows > max_rows:
            continue
        # Avoid div-by-zero by treating "no writes" as a very large ratio.
        ratio = float("inf") if writes == 0 else reads / max(writes, 1)
        if ratio < min_read_write_ratio:
            continue
        reason = _classify_recommendation(reads, writes, est_rows)
        schema = row.cells["schema"]
        table = row.cells["table_name"]
        stub = (
            f"-- redis_fdw cache stub for {schema}.{table}\n"
            f'CREATE FOREIGN TABLE IF NOT EXISTS "{schema}"."{table}_cache" (\n'
            f"    key text,\n"
            f"    value text\n"
            f') SERVER "{target_server}" OPTIONS (\n'
            f"    tabletype 'hash',\n"
            f"    keyprefix '{schema}:{table}:'\n"
            f");"
        )
        candidates.append(
            RedisCacheRecommendation(
                schema=schema,
                table=table,
                reads=reads,
                writes=writes,
                read_write_ratio=ratio if ratio != float("inf") else float(reads),
                estimated_row_count=est_rows,
                reason=reason,
                ready_to_run_sql=stub,
            )
        )
    candidates.sort(key=lambda c: (-c.reads, c.estimated_row_count))
    return RecommendRedisCacheTargetsResult(server=server, candidates=candidates[:limit])


def _classify_recommendation(reads: int, writes: int, est_rows: int) -> str:
    if writes == 0:
        return "read_only_lookup_table"
    if est_rows <= 10_000:
        return "small_hot_relation"
    if reads >= writes * 100:
        return "read_heavy_low_write"
    return "moderate_read_dominant"


__all__ = [
    "CreateRedisCacheTableResult",
    "CreateRedisServerResult",
    "CreateRedisUserMappingResult",
    "RecommendRedisCacheTargetsResult",
    "RedisCacheRecommendation",
    "RedisCacheStats",
    "RedisCacheTableInfo",
    "RedisFdwError",
    "RedisForeignServer",
    "create_redis_cache_server",
    "create_redis_cache_table",
    "create_redis_user_mapping",
    "describe_redis_cache_table",
    "get_redis_cache_stats",
    "list_redis_foreign_servers",
    "recommend_redis_cache_targets",
]
