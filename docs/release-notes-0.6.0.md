# MCPg v0.6.0 — release notes

**Released:** 2026-06-05
**Tool surface:** 107 → **141**  (+34)
**Tests:** 1354 pass
**CI:** PG 14 / 15 / 16 / 17 / 18

This release is the **security + observability + analytics wave**:
every HTTP transport gains TLS/mTLS, an IP allowlist, and cloud
secrets backends; every tool call now emits one structured log line
plus (optionally) one OpenTelemetry span; and pgvector grows nine new
analytics tools alongside a long-needed `recommend_index_drops`
advisor. Twenty-plus PRs landed on top of v0.5.1.

| Theme | PRs | Highlights |
|-------|-----|------------|
| Security hardening | #63 / #64 / #65 | HTTP IP allowlist (C1), TLS / mTLS (C2), cloud secrets backends — Vault / AWS / GCP (C3) |
| Observability | #46 / #47 / #60 | Structured JSON logs (B1), slow-call logging (B2), OpenTelemetry tracing (B3) |
| Migrations | #55 / #56 / #61 | Migration history reader (E1), zero-downtime cookbook + pre-deployment validation (E2/E3), unapplied-script delta (E4) |
| pgvector analytics | #42 / #48–#50 / #57–#59 | mmr_search, import_vectors, analyze_distance_metric, cross_table_similarity, cluster_vectors, detect_vector_outliers, monitor_embedding_drift, migrate_vector_to_halfvec |
| Advisors | #66 | `recommend_index_drops` — sibling of `recommend_indexes` for what to remove |
| Extension surface | #53 / #54 / #45 | pg_buffercache (D2), pg_walinspect (D3), `walk_blocking_chains` (D4) |
| UX | #44 / #56 / #62 | `generate_schema_docs` (F1), inline usage examples in tool descriptions (F2), sample-data seeding (F3) |
| NL→SQL | #35 | Multi-provider routing + auto-discovery + per-call provider arg |
| Hardening / lifecycles | #37 / #38 / #40 | HTTP middleware (security headers, body cap, CORS), graceful-shutdown draining, HMAC audit-log integrity chain, `verify_connection_encryption`, `prune_audit_events`, subprocess hardening, per-request HTTP timeout |
| Adaptive caching / feature flags | #36 | Adaptive caching + feature-flag plumbing |

## Headlines

### TLS / mTLS for the HTTP transport (`MCPG_HTTP_TLS_*`)

Production deployments no longer need a reverse proxy just for TLS
termination. When `MCPG_HTTP_TLS_CERTFILE` + `MCPG_HTTP_TLS_KEYFILE`
are set, `run_http` instructs uvicorn to terminate TLS itself. Adding
`MCPG_HTTP_TLS_CA_CERTS` plus
`MCPG_HTTP_TLS_CLIENT_CERT_REQUIRED=true` upgrades the listener to
full mutual TLS — connections without a client cert signed by a CA
in the bundle are refused at the handshake layer, before any ASGI
middleware sees the request.

Settings are cross-validated at boot: cert/key paired, mTLS requires
a CA bundle, every path must exist on disk — so a typo fails
`load_settings` instead of the first request. Disabled by default;
operators behind a reverse proxy keep terminating TLS at the proxy
as before.

### HTTP IP allowlist (`MCPG_HTTP_IP_ALLOWLIST`)

A first-line network filter for the HTTP transports. Set a
comma-separated list of IPs and/or CIDRs and every request is
matched against them before any other middleware runs. Non-matching
clients receive a minimal 403 with no body specifics — a scanner
can't fingerprint the allowlist.

Entries are validated at boot (a malformed entry fails
`load_settings` instead of the first request). The matched IP is the
immediate connecting peer; `X-Forwarded-For` is deliberately not
honoured because trusting a forwarded header without a verified
upstream is a known spoofing vector — operators behind a reverse
proxy should enforce the allowlist at the proxy layer where TLS
terminates.

### Cloud secrets backends — Vault / AWS / GCP

The pluggable secrets provider shipped in v0.5.1 (with `env` + `file`)
now extends to three real cloud backends, each behind its own extra
so a deployment only pays for the SDK it uses:

| Backend | Extra | Env switch | Notes |
|---|---|---|---|
| HashiCorp Vault (KV v2) | `mcpg[vault]` | `MCPG_SECRETS_BACKEND=vault` | `MCPG_VAULT_ADDR` + `MCPG_VAULT_TOKEN`, optional `MCPG_VAULT_NAMESPACE`, `MCPG_VAULT_PATH_PREFIX` (default `secret/mcpg`). |
| AWS Secrets Manager | `mcpg[aws]` | `MCPG_SECRETS_BACKEND=aws` | Standard AWS env / IAM-role chain. `MCPG_AWS_SECRETS_PREFIX` prepended to every name. `SecretString` auto-detected as JSON (per-field) or single-value. |
| GCP Secret Manager | `mcpg[gcp]` | `MCPG_SECRETS_BACKEND=gcp` | `MCPG_GCP_PROJECT_ID` required, optional `MCPG_GCP_SECRETS_PREFIX`. Path: `projects/{project}/secrets/{prefix+name}/versions/latest`. |

