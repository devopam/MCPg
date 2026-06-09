


# MCPg

**A production-grade [Model Context Protocol](https://modelcontextprotocol.io)
server for PostgreSQL.** Lets AI agents safely inspect, query, operate, and
tune a Postgres database — over 100 tools spanning catalog introspection,
query intelligence, natural-language SQL, structural diffs, hybrid search,
graph queries, data movement, live ops, and more.


[![PyPI version](https://img.shields.io/pypi/v/mcpg.svg)](https://pypi.org/project/mcpg/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcpg.svg)](https://pypi.org/project/mcpg/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/devopam/MCPg/blob/main/LICENSE)
[![CI](https://github.com/devopam/MCPg/actions/workflows/ci.yml/badge.svg)](https://github.com/devopam/MCPg/actions/workflows/ci.yml)
[![Stars](https://img.shields.io/github/stars/devopam/MCPg)](https://github.com/devopam/MCPg)

---

| Aspect              | MCPg                              |
|---------------------|-----------------------------------|
| Safety              | Read-only default + AST validation |
| Transport           | stdio + HTTP/SSE                  |
| Install             | `pip install mcpg`                |
| Postgres Versions   | 14–18                             |
| Key Differentiator  | Production observability + multi-tenancy |

## Why MCPg

- **Safe by default.** Read-only access mode. Every user-supplied SQL
  statement parses through a validated AST allowlist before execution.
  Identifier interpolation flows through a strict
  `[A-Za-z_][A-Za-z0-9_]*` regex — a design constraint that means
  user input never reaches the database through string concatenation.
  Capabilities like DDL, shell, and `LISTEN/NOTIFY` are off until you
  opt in.
- **One server, broad surface.** Application data access (queries, search,
  cursors, NL→SQL) *and* DBA-grade operations (health checks, index tuning,
  EXPLAIN analysis, locks, vacuum, dumps, replicas, migrations) in a
  single MCP server. Agents don't have to switch tools to switch tasks.
- **PostgreSQL-native everything.** No ORM, no abstraction tax — uses
  `psycopg3` directly, speaks every `pg_*` system view, integrates with
  TimescaleDB, pgvector, PostGIS, Apache AGE, and `pg_stat_statements`
  where they're available, and degrades gracefully when they aren't.
- **Production-shaped, not demo-shaped.** Connection pooling, per-request
  `SET ROLE` multi-tenancy, read-replica routing with degraded-host
  detection, server-side cursors with dedicated connections,
  rate-limiting, audit trail with regex redaction, PG TLS enforcement
  on startup, OIDC JWT bearer auth, per-session statement / lock
  timeouts.
- **Observability built in.** Prometheus `/metrics` endpoint on the
  HTTP transport surfaces `mcpg_tool_calls_total{tool,status}` +
  `mcpg_tool_duration_seconds`. Every tool call records a structured
  audit event with credential-redacted arguments.
- **Test-driven, multi-version.** 800+ unit tests plus an integration
  suite that runs against a real PostgreSQL container in CI — matrix
  covers PG **14, 15, 16, 17, 18** on every push.

---

### Featured In
- mcp.so  
- mcpservers.org  
- Official MCP Registry (coming soon)

## Install

### From PyPI (recommended)

```bash
pip install mcpg
# or, in an isolated venv exposed globally:
uv tool install mcpg
```

Verify:

```bash
mcpg --version
```

### Docker

```bash
docker build -t mcpg https://github.com/devopam/MCPg.git
docker run --rm -p 8000:8000 \
    -e MCPG_DATABASE_URL=postgresql://user:pass@host:5432/db \
    -e MCPG_ACCESS_MODE=read-only \
    mcpg
```

Multi-stage image: runtime stage drops the build toolchain, runs as
`uid=10001 / gid=10001` with `nologin` shell, application files
root-owned and read-only to the runtime user.

### From source (developers)

```bash
git clone https://github.com/devopam/MCPg && cd MCPg
uv sync
```

`uv sync` creates a venv with all runtime + dev dependencies and exposes
the `mcpg` console script.

More detail in the [Installation Guide](https://github.com/devopam/MCPg/blob/main/docs/installation.md).

---

## Quick start

### Wire MCPg into Claude Desktop (stdio transport)

Drop this into your `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`;
Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb"
      }
    }
  }
}
```

Restart Claude Desktop. The MCPg toolset is now available to the model.
You can ask Claude things like:

> *"What schemas exist in this database? For each one, summarise the
> three biggest tables."*

> *"Why is this query slow?
> `SELECT * FROM orders WHERE customer_id = 42 ORDER BY created_at DESC`"*

### Run as an HTTP server (for IDE integrations, web apps, etc.)

```bash
MCPG_DATABASE_URL=postgresql://user:pass@localhost:5432/mydb \
MCPG_TRANSPORT=streamable-http \
MCPG_HTTP_PORT=8000 \
mcpg
```

Then point any MCP-aware client at `http://localhost:8000`. Set
`MCPG_HTTP_AUTH_TOKEN=...` for a static bearer, or
`MCPG_AUTH_MODE=oidc` for full JWT validation against an OIDC issuer.

---

## Configuration

MCPg is configured **entirely through environment variables** — no
config file, no flags. The only required one is `MCPG_DATABASE_URL`;
everything else has a safe default.

### Common scenarios

| Scenario | Set |
|---|---|
| Local exploration, read-only | `MCPG_DATABASE_URL` |
| Read-write app data access | `MCPG_ACCESS_MODE=restricted` |
| DBA toolkit (DDL, vacuum, etc.) | `MCPG_ACCESS_MODE=unrestricted` + `MCPG_ALLOW_DDL=true` |
| HTTP transport with bearer auth | `MCPG_TRANSPORT=streamable-http` + `MCPG_HTTP_AUTH_TOKEN=…` |
| Multi-tenant SaaS | `MCPG_DEFAULT_ROLE=tenant_a` + `MCPG_ALLOWED_ROLES=tenant_a,tenant_b,…` |
| Read-replica fan-out | `MCPG_REPLICA_URLS=postgresql://…?sslmode=require,postgresql://…?sslmode=require` |
| NL→SQL — single provider | Set `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` / `GEMINI_API_KEY`). MCPg auto-picks the default. |
| NL→SQL — multiple providers, caller picks | Set all vendor keys you want active. Each call to `translate_nl_to_sql` can pass `provider="anthropic"\|"openai"\|"gemini"`. |

### Full reference

#### Core

| Variable | Default | Description |
|---|---|---|
| `MCPG_DATABASE_URL` | **required** | Primary PostgreSQL DSN. Supports URI (`postgresql://…`) and keyword (`host=… user=…`) forms. Remote hosts require `sslmode=require` (or stronger). |
| `MCPG_ACCESS_MODE` | `read-only` | `read-only` \| `restricted` (allows write tools) \| `unrestricted` (also unlocks DBA tools when paired with the gate vars). |
| `MCPG_TRANSPORT` | `stdio` | `stdio` (default, for Claude Desktop) \| `streamable-http` \| `sse`. |
| `MCPG_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`. |
| `MCPG_HTTP_HOST` | `127.0.0.1` | Bind address for HTTP transports. Set to `0.0.0.0` inside containers. |
| `MCPG_HTTP_PORT` | `8000` | Listen port for HTTP transports (1–65535). |

#### Capability gates (opt-in for higher-blast-radius tools)

| Variable | Default | Description |
|---|---|---|
| `MCPG_ALLOW_DDL` | `false` | Expose DDL tools (`run_ddl`, `create_graph`, `drop_graph`, hypertable tools, migration tools). Requires `MCPG_ACCESS_MODE=unrestricted`. |
| `MCPG_ALLOW_SHELL` | `false` | Expose subprocess-backed tools (`dump_database`, `restore_database`, `run_pg_binary`). Required PG client binaries must be on `PATH`. |
| `MCPG_ALLOW_LISTEN` | `false` | Expose `LISTEN/NOTIFY` tools (`subscribe_channel`, `poll_notifications`, `unsubscribe_channel`, `list_notification_subscriptions`). |

#### Authentication (HTTP transports only)

| Variable | Default | Description |
|---|---|---|
| `MCPG_AUTH_MODE` | `static` | `static` (compare bearer to `MCPG_HTTP_AUTH_TOKEN`) \| `oidc` (full JWT validation). |
| `MCPG_HTTP_AUTH_TOKEN` | — | Required bearer token when `MCPG_AUTH_MODE=static`. Constant-time compare. |
| `MCPG_OIDC_ISSUER` | — | OIDC issuer URL (required when `MCPG_AUTH_MODE=oidc`). |
| `MCPG_OIDC_AUDIENCE` | — | Expected `aud` claim (required when `MCPG_AUTH_MODE=oidc`). |
| `MCPG_OIDC_JWKS_URL` | discovered | Override JWKS endpoint (auto-discovered from issuer's `.well-known` otherwise). |
| `MCPG_OIDC_ROLE_CLAIM` | — | JWT claim whose value becomes the per-request PG role (`SET LOCAL ROLE`). Composes with the tenancy driver. |

#### HTTP hardening (HTTP transports only)

| Variable | Default | Description |
|---|---|---|
| `MCPG_HTTP_MAX_BODY_BYTES` | `1048576` | (1 MiB) Request bodies above this get a `413`. Counts streamed bytes, so a missing/lying `Content-Length` can't bypass it. |
| `MCPG_HTTP_ALLOWED_ORIGINS` | — | Comma-separated CORS allowlist. Unset = no CORS middleware (no cross-origin headers emitted). |
| `MCPG_HTTP_HSTS_MAX_AGE` | `31536000` | `Strict-Transport-Security` max-age. `0` disables the HSTS header. Security headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy) are always added unless the app already set them. |
| `MCPG_HTTP_REQUEST_TIMEOUT_SECONDS` | `0` | Per-request wall-clock cap (`504` on expiry). `0` = disabled. Leave off if you rely on long-lived SSE / streamable-http streams — a hard cap also severs those. |

#### Multi-tenancy (`SET ROLE`)

| Variable | Default | Description |
|---|---|---|
| `MCPG_DEFAULT_ROLE` | — | Static PG role applied to every query. Identifier-validated. |
| `MCPG_ALLOWED_ROLES` | — | Comma-separated allowlist. When set, the `X-MCPG-Role` header / OIDC role claim must be in this list. |

#### Read replicas

| Variable | Default | Description |
|---|---|---|
| `MCPG_REPLICA_URLS` | — | Comma-separated replica DSNs. `force_readonly` queries round-robin across healthy replicas; primary fallback on failure; 30 s degraded-replica retry window. |

#### Pool / timeouts / TLS

| Variable | Default | Description |
|---|---|---|
| `MCPG_POOL_MIN_SIZE` | `1` | Minimum pool connections. |
| `MCPG_POOL_MAX_SIZE` | `5` | Maximum pool connections. Must be ≥ `MCPG_POOL_MIN_SIZE`. |
| `MCPG_STATEMENT_TIMEOUT_MS` | `30000` | Per-session `statement_timeout` set on connection checkout. Runaway queries self-terminate. |
| `MCPG_LOCK_TIMEOUT_MS` | `5000` | Per-session `lock_timeout`. Hanging lock waits self-terminate. |
| `MCPG_ALLOW_INSECURE_TLS` | `false` | Bypass the startup TLS check that refuses remote DSNs without `sslmode=require` (or stronger). Loopback hosts are always exempt. |
| `MCPG_SHUTDOWN_DRAIN_SECONDS` | `30` | On SIGTERM, wait up to this long for in-flight tool calls to finish before closing the pool and cursors. |

#### Subprocess tools (`MCPG_ALLOW_SHELL=true` only)

| Variable | Default | Description |
|---|---|---|
| `MCPG_SHELL_TIMEOUT_SEC` | `60` | Max wall-clock for `pg_dump` / `pg_restore` / `psql` invocations. |
| `MCPG_SHELL_MAX_OUTPUT_BYTES` | `67108864` | (64 MiB) Cap on captured stdout per subprocess call. |
| `MCPG_SUBPROCESS_BIN_ALLOWLIST` | — | Comma-separated absolute dirs the resolved `pg_dump` / `pg_restore` / `psql` must live under. Empty = trust `PATH`. Defeats a PATH-shim of these binaries. |
| `MCPG_SUBPROCESS_CPU_SECONDS` | — | Per-child `RLIMIT_CPU` (seconds). POSIX only; unset = inherit. |
| `MCPG_SUBPROCESS_MEMORY_MB` | — | Per-child `RLIMIT_AS` (MiB). POSIX only; unset = inherit. |

#### LISTEN/NOTIFY (`MCPG_ALLOW_LISTEN=true` only)

| Variable | Default | Description |
|---|---|---|
| `MCPG_LISTEN_QUEUE_MAX` | `1000` | Per-channel buffer; oldest notifications dropped on overflow. |

#### Audit

| Variable | Default | Description |
|---|---|---|
| `MCPG_AUDIT_PERSIST` | `false` | When true, every `run_write` / `run_ddl` call persists to a `mcpg_audit.events` table (auto-created idempotently). |
| `MCPG_AUDIT_REDACT_KEYS` | — | Comma-separated regex fragments added to the secret-name pattern (defaults already cover `password`, `passwd`, `secret`, `token`, `api[_-]?key`, `bearer`, `authorization`, `database_url`, `dsn`, `conninfo`). |
| `MCPG_AUDIT_INTEGRITY` | `false` | When true, each persisted event is signed with an HMAC chained over the previous event; the `verify_audit_chain` tool walks the chain and reports the first break. Requires `MCPG_AUDIT_HMAC_KEY`. |
| `MCPG_AUDIT_HMAC_KEY` | — | Secret key for the audit HMAC chain. Required when `MCPG_AUDIT_INTEGRITY=true`. Never appears in `repr`/logs. |

#### Secrets backend

By default every secret is read straight from the environment. Set
`MCPG_SECRETS_BACKEND=file` to instead load API keys / bearer token /
HMAC key from a mounted file — a name in the file wins; anything absent
falls back to the env var, so partial files work.

| Variable | Default | Description |
|---|---|---|
| `MCPG_SECRETS_BACKEND` | `env` | `env` (read every secret from the environment) \| `file` (overlay a secrets file on top of the environment). |
| `MCPG_SECRETS_FILE_PATH` | — | Required when `MCPG_SECRETS_BACKEND=file`. Path to a flat `name → value` map: JSON always, or YAML (`.yaml`/`.yml`) when PyYAML is installed. Covers `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `MCPG_NL2SQL_API_KEY`, `MCPG_HTTP_AUTH_TOKEN`, and `MCPG_AUDIT_HMAC_KEY`. |

#### Rate limiting

| Variable | Default | Description |
|---|---|---|
| `MCPG_RATE_LIMIT_ENABLED` | `false` | Enable token-bucket per-tool rate limiting. |
| `MCPG_RATE_LIMIT_MAX_REQUESTS` | `60` | Global cap per window across all tools. |
| `MCPG_RATE_LIMIT_WINDOW_SECONDS` | `60` | Window length for the global quota. |
| `MCPG_RATE_LIMIT_HEAVY_MAX` | `5` | Cap for heavy tools (`run_write`, `run_ddl`, `dump_database`, etc.). |
| `MCPG_RATE_LIMIT_HEAVY_WINDOW` | `60` | Window length for the heavy-tool quota. |

#### Caching & Feature flags

| Variable | Default | Description |
|---|---|---|
| `MCPG_CACHE_ENABLED` | `true` | Enable or disable the adaptive cache layer. |
| `MCPG_CACHE_TTL_SECONDS` | `300` | Default cache Time-To-Live in seconds. |
| `MCPG_CACHE_MAXSIZE` | `1024` | Maximum LRU capacity bound for the memory cache. |
| `MCPG_REDIS_URL` | — | Optional Redis backend connection string for external, multi-node caching. |
| `MCPG_ENABLE_HEAVY_DIAGNOSTICS` | `true` | Toggle computationally heavy diagnostic, diagram, and advisor tools. |

#### Natural-language SQL

MCPg auto-discovers every configured provider from the environment at
startup. Set as many of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`GEMINI_API_KEY` (or `GOOGLE_API_KEY`) as you have — each becomes
callable. When `MCPG_NL2SQL_PROVIDER` is unset, MCPg picks the default
in preference order **anthropic → openai → gemini**. The
`translate_nl_to_sql` tool accepts an optional `provider="…"` argument
so a caller can route between providers per call; `get_server_info`
reports which are available.

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Vendor-conventional key for Anthropic / Claude. |
| `OPENAI_API_KEY` | — | Vendor-conventional key for OpenAI. |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | — | Vendor-conventional key for Google / Gemini. |
| `MCPG_NL2SQL_PROVIDER` | auto-picked | `anthropic` \| `openai` \| `gemini`. Pins the default provider used when the tool is called without `provider=`. When unset and any vendor key is in the env, MCPg auto-picks anthropic → openai → gemini. |
| `MCPG_NL2SQL_API_KEY` | — | Explicit key for the configured `MCPG_NL2SQL_PROVIDER`. Overrides the vendor-conventional env var for that provider only. Requires `MCPG_NL2SQL_PROVIDER` to be set. |
| `MCPG_NL2SQL_MODEL` | provider default | Override the default model (e.g. `claude-sonnet-4-6`, `gpt-4o-mini`, `gemini-2.5-flash`). Applies only to the default provider. |
| `MCPG_NL2SQL_BASE_URL` | — | OpenAI-compatible endpoint override (Ollama, vLLM, OpenRouter). Applies only when the default provider is `openai`. |
| `MCPG_NL2SQL_MAX_TOKENS` | `2048` | Cap on generated tokens (hard limit: 16384). |

---

## Usage examples

The MCP tools are invoked by the agent (Claude, Cursor, Continue, …)
in response to your natural-language instructions. A handful of
illustrative round-trips:

### Inspect the schema

> **You:** What tables live in the `public` schema, and which ones are
> the biggest by row count?

> **Agent (using `list_tables` + `summarize_table` × N):**
> Six tables: `customers` (1.2M rows), `orders` (4.7M), `line_items`
> (18.3M), `products` (340), `addresses` (1.4M), `audit_log`
> (45.8M — the largest, accumulates without retention).

### Diagnose a slow query

> **You:** Why does
> `SELECT * FROM orders o JOIN line_items l ON o.id = l.order_id WHERE o.created_at > now() - interval '7 days'`
> take 8 seconds?

> **Agent (using `why_is_this_slow` + `recommend_indexes`):**
> `EXPLAIN ANALYZE` shows a sequential scan over `orders` (4.7M rows)
> filtered by `created_at`. There's no index on `orders.created_at`.
> Recommended:
> `CREATE INDEX CONCURRENTLY orders_created_at_idx ON orders (created_at DESC);`
> Expected improvement: ~95% reduction (index scan touches ~33K rows
> instead of full table). Run `validate_migration` on it before applying
> to confirm there's no locking surprise.

### Translate plain English to SQL

> **You:** Find customers who placed an order this month but never
> in the previous three months — show me their email and current
> lifetime spend.

> **Agent (using `translate_nl_to_sql`):**
> ```sql
> SELECT c.email, SUM(l.price * l.quantity) AS lifetime_spend
> FROM customers c
> JOIN orders o ON o.customer_id = c.id
> JOIN line_items l ON l.order_id = o.id
> WHERE EXISTS (
>   SELECT 1 FROM orders o2 WHERE o2.customer_id = c.id
>     AND o2.created_at >= date_trunc('month', now()))
>   AND NOT EXISTS (
>   SELECT 1 FROM orders o3 WHERE o3.customer_id = c.id
>     AND o3.created_at >= date_trunc('month', now()) - interval '3 months'
>     AND o3.created_at <  date_trunc('month', now()))
> GROUP BY c.email;
> ```

### Picture the schema

> **You:** Draw me an ER diagram of the `public` schema.

> **Agent (using `generate_schema_diagram`):** *Returns a Mermaid
> diagram you can paste straight into GitHub / Notion / Obsidian.*

### Audit the database

> **You:** How healthy is this database right now?

> **Agent (using `audit_database`):** Returns a graded report:
> *Memory & I/O score 92 (GOOD), Transaction & Connection 78 (WARNING:
> rollback rate 0.4%, look at app logs), Concurrency & Locks 60
> (CRITICAL: 14 backends waiting), Cleanliness & Bloat 88 (GOOD), Slow
> queries 70 (WARNING: top query template runs 5000×, mean 90 ms —
> see `optimize_query`).*

### Run a guarded write

> **You:** Soft-delete every order older than 5 years.

> **Agent (using `run_write` with `MCPG_AUDIT_PERSIST=true`):** Validates
> the statement through the safe-SQL kernel, runs it inside a transaction,
> returns affected row count, persists the call (sql + arguments —
> with secrets regex-redacted — + status) to `mcpg_audit.events` for
> after-the-fact review.

For dozens more recipes — multi-tenant routing, RLS testing, NL→SQL,
hybrid vector + FTS search, Apache AGE Cypher, TimescaleDB, ORM schema
exports, server-side cursors — see [`docs/cookbook.md`](https://github.com/devopam/MCPg/blob/main/docs/cookbook.md).

---

## What's in the box

Compact category list. For the full, current tool reference see
[`docs/tools.md`](https://github.com/devopam/MCPg/blob/main/docs/tools.md); for a guided walkthrough see
[`docs/tour.md`](https://github.com/devopam/MCPg/blob/main/docs/tour.md).

- **Catalog introspection** — schemas, tables, columns, indexes,
  constraints, views, functions, triggers, sequences, partitions,
  policies, roles, grants, enums, domains, composite types, FDWs,
  publications, subscriptions, extensions, generated columns.
- **Query intelligence** — `run_select`, `run_select_parallel`,
  `explain_query`, `analyze_query_plan`, `why_is_this_slow`,
  `recommend_indexes`, `analyze_workload`, `check_database_health`,
  `detect_n_plus_one`, `audit_database`.
- **Search** — `fuzzy_search` (trigram), `full_text_search`,
  `vector_search`, `hybrid_search` (pgvector + FTS via RRF),
  `geo_search` (PostGIS k-NN).
- **Natural language → SQL** — `translate_nl_to_sql` (Anthropic,
  OpenAI, or Gemini; output passes through the same safe-SQL kernel
  as hand-written queries).
- **Visualisation** — `generate_schema_diagram` (ER),
  `generate_fk_cascade_graph` (blast-radius of `ON DELETE CASCADE`),
  `generate_graph_diagram` (Apache AGE property graphs).
- **Structural diff & migrations** — `compare_schemas`,
  `validate_migration`, staged `prepare_migration` /
  `complete_migration` / `cancel_migration` workflow.
- **Apache AGE graph + Cypher** — `list_graphs`, `describe_graph`,
  `run_cypher`, `create_graph`, `drop_graph`, `generate_graph_diagram`.
- **Composite + advisor tools** — `summarize_table`,
  `find_unused_objects`, `find_sensitive_columns` (PII heuristic),
  `lint_naming_conventions`, `test_rls_for_role`, `list_locks`,
  `find_blocking_chains`, `read_pg_stat_io` (PG16+),
  `generate_test_data`.
- **Live ops & maintenance** — `list_active_queries`,
  `verify_connection_encryption` (TLS status of the live link),
  `run_maintenance` (VACUUM/ANALYZE), `prune_audit_events`
  (audit retention), `cancel_query`, `terminate_backend`,
  `run_write`, `run_ddl`, `enable_extension`.
- **Data movement** — `export_query` / `export_table` (CSV/JSON),
  `dump_database` / `restore_database`, `import_csv` / `import_json`
  (COPY FROM STDIN), `copy_table_between_databases`.
- **Server-side cursors** — `open_cursor`, `fetch_cursor`,
  `close_cursor`, `list_cursors` for pageable reads over millions
  of rows.
- **TimescaleDB** — `list_hypertables`, `list_chunks`,
  `create_hypertable`, `add_compression_policy`,
  `add_retention_policy`.
- **ORM schema exporters** — Prisma, Drizzle, SQLAlchemy, sqlc,
  Diesel, jOOQ, Ent, Ecto.
- **Event streams** — `subscribe_channel`, `poll_notifications`,
  `unsubscribe_channel`, `list_notification_subscriptions` bridging
  PostgreSQL `LISTEN/NOTIFY` into the MCP poll model.
- **Observability** — Prometheus `/metrics` endpoint +
  `get_metrics_exposition` tool for stdio; structured audit trail
  with regex-based credential redaction.

---

## Documentation

- [`docs/installation.md`](https://github.com/devopam/MCPg/blob/main/docs/installation.md) — install + configure
- [`docs/tour.md`](https://github.com/devopam/MCPg/blob/main/docs/tour.md) — guided tool tour
- [`docs/cookbook.md`](https://github.com/devopam/MCPg/blob/main/docs/cookbook.md) — practical agent recipes
- [`docs/tools.md`](https://github.com/devopam/MCPg/blob/main/docs/tools.md) — complete tool reference
- [`docs/architecture.md`](https://github.com/devopam/MCPg/blob/main/docs/architecture.md) — how the pieces fit together
- [`docs/scaling.md`](https://github.com/devopam/MCPg/blob/main/docs/scaling.md) — pool sizing, replicas, performance
- [`docs/security-hardening.md`](https://github.com/devopam/MCPg/blob/main/docs/security-hardening.md) — security feature roadmap
- [`docs/release-process.md`](https://github.com/devopam/MCPg/blob/main/docs/release-process.md) — how releases ship to PyPI
- [`docs/adr/`](https://github.com/devopam/MCPg/tree/main/docs/adr) — architecture decision records
- Browse at **https://devopam.github.io/MCPg/**

---

## Security

- Vulnerability reporting: see [`SECURITY.md`](https://github.com/devopam/MCPg/blob/main/SECURITY.md). 90-day
  coordinated-disclosure window; reports to `devopam@gmail.com`.
- Defence-in-depth: capability gates, SafeSQL kernel, identifier
  allowlist, audit redaction, PG TLS enforcement at startup,
  rate-limiting, OIDC JWT validation, per-session timeouts.
- See [`docs/security-hardening.md`](https://github.com/devopam/MCPg/blob/main/docs/security-hardening.md) for
  the living roadmap of shipped (✅) and queued (⬜) hardening items.

---

## Release notes & changelog

See [`CHANGELOG.md`](https://github.com/devopam/MCPg/blob/main/CHANGELOG.md) for the full version history,
[`docs/release-process.md`](https://github.com/devopam/MCPg/blob/main/docs/release-process.md) for how releases
are cut, and the [GitHub Releases](https://github.com/devopam/MCPg/releases)
page for downloadable artifacts.

---

## Contributing

Pull requests welcome — see [`CONTRIBUTING.md`](https://github.com/devopam/MCPg/blob/main/CONTRIBUTING.md) for
the dev-loop setup, test conventions, and the per-PR review
checklist.

---

## License

MIT — see [`LICENSE`](https://github.com/devopam/MCPg/blob/main/LICENSE). The vendored SQL-safety kernel at
`src/mcpg/_vendor/sql/` is also MIT-licensed; see [`NOTICE`](https://github.com/devopam/MCPg/blob/main/NOTICE)
for provenance.

> **Disclaimer.** Best efforts have been made to bring MCPg to
> production grade, but it remains an actively developed project and
> may contain issues. See the License terms for indemnity details.

<!-- mcp-name: io.github.devopam/mcpg -->
