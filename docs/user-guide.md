# MCPg User Guide

How to use MCPg once it's installed. For getting it installed see
[`installation.md`](installation.md); for the full per-tool
parameters see [`tools.md`](tools.md); for short task-shaped
recipes see [`cookbook.md`](cookbook.md).

This guide is the narrative walkthrough — read it top-to-bottom the
first time, skim it as a reference after.

---

## Contents

1. [What MCPg is](#what-mcpg-is)
2. [Access modes & capability gates](#access-modes--capability-gates)
3. [Connecting an MCP client](#connecting-an-mcp-client)
4. [Working with the tools](#working-with-the-tools)
5. [Multi-tenancy](#multi-tenancy)
6. [Read-replica routing](#read-replica-routing)
7. [Natural-language SQL](#natural-language-sql)
8. [Server-side cursors](#server-side-cursors)
9. [Data movement](#data-movement)
10. [Reactive workflows: LISTEN / NOTIFY](#reactive-workflows-listen--notify)
11. [Staged migrations](#staged-migrations)
12. [ORM bridges](#orm-bridges)
13. [Audit trail](#audit-trail)
14. [Observability](#observability)
15. [Rate limiting](#rate-limiting)
16. [Security defaults](#security-defaults)
17. [Troubleshooting](#troubleshooting)

---

## What MCPg is

MCPg is an MCP server that exposes a PostgreSQL database to an AI
agent through a fixed, audited set of **122 tools**. The agent never
gets a raw database connection — it can only call the tools MCPg
registers, every call is validated, and every call is logged. MCPg
runs as a single async process and ships as both a PyPI package
(`pip install mcpg`) and a hardened Docker image.

---

## Access modes & capability gates

The `MCPG_ACCESS_MODE` setting decides the **default** tool surface;
additional **capability gates** unlock higher-blast-radius families
within `unrestricted` mode.

| Mode | What's exposed |
|---|---|
| `read-only` (default) | Catalog introspection, querying, search, health, EXPLAIN — all read-only. |
| `restricted` | Same as `read-only`. Reserved for future tighter execution limits. |
| `unrestricted` | The above **plus** writes: `run_write`, `run_maintenance`, `cancel_query`, `terminate_backend`. |

Within `unrestricted`, the gate vars decide which additional
families come along:

| Gate | Unlocks |
|---|---|
| `MCPG_ALLOW_DDL=true` | `run_ddl`, `enable_extension`, hypertable tools (`create_hypertable`, `add_compression_policy`, `add_retention_policy`), AGE graph DDL (`create_graph`, `drop_graph`), staged migrations (`prepare_migration` / `complete_migration` / `cancel_migration` / `validate_migration` / `list_pending_migrations`). |
| `MCPG_ALLOW_SHELL=true` | Subprocess tools — `dump_database`, `restore_database`, `copy_table_between_databases`. Requires the PostgreSQL client binaries (`pg_dump` / `pg_restore` / `psql`) on `PATH`. |
| `MCPG_ALLOW_LISTEN=true` | `subscribe_channel`, `poll_notifications`, `unsubscribe_channel`, `list_notification_subscriptions`. |

Read-only is the default because the typical agent workflow is
**inspect first, modify second**. Each gate is a deliberate opt-in
so an experimentation deployment can't drop a table by accident.

---

## Connecting an MCP client

### stdio (Claude Desktop and other local clients)

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb",
        "MCPG_ACCESS_MODE": "read-only"
      }
    }
  }
}
```

(Use `"command": "mcpg"` with no `args` if you've done a
`pip install mcpg` on the system Python.)

### Streamable HTTP (Cursor, Continue, custom web clients, etc.)

```bash
export MCPG_DATABASE_URL=postgresql://user:pass@localhost:5432/mydb
export MCPG_TRANSPORT=streamable-http
export MCPG_HTTP_AUTH_TOKEN=<random_long_token>
mcpg
```

Then point the client at `http://<host>:8000/mcp` and have it send
`Authorization: Bearer <random_long_token>`.

For production, prefer OIDC over a static token — set:

```bash
export MCPG_AUTH_MODE=oidc
export MCPG_OIDC_ISSUER=https://your-issuer/
export MCPG_OIDC_AUDIENCE=mcpg
# Optional — map a JWT claim to the per-request PG role:
export MCPG_OIDC_ROLE_CLAIM=pg_role
```

OIDC validates every request's JWT against the issuer's JWKS
(asymmetric algorithms only — RS256/RS384/RS512 + ES256/ES384/ES512).

---

## Working with the tools

The tools group into a few common workflows. Full discovery surface
is in [`tour.md`](tour.md); long-form parameters in
[`tools.md`](tools.md); task-shaped recipes in
[`cookbook.md`](cookbook.md).

### Explore a schema

`list_schemas` → `list_tables` → `describe_table` maps a database.
`list_indexes` shows a table's indexes and their access method
(B-tree / GIN / HNSW / IVFFlat / GiST / BRIN). `list_extensions`
and `list_available_extensions` show what extensions are installed
or available; `describe_table` reports the dimension of a
`pgvector` `vector(N)` column.

For a one-call snapshot use `summarize_table(schema, table)` —
columns + PK + FKs + indexes + stats + sample rows, all in one
round-trip. Excellent agent UX.

### Query data

`run_select(sql, max_rows=1000)` runs a read-only SQL query —
validated against the SafeSQL allowlist, executed under a forced
read-only transaction, and capped with a `truncated` flag.
`run_select_parallel(statements)` fans out concurrently with
per-statement error isolation.

`explain_query(sql)` returns a query plan; `analyze_query_plan(sql)`
summarises it (cost, node types, sequential scans);
`why_is_this_slow(sql)` rolls EXPLAIN + active queries + locks +
cache + index suggestions into one call.

### Search

`fuzzy_search` (trigram, needs `pg_trgm`), `full_text_search`
(tsvector / tsquery, web-search syntax), `vector_search` (pgvector
k-NN, choose `<->` / `<#>` / `<=>` operator), `vector_range_search`
(distance threshold), `hybrid_search` (vector + FTS via
reciprocal-rank fusion), `geo_search` (PostGIS k-NN over a
geography column).

For vector index sizing: `recommend_vector_index` (HNSW vs IVFFlat)
and `recommend_vector_quantization` (vector → halfvec / bit
storage advisor).

### Diagnose and tune

- `check_database_health` — connections, cache hit ratio, dead
  tuples, invalid indexes, replication lag, bloat.
- `audit_database(schema)` — comprehensive 5-category DBA report
  (Memory & I/O, Transactions & Connections, Concurrency & Locks,
  Cleanliness & Bloat, Slow Queries) with per-metric scores, top
  issues, and prescriptive recommendations.
- `analyze_workload` — slowest query templates from
  `pg_stat_statements`.
- `recommend_indexes` — missing-index heuristics.
- `find_unused_objects(schema)` — zero-scan tables and user
  indexes; great for "what can I drop?".
- `detect_n_plus_one(min_calls=100)` — walks
  `pg_stat_statements` looking for ORM lazy-load loops.

### Change data (`unrestricted` mode)

- `run_write(sql)` — one `INSERT` / `UPDATE` / `DELETE`. Add a
  `RETURNING` clause to get affected rows back.
- `run_ddl(sql)` (`MCPG_ALLOW_DDL=true`) — one DDL statement; can
  optionally snapshot the structural diff for an `audit_database`
  follow-up.
- `enable_extension(name)` — allowlisted extensions only.

### Visualise

- `generate_schema_diagram(schema)` — Mermaid ER, paste-ready into
  GitHub / Notion / Obsidian.
- `generate_fk_cascade_graph(schema)` — Mermaid blast-radius graph
  of `ON DELETE CASCADE` / `SET NULL` / `SET DEFAULT` FKs.
- `generate_graph_diagram(graph_name)` — Mermaid view of an Apache
  AGE property graph.

---

## Multi-tenancy

MCPg supports **per-request PostgreSQL role switching** via
`SET LOCAL ROLE` so one process can safely serve many tenants from
a single pool.

```bash
# Static default role applied to every query that doesn't override:
export MCPG_DEFAULT_ROLE=tenant_a

# Allowlist — when set, the X-MCPG-Role header / OIDC claim must
# be in the list (otherwise 403):
export MCPG_ALLOWED_ROLES=tenant_a,tenant_b,tenant_c
```

HTTP requests override the default by sending
`X-MCPG-Role: tenant_b`. OIDC deployments override via
`MCPG_OIDC_ROLE_CLAIM` — the named JWT claim's value becomes the
per-request role automatically.

Role names are identifier-validated
(`[A-Za-z_][A-Za-z0-9_]*`) so they're safe to inline into
`SET LOCAL ROLE "<name>"`.

RLS policies keyed on `current_user` then isolate tenants
correctly, and the audit trail records which role ran each call.

---

## Read-replica routing

```bash
export MCPG_REPLICA_URLS=postgresql://u:p@replica-1/db?sslmode=require,postgresql://u:p@replica-2/db?sslmode=require
```

When `MCPG_REPLICA_URLS` is non-empty, every `force_readonly=true`
query is round-robin-routed to a healthy replica; writes always go
to the primary. Replica failures fall back to the primary once and
mark the replica degraded for 30 s before re-trying.

Each replica has its own connection pool; per-request `SET LOCAL
ROLE` composes across replicas (each replica's pool gets its own
`TenantSqlDriver`).

`list_replicas()` reports per-replica index, password-obfuscated
DSN, degraded flag, last error, and seconds-until-retry. Routing
decisions land in the Prometheus `mcpg_tool_calls_total` counter
under the synthetic tool name `__replica_route` with statuses
`primary` / `primary_no_healthy` / `fallback` / `replica_<n>`.

---

## Natural-language SQL

`translate_nl_to_sql(question, schema, provider=None, execute=false)`
asks an LLM provider to produce read-only SQL for the supplied
natural-language question, given the named schema's catalog. The
generated SQL is passed through the **same SafeSQL allowlist** as
any hand-written query before any optional execution.

### Provider configuration

MCPg auto-discovers every configured provider from the environment at
startup. Set as many of these as you want active — each becomes
callable through the tool:

| Provider | Set in env | Default model |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | provider's recent Claude Sonnet |
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| `gemini` | `GEMINI_API_KEY` (falls back to `GOOGLE_API_KEY`) | provider's recent Gemini Flash |

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # configures anthropic
export OPENAI_API_KEY=sk-...             # configures openai too
# Both available; MCPg auto-picks anthropic as the default
# (preference order: anthropic → openai → gemini).
```

To pin a specific default, set `MCPG_NL2SQL_PROVIDER` explicitly:

```bash
export MCPG_NL2SQL_PROVIDER=openai       # default is now openai
```

`MCPG_NL2SQL_API_KEY` (when set) supplies the key for the configured
default provider; it requires `MCPG_NL2SQL_PROVIDER` to also be set so
MCPg knows which provider it's for.

### Per-call routing

Multiple providers configured? Any caller can route per-call by
passing the optional `provider=` argument:

```
translate_nl_to_sql(question, schema, provider="anthropic")
translate_nl_to_sql(question, schema, provider="openai")
translate_nl_to_sql(question, schema, provider="gemini")
translate_nl_to_sql(question, schema)              # uses default
```

This is the recommended shape for **one MCPg server, many MCP
clients** — set every vendor key on the host, run one MCPg over the
HTTP transport, let each agent / IDE pick its preferred LLM per call.
`get_server_info()` reports `nl2sql_default_provider` and
`nl2sql_available_providers` so a caller can introspect.

### Model + endpoint overrides

Override the default provider's model with `MCPG_NL2SQL_MODEL`, or
point at an OpenAI-compatible self-hosted endpoint (Ollama, vLLM,
OpenRouter) with `MCPG_NL2SQL_BASE_URL`. Both apply **only** when the
call targets the default provider — an explicit `provider="openai"`
call when the default is `anthropic` falls back to OpenAI's default
model + endpoint, since the operator's overrides are
provider-specific.
The per-call response budget is capped by `MCPG_NL2SQL_MAX_TOKENS`
(default 2048, hard limit 16384).

---

## Server-side cursors

For pageable reads over millions of rows.

```
open_cursor(sql) → { cursor_id: "mcpg_e3a91f", … }
fetch_cursor(cursor_id, batch_size=100) → { rows: [...], exhausted: false }
fetch_cursor(cursor_id, batch_size=100) → { rows: [...], exhausted: true }   # stop polling
close_cursor(cursor_id)                                                       # idempotent
list_cursors()
```

Each cursor holds a **dedicated connection** so long-lived cursors
don't starve the main pool. Cursors auto-close after a 5-minute
idle timeout. The opening SQL passes through the same SafeSQL
allowlist as `run_select`, so cursors can only read.

---

## Data movement

Five tools, three blast-radius tiers:

- **In-process exports** (no opt-in needed):
  `export_query(sql, format="csv|json")`,
  `export_table(schema, table, format="csv|json", limit=10000)`.
- **In-process imports** (`unrestricted`): bulk-load via
  `COPY … FROM STDIN` + parametrised `executemany`.
  `import_csv(schema, table, content, header=true, delimiter=",")`,
  `import_json(schema, table, content)`.
- **Subprocess dump / restore**
  (`unrestricted` + `MCPG_ALLOW_SHELL=true`): `dump_database`,
  `restore_database`, `copy_table_between_databases` shell out to
  `pg_dump` / `psql` / `pg_restore` with hard timeouts
  (`MCPG_SHELL_TIMEOUT_SEC`, default 60 s) and output caps
  (`MCPG_SHELL_MAX_OUTPUT_BYTES`, default 64 MiB).

---

## Reactive workflows: LISTEN / NOTIFY

`subscribe_channel(channel)` opens a PG `LISTEN` on a dedicated
connection and returns a `subscription_id`. `poll_notifications(id,
timeout_ms=0, max_messages=100)` drains the per-sub bounded queue,
optionally waiting up to `timeout_ms`. `unsubscribe_channel(id)`
removes the subscription; `list_notification_subscriptions()`
reports the active ones.

Queue size cap: `MCPG_LISTEN_QUEUE_MAX` (default 1000); overflow
drops the oldest message and surfaces `dropped_count` on the next
poll. Requires `MCPG_ACCESS_MODE=unrestricted` +
`MCPG_ALLOW_LISTEN=true`.

---

## Staged migrations

`prepare_migration(name, target_schema, candidate_sql,
ttl_minutes=60)` clones the target schema's structure into a shadow,
applies the candidate SQL there, and runs `compare_schemas` so you
can review the structural diff. `validate_migration` separately
applies the candidate to a transient shadow seeded with sampled
real data so failures the diff misses (NOT NULL on existing NULLs,
CHECK violations, type narrowings) surface before apply.

`complete_migration(id)` applies to the target;
`cancel_migration(id)` drops the shadow without applying.
`list_pending_migrations()` shows what's staged.

All staged-migration tools require `unrestricted` +
`MCPG_ALLOW_DDL=true`.

---

## ORM bridges

Eight read-only exporters generate a starting schema/model file
from the live PG catalog:

- `generate_prisma_schema` (TypeScript / Prisma)
- `generate_drizzle_schema` (TypeScript / Drizzle)
- `generate_sqlalchemy_models` (Python / SQLAlchemy 2.0)
- `generate_sqlc_schema` (Go / sqlc)
- `generate_diesel_schema` (Rust / Diesel)
- `generate_jooq_config` (Java / jOOQ codegen XML)
- `generate_ent_schemas` (Go / Ent — one file per table)
- `generate_ecto_schemas` (Elixir / Ecto — one file per table)

All eight cover: base tables, columns, primary keys, single-column
intra-schema FKs, and enums. Cross-schema and composite FKs are
documented gaps.

---

## Audit trail

Every tool call — success or failure — is logged to the
`mcpg.audit` Python logger with the tool name, **redacted**
arguments (see below), and outcome. Configure where that logger's
records are shipped via your deployment's logging stack.

### Persistence

With `MCPG_AUDIT_PERSIST=true`, every `run_write` and `run_ddl` is
**also** persisted to a `mcpg_audit.events` table (auto-created
idempotently on first write) with redacted arguments + result +
status. Query the table via the `list_audit_events` tool.

### Redaction

Argument values are masked when their **key name** matches a
case-insensitive regex (matched via `re.search`, so `password`
also covers `PGPASSWORD`, `user_password`, `app.password`).
Default patterns:

```
password, passwd, secret, token, api[_-]?key, bearer,
authorization, database_url, dsn, conninfo
```

Extend the pattern list via `MCPG_AUDIT_REDACT_KEYS` (comma-separated
regex fragments). Walks nested dicts / lists / tuples so credentials
buried in result payloads are masked too.

String leaves are passed through the `obfuscate_password` helper so
an embedded connection-string credential nested anywhere is
scrubbed.

### Integrity

To prevent tampering (unauthorized alterations, insertions, or deletions) of your persisted audit events, MCPg supports a signature chain:

*   **`MCPG_AUDIT_INTEGRITY=true`** — Enables the HMAC-SHA256 signature chain.
*   **`MCPG_AUDIT_HMAC_KEY=<key>`** — The secret key used to compute and verify the signature chain (required when integrity is enabled).

When enabled, each event carries a signature computed over the deterministic payload and the preceding event's signature. Verify the entire log using the **`verify_audit_chain`** tool, which sequentially checks each link and reports any tampering or deletions.

---

## Observability

The HTTP transports expose:

- `GET /metrics` — Prometheus text-format snapshot. Two series:
  - `mcpg_tool_calls_total{tool,status}` — counter; `status` ∈
    `ok` / `error` / `primary` / `fallback` / `replica_<n>` /
    `primary_no_healthy` (the last four come from the synthetic
    `__replica_route` tool).
  - `mcpg_tool_duration_seconds_*` — histogram per tool.
- `GET /healthz` — liveness probe.
- `GET /readyz` — readiness probe (verifies a pool connection).

For stdio deployments, the `get_metrics_exposition()` MCP tool
returns the same Prometheus payload as a string the agent can hand
to a scrape target.

---

## Rate limiting

```bash
export MCPG_RATE_LIMIT_ENABLED=true
export MCPG_RATE_LIMIT_MAX_REQUESTS=60       # global cap per window
export MCPG_RATE_LIMIT_WINDOW_SECONDS=60     # window length
export MCPG_RATE_LIMIT_HEAVY_MAX=5           # cap for heavy tools
export MCPG_RATE_LIMIT_HEAVY_WINDOW=60       # heavy-window length
```

Token-bucket per-tool rate limiting. "Heavy" tools include
`run_write`, `run_ddl`, `dump_database`, `restore_database`, and
similarly expensive operations. A rate-limited call returns an
error immediately rather than queueing.

---

## Caching

```bash
export MCPG_CACHE_ENABLED=true             # enable or disable caching (default: true)
export MCPG_CACHE_TTL_SECONDS=300          # default cache TTL in seconds (default: 300)
export MCPG_CACHE_MAXSIZE=1024             # maximum LRU cache entry capacity (default: 1024)
export MCPG_REDIS_URL=redis://localhost:6379/0  # optional Redis connection string for external caching
```

MCPg provides a high-performance caching layer to save context window tokens and prevent database connection pool saturation from duplicate schema reads:
* **Adaptive Caching**: Wraps all schema introspection, diagram generators, property graphs, and DBA performance advisors.
* **Automatic Invalidation**: Any write or DDL statement executed on the database automatically clears the entire cache to prevent serving stale metadata.
* **Optional Redis Backend**: Soft-dependency Redis async support. If `redis` is configured but the library is not installed, the server logs a warning and falls back to a thread-safe, memory-bounded in-memory LRU cache rather than crashing.

---

## Feature Flags

```bash
export MCPG_ENABLE_HEAVY_DIAGNOSTICS=true   # toggle computationally heavy diagnostics (default: true)
```

Enables operational gating for administrators over expensive diagnostic tools. When set to `false`, diagnostic, diagramming, and advisor tools (`run_advisors`, `recommend_indexes`, `generate_schema_diagram`, etc.) remain registered for client discovery but raise a friendly, administrator-disabled `RuntimeError` at call-time.

---

## Security defaults

MCPg ships with defence-in-depth defaults:

- **Read-only access mode by default.** Writes and DDL need
  explicit opt-in.
- **SafeSQL allowlist.** Every agent-supplied SQL statement parses
  through the vendored `SafeSqlDriver` (built on `pglast`) — only
  whitelisted AST nodes pass through. Statement stacking, comment
  escapes, transaction-control escapes, DDL inside `run_select`,
  `COPY`, and `DO` blocks are refused **before execution**.
- **Identifier allowlist.** Every interpolated identifier (schema
  / table / column / role names) goes through a
  `[A-Za-z_][A-Za-z0-9_]*` regex.
- **PG TLS enforcement on startup.** MCPg refuses to start if
  `MCPG_DATABASE_URL` (or any `MCPG_REPLICA_URLS` entry) points
  at a non-loopback host without `sslmode=require` (or stronger).
  Use `MCPG_ALLOW_INSECURE_TLS=true` to opt out for non-prod.
- **Per-session timeouts** (`MCPG_STATEMENT_TIMEOUT_MS`, default
  30 s; `MCPG_LOCK_TIMEOUT_MS`, default 5 s) self-terminate
  runaway queries and lock waits.
- **HTTP authn.** Static bearer (`MCPG_HTTP_AUTH_TOKEN`) with
  constant-time compare, or OIDC JWT validation against the
  issuer's JWKS (asymmetric algorithms only).
- **Audit redaction** as documented above.
- **Graceful shutdown draining** (controlled by `MCPG_SHUTDOWN_DRAIN_SECONDS`, default 30 s) ensures the server lifespan exit drains all in-flight tool calls before releasing database connection pools.
- **Multi-tenancy via `SET ROLE`.** One process can serve many
  tenants without the RLS-meets-pool footgun.

See [`security.md`](security.md) for the full threat model and
[`security-hardening.md`](security-hardening.md) for the living
roadmap of shipped (✅) and queued (⬜) hardening items.

---

## Troubleshooting

- **`mcpg: configuration error: …`** — A required `MCPG_*` env var
  is missing or invalid; the message names it. See the
  [README env-var reference](../README.md#configuration).
- **`MCPG_DATABASE_URL points at a remote host (…) but its sslmode
  is 'prefer'`** — The TLS enforcement guard caught a
  plaintext-capable DSN. Add `?sslmode=require` to the DSN or
  set `MCPG_ALLOW_INSECURE_TLS=true` if intentional.
- **A write tool is missing.** Set `MCPG_ACCESS_MODE=unrestricted`
  plus the matching gate (`MCPG_ALLOW_DDL=true`,
  `MCPG_ALLOW_SHELL=true`, or `MCPG_ALLOW_LISTEN=true`).
- **`fuzzy_search` / `analyze_workload` / `vector_search` reports
  `available: false`.** The corresponding extension isn't
  installed in the target DB. MCPg degrades gracefully — install
  when ready.
- **A query is rejected.** `run_select` only permits safe read-only
  statements; writes, DDL, and multi-statement input are refused
  by design. Use `run_write` / `run_ddl` (under `unrestricted`
  mode) instead.
- **`prepare_migration` refuses with "cannot run inside a
  transaction"** — the candidate SQL contains a
  `CONCURRENTLY` / `VACUUM` / `ALTER SYSTEM` statement. The
  staged-migration workflow always wraps the candidate in a
  transaction; for those, use `run_ddl` directly.
- **Rate-limit errors.** With `MCPG_RATE_LIMIT_ENABLED=true`,
  exceeding the per-tool quota returns
  "Rate limit exceeded for tool '<name>'". Wait the window out
  or raise `MCPG_RATE_LIMIT_*` defaults.
- **Replica request fell back to primary.** Check `list_replicas()`
  — the affected replica will be `degraded=true` with
  `last_error` and `seconds_until_retry`. The 30 s window
  auto-clears once the replica is healthy.
- **JWT was rejected.** OIDC validates against `iss` + `aud` +
  `exp` + `nbf` + signature, with 30 s clock leeway. Confirm
  `MCPG_OIDC_ISSUER` + `MCPG_OIDC_AUDIENCE` match what your IdP
  is minting; check that the JWT algorithm is asymmetric (HS-family
  is rejected by design).
- **`audit_database` returns `pg_stat_statements: Not Installed`**.
  Add `pg_stat_statements` to `shared_preload_libraries`,
  restart Postgres, then `CREATE EXTENSION pg_stat_statements;`.