Every backend preserves the `file`-backend semantics: a name present
in the backend wins; anything absent falls back to the process
environment, so partial backends and vendor-conventional env vars
keep working. Each provider caches lookups in-process for the
lifetime of the server, lazily imports its SDK (so missing-deps
errors only fire on first lookup), and distinguishes auth/permission
failures (raised as `SecretsError`) from resource-not-found (silent
env fallback).

### OpenTelemetry tracing (`MCPG_OTEL_ENABLED`)

One span per `call_tool` on the `mcpg.tools` tracer when the
optional `mcpg[otel]` extra is installed. Span attributes:
`mcp.tool.name`, `mcp.tool.argument_count`, `mcp.tool.status`,
and `error.type` / `error.message` (truncated at 200 chars) on
failure. Span status is set to OK / ERROR so backends that surface
that field light up failure cases without parsing attribute text.

Raw argument *values* are deliberately not attached — tool
arguments can carry secrets / PII. Standard `OTEL_*` env vars
(collector endpoint, headers, resource attributes, sampler) take
precedence; `MCPG_OTEL_SERVICE_NAME` is the only project-specific
knob and only applies when `OTEL_RESOURCE_ATTRIBUTES` doesn't
already set `service.name`. Disabled by default.

### Structured JSON logging + slow-call warnings

```text
# MCPG_LOG_FORMAT=json
{"timestamp": "...", "level": "INFO", "logger": "mcpg.server", "message": "..."}
{"timestamp": "...", "level": "INFO", "logger": "mcpg.audit",
 "tool": "run_select", "status": "ok", "arguments": {...}, "error": null}
```

`MCPG_LOG_FORMAT=json` flips every line on the `mcpg` logger to
structured JSON. `mcpg.audit` lines merge the audit payload
(`tool`, `status`, `arguments`, `error`) directly into the top
level — log aggregators (Loki, ELK, Datadog) consume them without
custom parsers.

`MCPG_SLOW_CALL_THRESHOLD_MS` (default `1000`) logs a WARN on
the `mcpg.server` logger when any tool execution exceeds the
threshold. A value of `0` disables.

### pgvector analytics suite

Nine new tools cover the analytics gap between "store embeddings"
and "operate on them":

* `mmr_search` — diversity-aware re-ranking (Maximal Marginal
  Relevance) over a pgvector recall pass; better LLM context than
  raw top-k.
* `cluster_vectors` — in-process k-means with k-means++ seeding,
  empty-cluster re-seeding, and a centroid-drift convergence
  check; deterministic via `seed`.
* `detect_vector_outliers` — k-means + per-cluster z-score, so
  outliers are weird-for-their-group rather than weird-overall.
* `monitor_embedding_drift` — two-window centroid drift +
  cosine-distance check between sampled windows; `drift_detected`
  flips when the cosine distance crosses `drift_threshold`
  (default 0.05).
* `cross_table_similarity` — k-NN from a specific row in source
  table A against `target.table.column`; mismatched dimensions
  surface up-front from the catalog rather than as a pgvector cast
  error.
* `analyze_distance_metric` — samples L2 norms and recommends
  `inner_product` / `cosine` / `l2` based on the magnitude
  distribution.
* `import_vectors` — bulk-load embeddings from JSON or CSV with
  full dimension validation before any INSERT runs (no partial
  loads on bad rows).
* `migrate_vector_to_halfvec` — read-only DDL planner that emits
  the ordered SQL to convert a `vector(N)` column to `halfvec(N)`,
  with a mirror rollback. Refuses any ANN index whose opclass has
  no halfvec sibling rather than rewriting it incorrectly.
* `monitor_index_build` — surfaces every active `CREATE INDEX`
  from `pg_stat_progress_create_index` with a computed
  `progress_pct`.

All live in `mcpg.vector_ops` (the new vector-analytics namespace,
separate from search and storage tuning). Read-only;
`available=false` when pgvector isn't installed.

### `recommend_index_drops` — what to remove

```python
recommend_index_drops(schema="public", min_index_size_bytes=1_000_000)
```

The natural sibling of `recommend_indexes`. Walks
`pg_stat_user_indexes` + `pg_stat_user_tables` and flags existing
indexes that look like pure cost. Three reason codes, descending
strength:

