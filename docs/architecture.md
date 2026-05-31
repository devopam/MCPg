# MCPg Architecture

How MCPg is built. Describes the current shape; the running record
of how it got there lives in [`../CHANGELOG.md`](../CHANGELOG.md)
and the [`adr/`](adr/) directory.

---

## Overview

MCPg is a single-process, async ([`asyncio`](https://docs.python.org/3/library/asyncio.html))
MCP server. An MCP client connects via stdio or HTTP, calls tools,
and gets typed results. Every call passes through three layers:

```
   MCP client ──stdio / streamable-HTTP / SSE──▶ AuditedFastMCP
                                                       │  (rate limit + audit + metrics)
                                                       ▼
                                             tool wrapper (mcpg.tools)
                                                       │  (capability gate via mcpg.policy)
                                                       ▼
                                    logic module (query / health / search / …)
                                                       │
                                            SqlDriver / SafeSqlDriver / TenantSqlDriver / RoutedSqlDriver
                                                       │  psycopg3 pool + (optional) replica pools
                                                       ▼
                                                 PostgreSQL
```

Each layer has a focused responsibility — the tool wrapper
translates the MCP call into a typed Python call, the logic module
builds and runs the SQL, the driver stack validates / forces
read-only / picks a pool / sets the tenant role.

---

## Request lifecycle

1. The client invokes a tool. `AuditedFastMCP.call_tool` (a
   `FastMCP[AppContext]` subclass) wraps every invocation:
   - Checks the rate limiter (`mcpg.middleware.rate_limit`) when
     `MCPG_RATE_LIMIT_ENABLED=true`.
   - Records an audit event on completion (success or failure)
     with the tool name, redacted arguments, and outcome.
   - Updates the Prometheus counter + histogram
     (`mcpg_tool_calls_total{tool,status}` /
     `mcpg_tool_duration_seconds`).
2. The tool wrapper in `mcpg.tools` pulls the request's
   `AppContext` (settings + database + listen manager + cursor
   manager) from the server lifespan and obtains a `SqlDriver`.
3. The wrapper delegates to a **logic module** that builds and
   runs the SQL and maps rows to typed dataclasses.
4. The driver stack decides exactly which pool the SQL hits:
   - **SafeSqlDriver** — agent-supplied SQL is parsed and
     allowlisted via the vendored kernel before execution.
   - **RoutedSqlDriver** — when `MCPG_REPLICA_URLS` is set,
     `force_readonly=True` queries round-robin across healthy
     replicas; writes always go to the primary.
   - **TenantSqlDriver** — wraps a base driver to issue
     `BEGIN ... SET LOCAL ROLE "<role>" ... <stmt> ... COMMIT`
     when a static or per-request role is in play.
5. The result is mapped to a typed result class and returned
   through the tool wrapper. The audit hook records the outcome.

---

## Module map

| Module | Responsibility |
|---|---|
| `mcpg.config` | Env-driven, validated `Settings` (frozen dataclass). Validates TLS-required, role identifiers, OIDC settings; redacts secrets in `__repr__`. |
| `mcpg.database` | Primary-connection-pool lifecycle (`Database`) and per-connection `statement_timeout` / `lock_timeout` setup. |
| `mcpg.replicas` | Read-replica pool registry; degraded-replica tracking; routing logic. |
| `mcpg.context` | `AppContext` — the per-server state shared with every tool wrapper. |
| `mcpg.server` | `FastMCP` bootstrap, `AuditedFastMCP` subclass, transport selection, `run` entry point. |
| `mcpg.http_runtime` | Streamable-HTTP / SSE transport bring-up: bearer auth, OIDC validation, security middleware, `/metrics` / `/healthz` / `/readyz` endpoints. |
| `mcpg.oidc` | OIDC discovery + JWKS-backed JWT verification (asymmetric algorithms only). |
| `mcpg.tenancy` | The `current_role` ContextVar that powers per-request `SET LOCAL ROLE`. |
| `mcpg.middleware.rate_limit` | Token-bucket per-tool rate limiter. |
| `mcpg.policy` | Access-mode → capability permission table. |
| `mcpg.audit` | Tool-call audit logger + configurable secret-name regex redactor + the comprehensive `audit_database` DBA report. |
| `mcpg.audit_trail` | Optional `mcpg_audit.events` table persistence for `run_write` / `run_ddl` records. |
| `mcpg.tools` | Thin MCP tool wrappers + `register_tools` (consults `mcpg.policy` for capability gating). |
| `mcpg.introspection` | Schema / catalog inspection queries (parameterised). |
| `mcpg.query` | Safe read-only query execution + plan analysis. |
| `mcpg.write` | Single-statement DML / DDL execution + optional schema-diff capture. |
| `mcpg.health` | Database health checks. |
| `mcpg.workload` | Slow-query analysis (`pg_stat_statements`). |
| `mcpg.indexing` | Index recommendations. |
| `mcpg.textsearch` | Search: trigram fuzzy, full-text, pgvector, PostGIS k-NN, hybrid (vector + FTS via RRF). |
| `mcpg.vector_tuning` | pgvector advisors (HNSW vs IVFFlat, quantization, recall/speed). |
| `mcpg.composite` | One-call aggregates: `summarize_table`, `why_is_this_slow`, `audit_database`. |
| `mcpg.advisors` | Schema-quality advisors (PK, FK index, dup index, RLS, graph index, …). |
| `mcpg.cursors` | Server-side cursor manager (one dedicated connection per cursor; 5-minute idle TTL). |
| `mcpg.cypher` / `mcpg.graph` / `mcpg.graph_diagram` / `mcpg.graph_mgmt` | Apache AGE property graph + Cypher integration. |
| `mcpg.migrations` | Staged migration workflow — shadow schema, structural diff, transient validation. |
| `mcpg.nl2sql` | Natural-language → SQL via pluggable providers (Anthropic / OpenAI / Gemini). |
| `mcpg.data_movement` | Export (`export_query` / `export_table`) and bulk-load (`import_csv` / `import_json` via COPY FROM STDIN). |
| `mcpg.shell` | Subprocess wrappers (`dump_database` / `restore_database` / `copy_table_between_databases` / `run_pg_binary`). |
| `mcpg.listen` | LISTEN/NOTIFY bridge (dedicated connection per subscription; bounded queue). |
| `mcpg.cron` | `pg_cron` schedule / unschedule / update wrappers. |
| `mcpg.partman` | `pg_partman` partition-management wrappers. |
| `mcpg.timescale` | TimescaleDB hypertable / compression / retention wrappers. |
| `mcpg.extensions` | Allowlisted `enable_extension`. |
| `mcpg.diesel` / `mcpg.drizzle` / `mcpg.ecto` / `mcpg.ent` / `mcpg.prisma_export` / `mcpg.sqlalchemy_export` / `mcpg.sqlc_export` / `mcpg.jooq_export` | ORM schema exporters. |
| `mcpg.observability` | Prometheus counters + histograms and `get_metrics_exposition`. |
| `mcpg._vendor` | Vendored MIT-licensed `SafeSqlDriver` and connection-pool kernel (see below). |

`mcpg.__main__` is the `mcpg` console entry point; handles the
`--version` flag and falls through to `run(load_settings())`.

---

## The vendored SQL-safety kernel

`src/mcpg/_vendor/sql/` is a pinned copy of the SQL-safety
subpackage from
[`crystaldba/postgres-mcp`](https://github.com/crystaldba/postgres-mcp)
(MIT). It provides `SafeSqlDriver` — a `pglast`-AST allowlist
validator — and the base connection pool / driver.

The kernel is kept near-verbatim, excluded from the coverage gate
and `mypy`, and re-synced via the procedure in
`src/mcpg/_vendor/README.md`. See [ADR-0001](adr/0001-build-approach.md).

---

## Access-mode & capability model

`mcpg.policy` maps each access mode to a set of capabilities:

| Access mode | Capabilities granted |
|---|---|
| `read-only` | `READ` |
| `restricted` | `READ` |
| `unrestricted` | `READ`, `WRITE` |

Additional gates within `unrestricted` add fine-grained
capabilities:

| Env var | Capability |
|---|---|
| `MCPG_ALLOW_DDL=true` | `DDL` |
| `MCPG_ALLOW_SHELL=true` | `SHELL` |
| `MCPG_ALLOW_LISTEN=true` | `LISTEN` |

`register_tools` consults the policy so a tool is only exposed to
the MCP client when its required capability is permitted. There is
**no module-level mutable state** — settings, the database, the
listen manager, and the cursor manager all live in the server
lifespan's `AppContext`, passed to tools via `Context.lifespan_context`.

---

## Transport & HTTP middleware stack

For `MCPG_TRANSPORT=streamable-http` or `sse`, `mcpg.http_runtime`
constructs a Starlette app with this middleware stack (outermost
first):

1. **Bearer / OIDC authentication.** Static
   (`MCPG_HTTP_AUTH_TOKEN` constant-time compare) or full JWT
   validation against an OIDC issuer's JWKS. `/metrics` / `/healthz`
   / `/readyz` are exempt by design.
2. **Per-request role propagation.** Reads `X-MCPG-Role` (or the
   OIDC role claim when `MCPG_OIDC_ROLE_CLAIM` is set), validates
   against `MCPG_ALLOWED_ROLES`, and stashes the value in the
   `current_role` ContextVar that the `TenantSqlDriver` reads.
3. **The MCP transport handler** (FastMCP-provided).

Plus three first-party endpoints under the same auth-exempt rules:

- `GET /metrics` — Prometheus text format
- `GET /healthz` — liveness
- `GET /readyz` — readiness (verifies a pool connection)

---

## Security model (summary)

Read-only by default; every agent-supplied SQL statement is parsed
and allowlist-checked before execution; writes are validated as a
single statement of an expected kind; identifiers everywhere flow
through a `[A-Za-z_][A-Za-z0-9_]*` regex; credentials are redacted
from logs and audit trail; PG TLS is enforced on startup; HTTP
transports require a bearer token or OIDC JWT; per-session
`statement_timeout` and `lock_timeout` are set on each pool
checkout. The full threat model is in [`security.md`](security.md);
the shipped-vs-queued roadmap is in
[`security-hardening.md`](security-hardening.md).

---

## Graceful degradation for optional extensions

Tools that depend on an optional extension check for it at call
time and return an `available: false` result instead of failing
when it's absent. Affected tools:

| Tool | Required extension |
|---|---|
| `fuzzy_search` | `pg_trgm` |
| `analyze_workload`, `detect_n_plus_one` | `pg_stat_statements` |
| `vector_search`, `vector_range_search`, `hybrid_search`, `recommend_vector_*`, `analyze_vector_*` | `vector` (pgvector) |
| `geo_search` | `postgis` |
| `pg_cron.*` | `pg_cron` |
| `partman.*` | `pg_partman` |
| `list_hypertables`, `create_hypertable`, `add_compression_policy`, `add_retention_policy`, `list_chunks` | `timescaledb` |
| `list_graphs`, `describe_graph`, `run_cypher`, `create_graph`, `drop_graph`, `generate_graph_diagram` | `age` (Apache AGE) |

`describe_table` and `list_indexes` surface `pgvector` /
index-method details when present without requiring the extension
otherwise.

---

## Testing approach

MCPg is test-driven across three suites:

| Suite | Scope |
|---|---|
| `tests/unit/` | Fake-driver tests with a 90% coverage gate. Authored code only — the vendored kernel keeps its own tests. |
| `tests/integration/` | Real PostgreSQL — requires `MCPG_TEST_DATABASE_URL`. CI matrix runs against PostgreSQL 14, 15, 16, 17, 18 on a pgvector + PostGIS + AGE-enabled image. |
| `tests/vendor/` | The vendored kernel's own upstream tests, kept for adversarial SQL-injection coverage. |

The integration container is built from
`.github/ci-postgres.Dockerfile` and includes `pgvector`,
`postgis`, `pg_trgm`, `pg_stat_statements`, and Apache `age`.

---

## Configuration & deployment

Configuration is **entirely** environment-variable driven — no
config file, no flags beyond `--version`. The full env-var
reference is in the [README](../README.md#configuration); the
narrative is in [`installation.md`](installation.md).

MCPg ships as both a PyPI package (`pip install mcpg`) and a
hardened multi-stage Docker image — the runtime stage drops the
build toolchain, runs as `uid=10001 / gid=10001` with `nologin`,
and keeps application files root-owned and read-only.

Scaling characteristics, pool sizing, and observability guidance
live in [`scaling.md`](scaling.md).

---

## See also

- [`adr/`](adr/) — accepted architecture decision records.
- [`tour.md`](tour.md) — tool discovery surface, grouped by
  intent.
- [`tools.md`](tools.md) — full per-tool reference.
- [`security.md`](security.md) — threat model.
- [`security-hardening.md`](security-hardening.md) — shipped vs
  queued hardening roadmap.
- [`scaling.md`](scaling.md) — load behaviour and tuning.
- [`release-process.md`](release-process.md) — release playbook.