* `never_used` — `idx_scan = 0`; safe drop.
* `scan_no_fetch` — planner picks it but it returns no rows;
  usually an existence-check pattern a partial index would serve
  more cheaply.
* `rarely_used` — scan rate below `low_scan_ratio` (default 1%) of
  the table's total scan activity.

Primary-key, unique, and exclusion-constraint indexes are
excluded (dropping those would be a schema change, not a
performance win); indexes below `min_index_size_bytes`
(default 1 MB) are skipped. Each result carries a ready-to-run
`DROP INDEX CONCURRENTLY` statement; results sort by reason
strength then size descending. Read-only — execution is on the
operator.

### Migration ergonomics — E1..E4

* `read_migration_history` (E1) — read-only inspection of the
  bookkeeping tables for Alembic, Flyway, Diesel, Django, Prisma,
  Golang Migrate, Goose, Sequelize.
* Zero-downtime migration cookbook (E2) — comprehensive recipes
  for concurrent indexes, `NOT VALID` + `VALIDATE CONSTRAINT`,
  column renames, and type changes. Lives in `docs/cookbook.md`.
* `validate_migration_schema` (E3) — apply a candidate migration
  against a transient shadow schema cloned from a reference
  snapshot, then run `compare_schemas` against the reference.
  DDL-gated.
* `list_unapplied_migration_scripts` (E4) — pairs on-disk scripts
  (the source of truth for what *should* run) with the database's
  history table (what *has* run) and reports the delta. Walks
  `MCPG_MIGRATION_SCRIPTS_ROOTS`-allowlisted directories for
  Flyway / Alembic / Liquibase patterns. Filesystem access is
  gated; symlinks dereferenced + `..` resolved before the
  allowlist check so traversal escapes are caught at the gate.

### Inline usage examples in tool descriptions (F2)

~25 high-traffic tools (introspection, query, composite, health,
search, vector analytics, diagrams, schema-diff, migrations, data
movement) now ship a canonical pseudo-Python invocation example at
the end of their MCP description. Agents get a concrete starting
point for tools whose argument shape isn't obvious from the name.
The `_with_example(description, example)` helper in `mcpg.tools` is
the contract for future tools; rendered format always ends with
` Example: \`tool(...)\` `.

### Multi-provider NL→SQL

`translate_nl_to_sql` now auto-discovers every configured provider
from the environment at startup (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY`). Each
configured provider becomes callable through the tool, not just the
configured default. The tool gains an optional
`provider="anthropic"|"openai"|"gemini"` argument so a caller can
route per call.

Fall-through order: explicit `provider=` arg → `MCPG_NL2SQL_PROVIDER`
default → first available in preference order **anthropic → openai →
gemini**. `get_server_info` surfaces `nl2sql_default_provider` and
`nl2sql_available_providers` so agents can introspect.

Enables "one MCPg server, many MCP clients" — set every vendor key
on the host, run one MCPg over HTTP, let each agent pick its
preferred LLM per call.

## Smaller landings

* **`generate_schema_docs`** (F1) — comprehensive Markdown catalog
  reference for tables, views, foreign tables, enums, constraints,
  indexes, comments. Optional `include_samples` flag samples
  first-few non-null distinct values per column. Gated under
  `MCPG_ENABLE_HEAVY_DIAGNOSTICS`; cacheable.
* **`seed_table_with_sample_data`** (F3) — synthetic INSERT
  generation that respects types, NOT NULL, and DEFAULT
  constraints. WRITE-gated.
* **`walk_blocking_chains`** (D4) — reconstructs the active
  lock-wait graph from `pg_blocking_pids`. Identifies simple
  deadlock cycles, linear blocking paths leading to root blockers,
  the list of root blocker PIDs, and renders a styling-annotated
  Mermaid flowchart of the lock dependency graph.
* **`pg_walinspect`** (D3) — `read_pg_wal_records`,
  `read_pg_wal_stats` for WAL record analysis over LSN ranges.
* **`pg_buffercache`** (D2) — `read_pg_buffercache_summary`,
  `read_pg_buffercache_relations` for shared-buffer-cache analysis.
* **`verify_connection_encryption`** — reports whether MCPg's own
  connection to PostgreSQL is TLS-encrypted, plus a cluster-wide
  encrypted/unencrypted backend tally.
* **`prune_audit_events`** — retention sweep on
  `mcpg_audit.events`; refuses to run when `MCPG_AUDIT_INTEGRITY`
  is enabled (pruning would break the HMAC chain).
* **Subprocess hardening** — `pg_dump` / `pg_restore` / `psql`
  resolved-path allowlist (`MCPG_SUBPROCESS_BIN_ALLOWLIST`),
  optional `RLIMIT_CPU` / `RLIMIT_AS`, spawn in a throwaway tempdir.
  All opt-in; defaults preserve prior behaviour.
* **Per-request HTTP timeout** — `MCPG_HTTP_REQUEST_TIMEOUT_SECONDS`
  (default `0` = disabled) caps wall-clock per request, returning
  `504` on expiry. Disabled by default so long-lived SSE /
  streamable-http streams keep working.
* **HMAC audit integrity chain** — `MCPG_AUDIT_INTEGRITY=true` +
  `MCPG_AUDIT_HMAC_KEY` produces a tamper-evident chain over
  `mcpg_audit.events`; the new `verify_audit_chain` tool checks
  it.

## Notable env-var additions

| Variable | Purpose |
|---|---|
| `MCPG_HTTP_TLS_CERTFILE` / `MCPG_HTTP_TLS_KEYFILE` | In-process TLS termination. |
| `MCPG_HTTP_TLS_CA_CERTS` / `MCPG_HTTP_TLS_CLIENT_CERT_REQUIRED` | Promote TLS to mTLS. |
| `MCPG_HTTP_IP_ALLOWLIST` | Comma-separated IP / CIDR allowlist. |
| `MCPG_HTTP_REQUEST_TIMEOUT_SECONDS` | Per-request wall-clock cap (`0` disables). |
| `MCPG_SECRETS_BACKEND` | `env` (default) / `file` / `vault` / `aws` / `gcp`. |
| `MCPG_VAULT_*`, `MCPG_AWS_SECRETS_PREFIX`, `MCPG_GCP_*` | Cloud-backend specific knobs. |
| `MCPG_LOG_FORMAT` | `text` (default) / `json`. |
| `MCPG_SLOW_CALL_THRESHOLD_MS` | Slow-call WARN threshold (default `1000`). |
| `MCPG_OTEL_ENABLED` / `MCPG_OTEL_SERVICE_NAME` | OpenTelemetry tracing. |
| `MCPG_AUDIT_INTEGRITY` / `MCPG_AUDIT_HMAC_KEY` | HMAC integrity chain on audit log. |
| `MCPG_SUBPROCESS_BIN_ALLOWLIST` / `MCPG_SUBPROCESS_CPU_SECONDS` / `MCPG_SUBPROCESS_MEMORY_MB` | Subprocess hardening. |
| `MCPG_MIGRATION_SCRIPTS_ROOTS` | Colon-separated directory allowlist for `list_unapplied_migration_scripts`. |
| `MCPG_SHUTDOWN_DRAIN_SECONDS` | In-flight tool drain window on SIGTERM. |

## Backwards compatibility

* **`Settings.nl2sql_api_key` → `Settings.nl2sql_api_keys`**
  (tuple of `(provider, key)` pairs). Backwards-incompatible only
  for code that imports `Settings` directly — the env-var surface
  stays compatible: `MCPG_NL2SQL_PROVIDER` + vendor-conventional
  env vars still work as before, and `MCPG_NL2SQL_API_KEY` (when
  set) still supplies the key for the configured default provider.
  `MCPG_NL2SQL_API_KEY` now requires `MCPG_NL2SQL_PROVIDER` to
  also be set; startup fails with a clear message if only the key
  is set.
* **`MCPG_NL2SQL_PROVIDER` is now optional** when at least one
  vendor key is in the env — MCPg auto-picks a default. Setting
  it explicitly still pins the default.
* Cloud-secrets extras are opt-in — `pip install mcpg[vault]` etc.
  No new mandatory dependencies.

## Test plan

* `uv run pytest tests/unit` — 1354 passed
* `uv run ruff check .` + `uv run ruff format --check .` — clean
* `uv run mypy src/mcpg` — no issues (68 source files)
* `python -m build` + `twine check dist/*` — wheel + sdist PASSED
* CI matrix PG 14 / 15 / 16 / 17 / 18 — all green on the merge
  commits

## Acknowledgements

Gemini Code Assist and Sourcery AI reviewed every PR in this
release and surfaced a steady stream of correctness, security, and
ergonomic improvements that landed as review-follow-up commits
inside each PR. Notable catches: the `monitor_embedding_drift`
unbiased `ORDER BY RANDOM()` sample (#59), the
`migrate_vector_to_halfvec` `int2vector` → `int2[]` cast for
`ANY()` (#58), the HTTP IP allowlist's accept-list-shaped client
(#63), the cloud-secrets auth-vs-missing-secret split (#65), the
OTel `OTEL_RESOURCE_ATTRIBUTES` parser (#60), and the
`recommend_index_drops` identifier-quoting fix (#66).
