# Changelog

All notable changes to MCPg are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **`monitor_index_build` tool.** Surfaces every active `CREATE
  INDEX` operation from `pg_stat_progress_create_index` (PG12+, no
  extension required). One row per build with PID, resolved
  `schema.relation.index_name`, the command, the phase label, raw
  `blocks_done`/`blocks_total` + `tuples_done`/`tuples_total`
  counters, and a computed `progress_pct` (blocks first, tuples as
  fallback, `null` when neither phase reports a denominator).
  Useful next to `list_active_queries` when an HNSW / IVFFlat build
  on a big table is taking longer than expected. Lives in
  `mcpg.liveops`; read-only.

- **`validate_migration_schema` tool.** Verify a candidate migration SQL against a reference schema definition. Clones the target schema (production snapshot) into a transient shadow schema, applies the candidate DDL, and runs `compare_schemas` against the reference schema. Gated under DDL (`unrestricted` access mode + `MCPG_ALLOW_DDL=true`).

- **`seed_table_with_sample_data` tool.** Generate and execute synthetic `INSERT` statements to seed a table with sample data. Values respect column types, NOT NULL, and DEFAULT constraints. Gated under WRITE (`unrestricted` access mode).

- **Zero-Downtime Migration Cookbook.** Added a comprehensive guide under `docs/cookbook.md` outlining recipes for safe, zero-downtime operations in PostgreSQL (such as concurrent indexes, validating constraints/FKs using `NOT VALID` + `VALIDATE CONSTRAINT`, column renames, and type changes).

- **`cross_table_similarity` tool (pgvector).** Locates a specific
  row in `source_schema.source_table` by `source_id_column =
  source_id_value`, reads its embedding from
  `source_embedding_column`, and issues a pgvector k-NN against
  `target_schema.target_table.target_embedding_column`. Both columns
  must be `vector(N)` with matching `N` — checked from the catalog
  up front so a mismatch fails with a clear error rather than a
  pgvector cast error on the inner query. Returns
  `source_embedding_found=false` distinctly from
  "no neighbours". Read-only; `available=false` without pgvector.
  Lives in `mcpg.vector_ops`.

- **`read_migration_history` tool.** Adds a read-only tool to inspect and parse the native migration bookkeeping tables of popular migration frameworks (Alembic, Flyway, Diesel, Django, Prisma, Golang Migrate, Goose, Sequelize). Reports migration history records with full framework-specific metadata fields.

- **`pg_walinspect` integration.** Adds `read_pg_wal_records` and
  `read_pg_wal_stats` tools to analyze Write-Ahead Log (WAL) record details and
  aggregated stats over specified LSN ranges. Degrades gracefully if the extension
  is not installed (`available=false`). Adds `pg_walinspect` to the list of
  programmatically enableable extensions.

- **`pg_buffercache` integration.** Adds `read_pg_buffercache_summary` and
  `read_pg_buffercache_relations` tools to analyze shared buffer cache usage
  at the cluster and relation levels. Degrades gracefully if the extension
  is not installed (`available=false`). Adds `pg_buffercache` to the list of
  programmatically enableable extensions.

- **`analyze_distance_metric` tool (pgvector).** Samples up to
  `sample_size` (default 1000) non-NULL embeddings from
  `schema.table.column`, computes each one's L2 norm, and recommends
  a distance metric from the magnitude distribution: pre-normalised
  vectors (CV < 5%, mean ≈ 1.0) → `inner_product`; nearly-constant
  but off-unit magnitudes → `cosine` (same ranking as L2, safer
  default); variable magnitudes → `cosine` (normalises out
  heterogeneous sources). Returns the metric + a rationale + the
  underlying distribution stats. New `mcpg.vector_ops` module — first
  resident of a vector-analytics namespace separate from search
  (`textsearch`) and storage tuning (`vector_tuning`,
  `vector_tuner_advanced`). Read-only; `available=false` without the
  pgvector extension.

- **`import_vectors` tool (pgvector).** Bulk-load embeddings into a
  pgvector `vector(N)` column from JSON (array of objects) or CSV.
  Reads the column's declared `N` from the catalog up-front and
  validates every row in the payload BEFORE any INSERT runs — a
  dimension mismatch on row 1000 fails the whole call rather than
  leaving 999 partial inserts behind. CSV cells accept bracketed
  pgvector literals or comma-separated numbers; JSON accepts lists or
  literal strings. Optional parallel `id_column` for an
  `INSERT (id, embedding) VALUES (%s, %s::vector)` shape. Errors when
  the target column isn't pgvector. Write-gated (unrestricted mode).

- **Slow-call logging from the MCP layer (`MCPG_SLOW_CALL_THRESHOLD_MS`).** Logs a warning
  to the `mcpg.server` logger when any tool execution duration exceeds the configured threshold.
  Defaults to `1000` ms. A value of `0` or less disables this logging.

- **Structured JSON logging toggle (`MCPG_LOG_FORMAT`).** Adds an opt-in structured
  JSON logging format. When `MCPG_LOG_FORMAT=json` is set, all logging output from the
  `mcpg` server is formatted as a structured JSON object. Standard log messages carry
  `timestamp`, `level`, `logger`, and `message` keys, while `mcpg.audit` tool calls merge
  the audit payload (`tool`, `status`, `arguments`, `error`) directly into the top level.

- **`walk_blocking_chains` tool (deadlock cycle walker).** Walks and reconstructs
  the active lock-wait graph of the database using `pg_blocking_pids`. Identifies
  all simple deadlock cycles, linear blocking paths leading to root blockers or
  cycles, list of root blocker PIDs, and renders a styling-annotated Mermaid
  flowchart representing the lock dependency graph. Read-only.

- **`generate_schema_docs` tool (schema documentation).** Generates a
  comprehensive Markdown catalog reference for a database schema's
  tables, views, foreign tables, custom enums, constraints, indexes,
  and comments. Supports an optional `include_samples` flag to sample
  and display the first few non-null, distinct, truncated values for
  each column. Gated under heavy diagnostics (`MCPG_ENABLE_HEAVY_DIAGNOSTICS`)
  and supports caching.

- **`mmr_search` tool (pgvector).** Diversity-aware vector search:
  fetches `fetch_k` nearest candidates by pgvector distance, then
  re-ranks with Maximal Marginal Relevance to return `k` rows that
  are relevant but not near-duplicates — better LLM context than raw
  top-k. `lambda_mult` (0–1) trades relevance for diversity; both the
  relevance and diversity terms are cosine similarities computed
  in-process over candidate embeddings, so the result is independent
  of the recall-pass `metric`. Each hit carries its `relevance`,
  `mmr_score`, and selection `rank`. Read-only; `available=false`
  without the pgvector extension.

- **Pluggable secrets backend (`MCPG_SECRETS_BACKEND`).** A
  `SecretsProvider` abstraction (`mcpg.secrets`) that the secrets read
  in `load_settings` route through — the NL→SQL provider API
  keys, `MCPG_HTTP_AUTH_TOKEN`, and `MCPG_AUDIT_HMAC_KEY`. Two
  backends ship: `env` (default — unchanged behaviour, zero new
  deps) and `file`, which overlays a JSON (or YAML, when PyYAML is
  importable) `name → value` map from `MCPG_SECRETS_FILE_PATH` on
  top of the environment (a name in the file wins; anything absent
  falls back to its env var). Cloud backends (Vault / AWS / GCP)
  will follow behind optional extras using the same switch.
  `Settings.secrets_backend` records the active choice (values never
  appear in `repr`).

- **`verify_connection_encryption` tool.** Reports whether MCPg's own
  connection to PostgreSQL is TLS-encrypted — negotiated protocol
  version, cipher, and key bits from `pg_stat_ssl` — plus a
  cluster-wide encrypted/unencrypted backend tally. A runtime
  complement to the startup TLS-enforcement check. Read-only;
  available in every access mode.

- **`prune_audit_events` retention tool.** Deletes persisted audit
  events older than `older_than_days` from `mcpg_audit.events`, a
  cron-friendly cap on the otherwise-unbounded audit table. Returns
  the number deleted, the cutoff timestamp, and rows remaining.
  Refuses to run when `MCPG_AUDIT_INTEGRITY` is enabled (pruning
  would break the HMAC signature chain). Write-gated (unrestricted
  mode).

- **Subprocess hardening for the shell-gated PG binaries.**
  `run_pg_binary` now (a) validates the resolved `pg_dump` /
  `pg_restore` / `psql` directory against
  `MCPG_SUBPROCESS_BIN_ALLOWLIST` (empty = trust `PATH`), rejecting a
  PATH shim in an untrusted directory while still allowing distro
  `pg_dump -> pg_wrapper` symlinks; (b) applies `RLIMIT_CPU` /
  `RLIMIT_AS` via a `preexec_fn` when `MCPG_SUBPROCESS_CPU_SECONDS` /
  `MCPG_SUBPROCESS_MEMORY_MB` are set (POSIX only); and (c) spawns in
  a throwaway temp working directory. All opt-in; defaults preserve
  prior behaviour.

- **Opt-in per-request HTTP timeout.**
  `MCPG_HTTP_REQUEST_TIMEOUT_SECONDS` (default `0` = disabled) caps
  wall-clock time per request on the HTTP transports, returning `504`
  on expiry. Disabled by default so long-lived SSE / streamable-http
  streams keep working; intended for plain request/response
  deployments wanting a DoS backstop. Completes the HTTP-hardening
  set started in the security-diagnostics release.

- **Advanced security hardening, lifecycles & diagnostics (#37).**
  HTTP hardening middleware (security headers, request-size limit,
  CORS allowlist); graceful-shutdown draining of in-flight tool calls
  (`MCPG_SHUTDOWN_DRAIN_SECONDS`); audit-log HMAC integrity chain with
  the `verify_audit_chain` tool (`MCPG_AUDIT_INTEGRITY` +
  `MCPG_AUDIT_HMAC_KEY`); a redundant-index advisor; and an
  `analyze_hnsw_recall` pgvector tuner.

- **Adaptive caching, feature flags & related (#36).** See the PR for
  detail.

- **Multi-provider routing for `translate_nl_to_sql`.** MCPg now
  auto-discovers every configured NL→SQL provider from the
  environment at startup (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `GEMINI_API_KEY` / `GOOGLE_API_KEY`) — each one becomes callable
  through the tool, not just the configured default. The tool gains
  an optional `provider="anthropic"|"openai"|"gemini"` argument so a
  caller can route per-call across configured providers; without it,
  MCPg falls back to `MCPG_NL2SQL_PROVIDER` (the default), then to
  the first available in preference order **anthropic → openai →
  gemini**. `get_server_info` surfaces `nl2sql_default_provider` and
  `nl2sql_available_providers` so agents can introspect.

  Enables the "one MCPg server, many MCP clients" deployment shape:
  set every vendor key on the host, run one MCPg over HTTP, let each
  agent / IDE pick its preferred LLM per call.

### Changed

- **`Settings.nl2sql_api_key` (single value) → `Settings.nl2sql_api_keys`
  (tuple of `(provider, key)` pairs).** Backward-incompatible only
  for code that imports `Settings` directly — the env-var surface
  stays compatible: `MCPG_NL2SQL_PROVIDER` + vendor-conventional env
  vars still work as before, and `MCPG_NL2SQL_API_KEY` (when set)
  still supplies the key for the configured default provider.
  `MCPG_NL2SQL_API_KEY` now requires `MCPG_NL2SQL_PROVIDER` to also
  be set (MCPg can't tell which provider a stray key belongs to);
  startup fails with a clear message if only the key is set.

- **`MCPG_NL2SQL_PROVIDER` is now optional** when at least one
  vendor key is in the env — MCPg auto-picks a default. Setting it
  explicitly still pins the default.

## [0.5.1] - 2026-05-29

Inaugural PyPI release. Three landings since 0.5.0: security
hardening, license switch to MIT, and the packaging surface needed
to publish to PyPI.

### Added

- **`mcpg --version` (`mcpg -V`) flag.** Prints `mcpg <version>` so
  bug reporters can paste the line `SECURITY.md` asks for. Backed
  by `mcpg.__version__`.

- **PyPI publishing pipeline.** New `.github/workflows/publish.yml`
  triggers on `v*.*.*` tag pushes and runs: build → twine check →
  TestPyPI upload → install-smoke against the published TestPyPI
  artifact (deps resolved from real PyPI first, then `mcpg` with
  `--no-deps` from TestPyPI — closes a dependency-confusion vector)
  → reviewer-gated PyPI upload via Trusted Publishing OIDC → cut a
  GitHub Release with notes pulled from `CHANGELOG.md`. The full
  playbook lives at `docs/release-process.md`.

- **`pyproject.toml` packaging metadata.** Adds `authors`,
  `maintainers`, `keywords`, `classifiers`, `[project.urls]`
  (Homepage / Documentation / Repository / Issues / Changelog /
  Release notes / Security), and `[tool.hatch.build.targets.sdist]`
  to keep the sdist tight. Adds `build>=1.2` + `twine>=5.1` to the
  `dev` dep group.

- **"Install from PyPI" section in `docs/installation.md`.**
  Covers both `pip install mcpg` and `uv tool install mcpg`.

- **Per-session `statement_timeout` / `lock_timeout`** (PR #32).
  Every checked-out pool connection has
  `statement_timeout=MCPG_STATEMENT_TIMEOUT_MS` (default 30000) and
  `lock_timeout=MCPG_LOCK_TIMEOUT_MS` (default 5000) applied once
  per connection via a single batched `SET` — runaway queries and
  hanging lock waits self-terminate without operator intervention.
  Applies to the primary pool and every replica pool.

### Changed

- **Hardened multi-stage Docker image** (PR #32). New runtime stage
  drops the build toolchain entirely; runs as a non-root user
  (`uid=10001 / gid=10001`) with a `nologin` shell. Application
  files stay owned by root and read-only to the `mcpg` user so a
  remote-code-execution bug can't modify the application on disk
  to persist. Entrypoint switches from `uv run` to `python -m mcpg`
  for a smaller process tree.

### Security

- **PG TLS enforcement at startup.** `load_settings` now refuses to
  start when `MCPG_DATABASE_URL` (or any entry in
  `MCPG_REPLICA_URLS`) points at a non-loopback host with an
  `sslmode` of `disable` / `allow` / `prefer` (or no `sslmode` set —
  libpq's default falls back to plaintext). Uses
  `psycopg.conninfo.conninfo_to_dict` so the check covers both URI
  DSNs and keyword/value DSNs (e.g. `host=db sslmode=disable`) plus
  failover multi-host URIs (e.g. `postgresql://h1,h2/db`); the
  earlier `urllib.parse`-based path silently bypassed the check on
  both shapes. DSNs with no explicit host are refused too — libpq
  can resolve `PGHOST` to a non-loopback default. Bypassed with the
  explicit opt-out `MCPG_ALLOW_INSECURE_TLS=true`. Loopback hosts
  (`localhost`, `127.0.0.1`, `::1`) are exempt. Replica errors
  identify the offending index (`MCPG_REPLICA_URLS[1]`) for
  diagnostics.

  > **Upgrade note.** This is a default-tightening change. Any
  > deployment already configured with a remote DSN whose `sslmode`
  > is `disable` / `allow` / `prefer` (or unset) will fail to
  > start after upgrade. Either set `sslmode=require` (or
  > `verify-ca` / `verify-full`) in the DSN — strongly recommended —
  > or opt out with `MCPG_ALLOW_INSECURE_TLS=true`.

- **Tool-argument audit redaction upgraded to regex pattern match.**
  `mcpg.audit.redact_arguments` and the persisted audit-trail
  walker (`mcpg.audit_trail._redact`) now share a case-insensitive
  regex matched via `re.search` against argument key names, so
  `password` also masks `PGPASSWORD` / `user_password` /
  `app.password`. Default patterns: `password`, `passwd`, `secret`,
  `token`, `api[_-]?key`, `bearer`, `authorization`, `database_url`,
  `dsn`, `conninfo`. Operators extend the list via
  `MCPG_AUDIT_REDACT_KEYS` (comma-separated regex fragments). Walks
  nested dicts / lists / tuples so credentials buried in result
  payloads are masked too.

- **Supply-chain CI hardening (dependency audit fix).** The
  `security` job in `.github/workflows/ci.yml` was invoking
  `uv audit` — a subcommand `uv` does not provide — so the
  dependency-vulnerability step has been a silent no-op since it
  was added. Replaced with `pip-audit --strict` over the
  `uv export`-resolved runtime requirements; the existing bandit
  SAST step is unchanged. Adds `pip-audit>=2.7` to the `dev`
  dependency group.

- **Vulnerability-reporting policy + hardening roadmap shipped.**
  New `SECURITY.md` at the repo root documents supported versions,
  the reporting address (`devopam@gmail.com`), the 3-business-day
  acknowledgement target, and the 90-day coordinated-disclosure
  window. New `docs/security-hardening.md` is a living checklist
  of robust-security features with status indicators
  (`✅` shipped / `🟡` partial / `⬜` queued) covering what's on
  `main` today plus the queued items (HTTP request limits +
  security headers, audit-log HMAC chain, pluggable secrets
  backend, subprocess hardening, graceful shutdown).

### Changed

- **License: relicensed from AGPL-3.0-or-later to MIT.** New `LICENSE`
  at the repo root carries the standard MIT text. `pyproject.toml`
  `license` field, `NOTICE`, ADR-0001, and the README "License" section
  updated to match. The vendored SQL-safety kernel at
  `src/mcpg/_vendor/sql/` is and remains MIT-licensed; no upstream
  attribution changed. No code surface changed.

### Added

- **Apache AGE graph + Cypher support** (PR #24). Six new tools
  wired into the read / DDL surfaces:
  - `list_graphs()` and `describe_graph(graph_name)` read
    `ag_catalog`, returning the graphs in the database plus per-graph
    label / edge / property statistics. Read-only.
  - `run_cypher(graph_name, cypher_query)` executes arbitrary
    Cypher against a named graph and returns a typed result
    (`columns` / `rows` / `row_count`). The `graph_name` identifier
    is validated (alphanumeric / underscores, not starting with a
    digit). The query is scanned for write keywords (`CREATE` /
    `SET` / `DELETE` / `REMOVE` / `MERGE`) and gated under the
    WRITE capability when any are present — reads stay under READ.
  - `generate_graph_diagram(graph_name, max_labels=50)` emits a
    Mermaid graph of label-to-label relationships — the graph
    equivalent of `generate_schema_diagram`.
  - `create_graph(graph_name)` / `drop_graph(graph_name,
    cascade=true)` are DDL, gated under unrestricted +
    `MCPG_ALLOW_DDL`.
  - Composes with the existing advisor surface — `run_advisors`
    now reports a `recommend_graph_indices` rule when AGE labels
    lack a property-search index that an expected access pattern
    would need.

- **Read-replica routing** (Shortlist 1.6). When `MCPG_REPLICA_URLS`
  is set (comma-separated DSN list), every `force_readonly=True`
  query is round-robin routed to a healthy replica; writes always
  go to the primary. New `mcpg.replicas` module owns per-replica
  pools + degraded-replica state with a 30s retry window; replica
  connection failures at startup are logged + marked degraded
  individually rather than aborting startup. Composes with the
  Phase-1.4 tenancy driver — each replica gets its own
  `TenantSqlDriver`. New `list_replicas` MCP tool reports index /
  password-obfuscated DSN / degraded / last_error /
  seconds_until_retry per replica. Routing decisions land in
  Prometheus metrics under `mcpg_tool_calls_total` with synthetic
  tool name `__replica_route` and statuses `primary` /
  `primary_no_healthy` / `fallback` / `replica_<n>`.

- **OIDC bearer-token validation** (Shortlist 6.5). New
  `MCPG_AUTH_MODE=oidc` swaps the static-token compare for full JWT
  validation against an OIDC issuer's JWKS. New `mcpg.oidc.OIDCVerifier`
  fetches the discovery doc on first use (cached), caches JWKS keys
  via `PyJWKClient` (default 1h), and validates each request's JWT
  against signature + iss + aud + exp + nbf with 30s clock leeway.
  Only asymmetric algorithms (RS256/RS384/RS512 + ES256/ES384/ES512)
  are accepted — HS-family is excluded to preserve the OIDC trust
  model. When `MCPG_OIDC_ROLE_CLAIM` is set the claim's value is
  validated as a safe PG identifier and stashed in the same
  `current_role` ContextVar the X-MCPG-Role middleware uses, so the
  tenanted driver issues `SET LOCAL ROLE "<role>"` for the request.
  Adds `pyjwt[crypto]` as a runtime dependency. The static
  `MCPG_HTTP_AUTH_TOKEN` path is unchanged when `MCPG_AUTH_MODE=static`
  (the default).

- **Docs refresh** — `docs/tour.md` tool count 90 → 108 + new
  sections for cursors / linting / RLS / replicas / NL→SQL.
  `docs/cookbook.md` adds two new recipes (replica routing,
  OIDC). README headline updated.

## [0.5.0] - 2026-05-27

Thirty-three new MCP tools and four major runtime features, closing
the full `docs/feature-shortlist.md` (**Tier A + Tier B + Tier C**)
plus an NL→SQL helper. Brings the total MCP tool surface from **74
to 107** and adds: HTTP transport bearer-token auth, Prometheus
`/metrics`, TimescaleDB wrappers, hybrid (vector + FTS) search,
per-request `SET ROLE` multi-tenancy, server-side cursors, RLS
testing, synthetic test-data generation, FK cascade graphs, and a
natural-language → SQL helper.

### Headline features

- **Per-request multi-tenancy.** `MCPG_DEFAULT_ROLE` + `X-MCPG-Role`
  header drive a `TenantSqlDriver` that wraps every query in
  `BEGIN ... SET LOCAL ROLE "<role>" ...` so one MCPg process can
  serve N tenants from a single connection pool. Role names
  validated, allowlist via `MCPG_ALLOWED_ROLES`.
- **HTTP transport bearer-token auth + Prometheus `/metrics`.**
  `MCPG_HTTP_AUTH_TOKEN` gates the streamable-http / sse surface;
  `/metrics`, `/healthz`, `/readyz` are exempt so a Prometheus
  scraper doesn't hold the MCP credential.
- **NL→SQL.** `translate_nl_to_sql` sends a schema brief to a
  pluggable LLM provider (Anthropic / OpenAI / Gemini), parses the
  JSON response, and optionally executes the generated SQL through
  the existing `SafeSqlDriver` allowlist.
- **Server-side cursors** via a `CursorManager` holding dedicated
  per-cursor connections — pageable reads through millions of rows
  without starving the main pool.

### Added

- **Tier-A milestone closed** — three picks from
  `docs/feature-shortlist.md` shipped together. Tool surface 84 → 90.
  - **HTTP transport bearer-token auth** (shortlist 1.1). New
    `mcpg.http_runtime` module wraps FastMCP's `streamable_http_app()`
    / `sse_app()` with an ASGI `_BearerAuthMiddleware`. When
    `MCPG_HTTP_AUTH_TOKEN` is set, every request needs
    `Authorization: Bearer <token>` (constant-time compared via
    `hmac.compare_digest`); missing / wrong tokens get a 401 with
    `WWW-Authenticate: Bearer realm="mcpg"`. `/metrics`, `/healthz`,
    and `/readyz` are exempt so a Prometheus scraper / load-balancer
    probe doesn't need the MCP token. New settings field
    `Settings.http_auth_token`. The `stdio` transport is unaffected
    (no HTTP surface). The runtime logs a WARNING when an HTTP
    transport starts without a token.
  - **Prometheus `/metrics` endpoint + `get_metrics_exposition` tool**
    (shortlist 2.1). New `mcpg.observability` module records every
    `call_tool` invocation in an in-process `Metrics` store and
    renders the standard text-exposition format (v0.0.4) — zero
    runtime dependency. Three series: `mcpg_tool_calls_total{tool,
    status}` (counter), `mcpg_tool_duration_seconds_bucket{tool,le}`
    (histogram with default Prometheus buckets + 30s/60s overflow),
    `mcpg_tool_duration_seconds_sum/_count{tool}`. `AuditedFastMCP`
    times every tool call and records (`ok` | `error`) +
    wall-clock seconds. The new `get_metrics_exposition` MCP tool
    returns the same payload over the MCP protocol for stdio
    transports where `/metrics` isn't reachable.
  - **TimescaleDB hypertable wrappers** (shortlist 4.2). New
    `mcpg.timescaledb` module adds five tools — two read-only
    (`list_hypertables`, `list_chunks`) plus three DDL-gated writes
    (`create_hypertable`, `add_compression_policy`,
    `add_retention_policy`). Every interval / identifier is
    allowlist-validated before being inlined into SQL (TimescaleDB's
    management functions take interval expressions as positional
    args, not bound params). Each tool degrades to
    `available=False` when the `timescaledb` extension is missing —
    same pattern as the existing pg_trgm / pgvector / postgis
    integrations.

- **Tier-B milestone closed** — four picks from the feature shortlist
  shipped together. Tool surface 90 → 93 plus the runtime tenancy
  feature.
  - **`find_sensitive_columns`** (6.2). Scans `pg_attribute` for
    columns whose names or types look like they hold PII / secrets:
    seven categories (credential, financial, contact, identifier,
    health, government_id, location) with high / medium / low
    confidence. Pure heuristic — no row sampling. Lives in
    `mcpg.advisors`.
  - **`detect_n_plus_one`** (8.4). Walks `pg_stat_statements` for
    the classic N+1 shape: query templates called hundreds-to-
    thousands of times, each returning ≤ `max_rows_per_call` rows
    and accumulating ≥ `min_total_ms` of wall-clock time. Sorted
    by total time desc; degrades to `available=false` on databases
    without `pg_stat_statements`.
  - **`validate_migration`** (9.2). Applies `candidate_sql` to a
    TRANSIENT shadow of `target_schema` pre-populated with up to
    `sample_rows_per_table` rows from each base table. Catches
    failure modes a structural diff misses: NOT NULL added to a
    column with NULLs, CHECK constraints violated by live rows,
    type narrowings that fail. Always drops the shadow before
    returning. Gated under MIGRATE.
  - **Per-request `SET ROLE` multi-tenancy** (1.4). New
    `mcpg.tenancy.TenantSqlDriver` subclasses the vendored driver
    and wraps every query in an explicit transaction with
    `SET LOCAL ROLE "<role>"`. Role resolved per-request from the
    `X-MCPG-Role` header (HTTP only) or falls back to
    `MCPG_DEFAULT_ROLE`. Role names validated against
    `[A-Za-z_][A-Za-z0-9_]*`; `MCPG_ALLOWED_ROLES` configures an
    allowlist enforced both at startup (default must be in it) and
    per request (403 if not in it). `SET LOCAL` auto-resets at txn
    end — no state leak into the pool. `_TenantRoleMiddleware` sits
    above bearer auth so unauthenticated requests can't reach the
    role parser.

- **Tier-C milestone closed** — every remaining pick from the
  shortlist. Tool surface 93 → 106 (13 new tools), plus a small
  follow-up fix.
  - **Catalog readers** — `list_generated_columns` (4.7) reads
    `pg_attribute.attgenerated` for stored-generated columns;
    `list_locks` + `find_blocking_chains` (4.5, new module
    `mcpg.locks`) join `pg_locks` / `pg_blocking_pids` with
    `pg_stat_activity`; `read_pg_stat_io` (4.3, new module
    `mcpg.io_stats`) wraps the PG16+ I/O stats view (degrades on
    14/15).
  - **`lint_naming_conventions`** (8.1, new module `mcpg.naming`).
    Detects the majority case style per schema and per table
    (snake_case / camelCase / PascalCase / SCREAMING_SNAKE), flags
    outliers, plus an index-prefix rule.
  - **`generate_fk_cascade_graph`** (8.5). Mermaid `graph LR` of
    foreign-key cascade chains; only CASCADE / SET NULL / SET
    DEFAULT FKs by default. Cross-schema targets get their schema
    prefix preserved.
  - **`run_select_parallel`** (3.4). Up to `parallel_limit`
    concurrent SELECTs via `asyncio.gather`; each goes through the
    same safety allowlist as `run_select`; one bad query doesn't
    abort the others.
  - **Server-side cursors** (3.1, new module `mcpg.cursors`). Four
    tools — `open_cursor`, `fetch_cursor`, `close_cursor`,
    `list_cursors`. Each cursor holds a DEDICATED psycopg
    connection (not a pool checkout) inside a `READ ONLY`
    transaction, with a per-cursor `asyncio.Lock` so concurrent
    fetch / close on the same cursor can't corrupt the wire
    protocol. Hard cap of 16 concurrent cursors; 5-min idle TTL
    with lazy sweep.
  - **`test_rls_for_role`** (4.8, new module `mcpg.rls`). Runs a
    SELECT as a target role inside `READ ONLY` + `SET LOCAL ROLE`,
    reports applicable policies, visible row count, and a bounded
    sample. Identifier-validated.
  - **`generate_test_data`** (10.3, new module `mcpg.test_data`).
    Produces synthetic INSERT statements honouring column type,
    NOT NULL, DEFAULT. Deterministic with a seed; covers numeric /
    text / boolean / date / timestamp / json / uuid types.
    Unsupported types (geometry, hstore, vector, ...) listed in
    `skipped_columns`. Does NOT execute — returns SQL for review.

- **NL → SQL helper** (shortlist 10.2). New `mcpg.nl2sql` module
  with a pluggable `LLMProvider` (Anthropic / OpenAI / Gemini)
  speaking each vendor's HTTPS API via `httpx` — no SDK dependency.
  `translate_nl_to_sql(question, schema, execute=False, ...)`
  gathers a compact schema brief (tables, columns, FKs), asks the
  configured model to emit JSON with `sql` + `explanation`, and —
  when `execute=true` — passes the generated SQL through
  `SafeSqlDriver` before running. Writes / DDL / multi-statement
  input rejected even if the model produced them. New settings:
  `MCPG_NL2SQL_PROVIDER` / `MCPG_NL2SQL_API_KEY` (with vendor-env
  fallbacks) / `MCPG_NL2SQL_MODEL` / `MCPG_NL2SQL_BASE_URL` /
  `MCPG_NL2SQL_MAX_TOKENS` (hard cap 16384). API key never appears
  in `repr(Settings)`.

- **Agent cookbook** (`docs/cookbook.md`). Practical recipes for
  common workflows: schema discovery, slow-query diagnosis,
  migration safety, cursor streaming, multi-tenancy, NL→SQL,
  observability scraping, RLS testing, data import / export,
  ORM model emission, TimescaleDB inspection. Linked from the
  README and the docs index.

- Three new agent-UX-focused tools (more Tier-A picks). Tool surface
  81 → 84. All read-only.
  - **`summarize_table`** — one-stop snapshot of a table: columns,
    primary key, foreign keys, every other constraint, indexes,
    storage / row-count / last-vacuum/analyze stats, and an optional
    short row sample. Replaces what would otherwise be 4-5
    individual tool calls. Lives in new module `mcpg.composite`.
  - **`why_is_this_slow`** — one-call diagnosis: runs
    `EXPLAIN (FORMAT JSON)` (does NOT execute the query), walks the
    plan tree, snapshots concurrent active queries and blocking
    lock pairs, reads the cluster-wide cache hit ratio, and
    produces categorised suggestions (plan / contention / cache /
    maintenance). Safe to run on a statement the agent doesn't
    want to materialise yet. Lives in `mcpg.composite`.
  - **`find_unused_objects`** — scans `pg_stat_user_tables` and
    `pg_stat_user_indexes` for tables/indexes with zero scans since
    stats were last reset. Tables also need zero writes (the row
    never moved) to qualify; indexes backing PRIMARY KEY / UNIQUE
    constraints are excluded since PG needs them for enforcement.
    Returns context (scan + write counts, size, definition) so the
    agent can decide whether the object is safe to drop. Documented
    as a SIGNAL not a verdict — recent stats resets produce false
    positives. Lives in `mcpg.advisors` alongside `run_advisors`.

- Three new pgvector tools (Tier-A picks from the feature shortlist).
  Tool surface 78 → 81. All read-only; all extend `mcpg.textsearch`
  alongside the existing `vector_search` / `recommend_vector_index`
  family.
  - **`hybrid_search`** — fuses vector and full-text ranking via
    reciprocal-rank fusion (RRF). Pulls `candidate_pool` candidates
    from each source (vector k-NN on `vector_column`, FTS via
    `websearch_to_tsquery` on `text_column`), then merges them with
    `score = Σ 1/(rrf_k + rank)`. Each match carries `vector_rank`,
    `fts_rank`, the fused `rrf_score`, and the original distance +
    ts_rank values. Tunables: `metric`, `text_config`,
    `candidate_pool` (default 50), `rrf_k` (default 60), `limit`.
    Closes the biggest unmet need in agentic RAG: pure vector
    misses keyword/identifier matches, pure FTS misses semantic
    synonyms.
  - **`vector_range_search`** — finds every row within
    `max_distance` of a query vector (not top-k). Useful for
    de-duplication, similarity gating, clustering pre-passes.
    Results still ordered by distance and capped at `limit` so a
    too-loose threshold cannot pull the whole table.
  - **`recommend_vector_quantization`** — scans a schema for
    `vector(N)` columns whose storage could shrink by switching to
    pgvector v0.7+'s `halfvec(N)` (16-bit float). Returns
    per-column current vs suggested bytes, the savings ratio, and a
    rationale. Skips columns that are already non-`vector` and
    small tables where the absolute saving wouldn't justify the
    migration. Catalog query uses `pg_attribute.atttypmod` + a
    `t.typname IN ('vector','halfvec','sparsevec')` filter so PG's
    built-in `bit(N)` doesn't false-positive.

- Four more catalog → DSL exporters under the same Batch G umbrella.
  Tool surface 74 → 78. All read-only, no new capability or env-var
  gates. Coverage matches the existing exporters (Prisma / Drizzle /
  SQLAlchemy 2.0 / sqlc): base tables, columns, primary keys, single-
  column intra-schema foreign keys, enums. Cross-schema FKs and
  composite FKs are documented v1 gaps.
  - `generate_diesel_schema` — emits a Diesel ORM (Rust) `schema.rs`
    with one `table!` macro per table, `Nullable<T>` wrappers for
    nullable columns, `joinable!` lines for intra-schema FKs, and an
    `allow_tables_to_appear_in_same_query!` macro so multi-table
    joins type-check. Enum types are emitted as Text-backed wrapper
    enums in a `pg_enum` module so the output works without
    `diesel_derive_enum`.
  - `generate_jooq_config` — emits a `jooq-codegen` configuration
    XML pointing at the database. Unlike the other exporters, jOOQ
    generates Java code itself from the live database at build
    time; the artefact here is the XML the user feeds to
    `mvn jooq-codegen:generate`. Includes an explicit `<includes>`
    regex naming every base table, an `<excludes>` covering MCPg's
    bookkeeping schemas, and a `<forcedType>` for every json / jsonb
    column so they map to `org.jooq.JSON` / `org.jooq.JSONB`.
  - `generate_ent_schemas` — emits Ent (Go) Schema struct files,
    one `.go` per table. Each struct lists `field.X(...)` calls,
    `edge.To(...)` lines for single-column FKs, and
    `field.Enum().Values()` for enum-typed columns. Returns a
    `{filename: source}` dict.
  - `generate_ecto_schemas` — emits Ecto (Elixir) schema modules,
    one `.ex` per table named after the singularised table
    (matching the Phoenix `lib/my_app/<singular>.ex` convention).
    Each module uses `use Ecto.Schema`, declares `@primary_key`,
    `field` for each column, `belongs_to` for single-column FKs,
    and `timestamps()` when both `inserted_at` + `updated_at`
    exist. The Elixir top-level module is configurable via the
    `app_module` arg (default `MyApp`).

## [0.4.0] - 2026-05-26

Twenty-nine new MCP tools, closing **Batches D / E / F / G** of the
post-0.3.0 roadmap (`PLAN.md` §11). Brings the total MCP tool surface
from **45 to 74** and ships the long-planned cross-cutting features:
the data-movement family, the LISTEN/NOTIFY bridge, the agent-driven
migration shadow workflow, and three new ORM-DSL exporters
(Drizzle / SQLAlchemy 2.0 / sqlc) alongside the existing Prisma one.

### Headline features

- **Batch D — data movement (5 tools).** `dump_database` /
  `restore_database` round-trip a database through `pg_dump` /
  `psql` / `pg_restore` via the ADR-0004 subprocess gate.
  `copy_table_between_databases` pipes one database's table into
  another in one shell pipeline. `import_csv` /
  `import_json` bulk-load via in-process `COPY ... FROM STDIN`
  and parametrised `executemany` — no subprocess gate needed.
- **Batch E — LISTEN/NOTIFY bridge (4 tools), ADR-0005.**
  `subscribe_channel` / `poll_notifications` /
  `unsubscribe_channel` / `list_notification_subscriptions`
  let an agent react to PostgreSQL events through a polled,
  per-subscription bounded queue. New `Capability.LISTEN` +
  `MCPG_ALLOW_LISTEN` opt-in.
- **Batch F — staged-migration workflow (4 tools), ADR-0006.**
  `prepare_migration` clones a target schema's structure into a
  shadow schema via introspection, applies a candidate SQL there,
  and runs `compare_schemas` so the agent reviews the structural
  delta. `complete_migration` lands it on the target.
  `cancel_migration` / `list_pending_migrations` round out the
  workflow. Same-database shadow (no full-DB clone). New
  `Capability.MIGRATE` reuses the existing `MCPG_ALLOW_DDL` opt-in.
- **Batch G — catalog → DSL exporters (3 new tools).**
  `generate_drizzle_schema` (Drizzle ORM TypeScript),
  `generate_sqlalchemy_models` (SQLAlchemy 2.0 declarative Python),
  `generate_sqlc_schema` (replayable plain DDL for sqlc). All
  read-only — drop into any agentic project as a starting point.

### Fixed

- PR #17 code-review findings (10 fixes across the Batches D / E / F / G
  surfaces):
  1. `restore_database` for custom/tar formats now passes
     `--dbname=postgresql:///` so pg_restore actually connects (it
     previously fell into "convert to SQL script" mode without `-d`).
  2. `ListenManager` recovers from a dead listener connection — the
     reader-loop clears `_conn` and sets `_needs_resubscribe`, the next
     subscribe opens a fresh conn and re-issues LISTEN for every active
     channel (previously the manager silently stopped delivering after
     any PG restart).
  3. Migration DDL replay only rewrites schema references on
     `foreign_key` constraints, not on every constraint type — a CHECK
     constraint whose literal happens to contain the target schema
     name (e.g. `CHECK (path LIKE 'public.%')`) is no longer corrupted.
  4. `mcpg.sqlc` enum labels are now apostrophe-escaped (PG-standard
     `''` doubling) so labels like `O'Brien` don't break the DDL.
  5. `mcpg.sqlalchemy_export` enum generator falls back to the
     functional `enum.Enum("Name", {...})` form when any label isn't a
     valid Python identifier (`in-progress`, `1st`, `class`, ...),
     keeping the generated file importable.
  6. `mcpg.drizzle` default rendering now translates PG escape rules to
     JS escape rules in the right order: `''` → `'`, backslash → `\\`,
     `"` → `\"`. Previously `'it''s'` became `"it''s"` and `'a\nb'`
     silently injected a newline.
  7. Shadow schema names are capped to fit PostgreSQL's 63-byte
     NAMEDATALEN limit, preventing silent truncation that would leak
     shadow schemas the workflow couldn't clean up.
  8. The migration shadow-workflow now refuses candidate SQL containing
     statements PG won't run inside a transaction block (CREATE INDEX
     CONCURRENTLY, VACUUM, ALTER SYSTEM, ...) with a clear error
     pointing the user at `run_ddl` instead.
  9. `mcpg.shell._write_stdin` always closes the child's stdin in a
     `finally` block — a non-`BrokenPipeError` from `write`/`drain`
     no longer leaks the pipe and wedges the child.
  10. `ListenManager.close()` bounds the `conn.close()` await at 2s so
      a libpq close hanging on a half-open socket can't wedge server
      shutdown.

- PR #17 code-review findings (10 fixes across the Batches D / E / F / G
  surfaces):
  1. `restore_database` for custom/tar formats now passes
     `--dbname=postgresql:///` so pg_restore actually connects (it
     previously fell into "convert to SQL script" mode without `-d`).
  2. `ListenManager` recovers from a dead listener connection — the
     reader-loop clears `_conn` and sets `_needs_resubscribe`, the next
     subscribe opens a fresh conn and re-issues LISTEN for every active
     channel (previously the manager silently stopped delivering after
     any PG restart).
  3. Migration DDL replay only rewrites schema references on
     `foreign_key` constraints, not on every constraint type — a CHECK
     constraint whose literal happens to contain the target schema
     name (e.g. `CHECK (path LIKE 'public.%')`) is no longer corrupted.
  4. `mcpg.sqlc` enum labels are now apostrophe-escaped (PG-standard
     `''` doubling) so labels like `O'Brien` don't break the DDL.
  5. `mcpg.sqlalchemy_export` enum generator falls back to the
     functional `enum.Enum("Name", {...})` form when any label isn't a
     valid Python identifier (`in-progress`, `1st`, `class`, ...),
     keeping the generated file importable.
  6. `mcpg.drizzle` default rendering now translates PG escape rules to
     JS escape rules in the right order: `''` → `'`, backslash → `\\`,
     `"` → `\"`. Previously `'it''s'` became `"it''s"` and `'a\nb'`
     silently injected a newline.
  7. Shadow schema names are capped to fit PostgreSQL's 63-byte
     NAMEDATALEN limit, preventing silent truncation that would leak
     shadow schemas the workflow couldn't clean up.
  8. The migration shadow-workflow now refuses candidate SQL containing
     statements PG won't run inside a transaction block (CREATE INDEX
     CONCURRENTLY, VACUUM, ALTER SYSTEM, ...) with a clear error
     pointing the user at `run_ddl` instead.
  9. `mcpg.shell._write_stdin` always closes the child's stdin in a
     `finally` block — a non-`BrokenPipeError` from `write`/`drain`
     no longer leaks the pipe and wedges the child.
  10. `ListenManager.close()` bounds the `conn.close()` await at 2s so
      a libpq close hanging on a half-open socket can't wedge server
      shutdown.

### Added

- ORM-bridge exporters — Batch G follow-ons (Phase 28b/c/d). Three
  new MCP tools sit alongside the existing `generate_prisma_schema`
  under the schema→DSL umbrella:
  - `generate_drizzle_schema` — emit a Drizzle ORM TypeScript schema
    (`drizzle-orm/pg-core`) covering tables, columns with PG-native
    types (incl. `serial`/`bigserial` from `nextval` defaults, length
    on varchar, `withTimezone` on timestamptz), single-column FKs as
    column-level `.references(() => ...)`, primary/unique/check
    constraints, indexes, defaults, and enums via `pgEnum`. The
    helper-import line is computed from what was actually emitted, so
    unused helpers don't clutter the output.
  - `generate_sqlalchemy_models` — emit a SQLAlchemy 2.0 declarative
    models file (`DeclarativeBase` + `Mapped[T]` + `mapped_column`)
    with PG types from both `sqlalchemy` core and
    `sqlalchemy.dialects.postgresql` (jsonb), single-column FKs via
    `ForeignKey("schema.table.col")`, composite uniques in
    `__table_args__`, enum types emitted as Python `enum.Enum`
    classes, and `server_default=text(...)` / `func.now()` for
    defaults. Composite FKs are a documented v1 gap.
  - `generate_sqlc_schema` — emit a sqlc-friendly `schema.sql` (plain
    DDL) ordered for clean replay: `CREATE SCHEMA` → `CREATE TYPE`
    enums → `CREATE TABLE` (columns only) → `ALTER TABLE ADD
    CONSTRAINT` (PK / unique / check / FK in that order) → `CREATE
    INDEX` for non-constraint indexes. In-process — no
    `MCPG_ALLOW_SHELL` needed.
  All three are read-only; gated by the standard READ capability.

- Staged-migration workflow — Batch F (Phase 27), per ADR-0006. New
  `mcpg.migrations` module implements Neon-style "branch the schema,
  test the migration, merge" with same-database shadow schemas (no
  `pg_dump` shell-out, no cross-batch dependency on Batch D). Four
  new MCP tools:
  - `prepare_migration(name, target_schema, candidate_sql,
    ttl_minutes=60)` clones the target schema's structure into a
    fresh `mcpg_shadow_<id>` schema via introspection-driven DDL
    replay (tables + columns, PK / UNIQUE / CHECK / FK constraints,
    indexes), applies `candidate_sql` against the shadow with
    `SET LOCAL search_path` so unqualified identifiers resolve there,
    runs `compare_schemas(target, shadow)`, and persists the staged
    row in `mcpg_migrations.staged`. Returns the migration id +
    shadow schema name + TTL + structural diff for review.
  - `complete_migration(id)` applies the candidate SQL to the
    target schema and drops the shadow. Refuses if status is not
    `prepared` or TTL has expired.
  - `cancel_migration(id)` drops the shadow and marks the row
    `cancelled`. Idempotent.
  - `list_pending_migrations()` lists prepared migrations newest
    first; sweeps any expired prepared rows before listing.
  Intra-schema FK references are rewritten to point at the shadow;
  cross-schema FKs are left pointing at the original and surface in
  the diff as removed (documented limitation per ADR-0006).
- New `Capability.MIGRATE` enum entry; the migration tools register
  under unrestricted mode + the existing `MCPG_ALLOW_DDL` opt-in
  (the underlying ops are DDL).
- New `mcpg_migrations` schema + `staged` table created idempotently
  on first migration call. State columns: `id`, `prepared_at`,
  `target_schema`, `shadow_schema`, `candidate_sql`, `status`
  (`prepared` / `completed` / `cancelled` / `expired`),
  `ttl_expires_at`, `completed_at`.

- LISTEN/NOTIFY bridge — Batch E first slice, per ADR-0005. New
  `mcpg.listen` module owns the server-lifetime subscription state.
  Four new MCP tools:
  - `subscribe_channel(channel)` opens a PostgreSQL `LISTEN` on the
    given channel (validated against the standard plain-identifier
    allowlist) and returns a subscription id. Notifications buffer
    in a per-subscription bounded queue.
  - `poll_notifications(subscription_id, timeout_ms, max_messages)`
    drains up to `max_messages` from the queue, waiting at most
    `timeout_ms` for the first one when the queue is empty. Each
    `{channel, payload, delivered_at, dropped_count}` notification
    surfaces drop count only on the first message after an overflow
    so the caller is informed exactly once.
  - `unsubscribe_channel(subscription_id)` removes a subscription;
    `UNLISTEN` fires when the last subscription on a channel is gone.
  - `list_notification_subscriptions()` reports the active
    `{subscription_id, channel}` pairs for visibility.
  A single dedicated PostgreSQL connection (separate from the request
  pool) holds every active LISTEN, opened lazily on first subscribe.
  A background `asyncio.Task` drains psycopg's notifies generator
  with a short polling timeout so subscribe/unsubscribe `execute()`
  calls can land between iterations (the psycopg connection lock
  would otherwise deadlock concurrent admin commands). Queue overflow
  drops the oldest message and surfaces `dropped_count` on the next
  poll.
- New `Capability.LISTEN` enum entry. Two new env vars:
  `MCPG_ALLOW_LISTEN` (bool, default `false`) toggling the
  subscription tool surface; `MCPG_LISTEN_QUEUE_MAX` (default 1000)
  capping per-subscription buffer size.
- `AppContext.listen_manager` exposes the manager to every tool;
  `create_server` accepts an optional `listen_manager` keyword arg so
  tests can inject a fake connection factory.

- `copy_table_between_databases` tool — copy a single table from one
  database to another by piping `pg_dump --format=custom --table=...`
  (source) into `pg_restore --format=custom --single-transaction
  --exit-on-error` (destination). Both legs run through the ADR-0004
  shell runner with separate libpq env dicts derived from the source
  and destination URLs; credentials never appear on argv. `include_schema`
  and `include_data` flags are required (no implicit default) so the
  caller can't accidentally copy the wrong half. If the captured
  pg_dump archive exceeds `MCPG_SHELL_MAX_OUTPUT_BYTES`, the tool
  raises before invoking pg_restore — a truncated custom-format archive
  would either fail obscurely or partially restore. A failed pg_dump
  short-circuits the same way, returning the dump stderr_tail with
  `restore_exit_code=-1` as a sentinel. Gated under unrestricted mode
  + `MCPG_ALLOW_SHELL`.

- `import_csv` tool — bulk-load CSV content into `schema.table` via
  `COPY ... FROM STDIN`. CSV text is sent verbatim; `header` toggles
  header-row skipping; optional `columns` restricts loading to named
  columns (each validated against the plain-identifier allowlist).
  Delimiter is restricted to a single non-newline, non-quote character
  so it cannot terminate the COPY options list early. Returns the
  server-reported row count. Gated under unrestricted mode (WRITE
  capability) — no subprocess, no `MCPG_ALLOW_SHELL` needed.
- `import_json` tool — bulk-load a JSON array of objects into
  `schema.table` via parametrised `INSERT ... executemany`. Columns
  are derived from the first row's keys (or supplied explicitly);
  nested `dict`/`list` values are JSON-serialised so they round-trip
  into `jsonb` columns; missing keys in later rows bind as `NULL`.
  Values are bound — never spliced into SQL — so they cannot inject
  statements. Gated under unrestricted mode (WRITE capability).
- `Database.copy_from_stdin` and `Database.execute_many` helpers —
  in-process plumbing for COPY FROM STDIN and `executemany`, used by
  the new import tools. The vendored `SqlDriver` exposes neither, so
  imports go through the `Database` wrapper for raw connection access.

- `restore_database` tool — restore a dump into the connected database
  via the ADR-0004 subprocess gate. `format='plain'` pipes SQL text
  through `psql --single-transaction --set=ON_ERROR_STOP=on` so a
  syntax error rolls back the whole restore; `format='custom'`/`'tar'`
  base64-decode the payload and pipe the binary archive into
  `pg_restore --single-transaction --exit-on-error`. Credentials reach
  the binary via libpq env vars; the dump bytes flow through stdin and
  are never interpolated into argv. Gated on unrestricted mode +
  `MCPG_ALLOW_SHELL`.

### Fixed

- `mcpg.shell.run_pg_binary` now writes the optional `stdin` payload
  concurrently with the stdout/stderr drain. The previous "write
  stdin after wait()" ordering would have deadlocked any subprocess
  that consumes stdin (`pg_restore`, `psql -f -`); no shipped tool
  used stdin yet, but the bug blocked `restore_database` from working.

- `dump_database` tool — wraps `pg_dump` to capture the connected
  database's schema (and optionally data) as a plain-SQL string or
  base64-encoded binary archive. Implements the ADR-0004 subprocess
  policy: argv-only invocation, allowlisted binaries, hard timeout,
  output cap with truncation flag, credentials passed via libpq env
  vars (never on the command line). Gated behind a new
  `Capability.SHELL` + `MCPG_ALLOW_SHELL` opt-in on top of
  unrestricted access mode.
- New `MCPG_ALLOW_SHELL` env var (bool, default `false`) toggling the
  whole subprocess-tool surface. Two companion knobs:
  `MCPG_SHELL_TIMEOUT_SEC` (default 60) and `MCPG_SHELL_MAX_OUTPUT_BYTES`
  (default 64 MiB).
- `Capability.SHELL` added to the policy table; required for any tool
  that invokes an external binary.
- `export_query` tool — run a read-only SQL query and serialise the
  rows to CSV or JSON. Reuses the safety checks of `run_select` and
  truncates at the supplied row limit with a `truncated` flag in the
  result so callers can paginate.
- `export_table` tool — serialise every row in a `schema.table` (up
  to the supplied limit) to CSV or JSON. Identifier names must match
  the plain SQL allowlist; anything that needs delimited-identifier
  quoting is rejected.
- `list_audit_events` tool — read recent rows from `mcpg_audit.events`
  (newest first). Returns an empty list when `MCPG_AUDIT_PERSIST` has
  never been turned on (no audit table yet). Optional tool-name filter.
- New `MCPG_AUDIT_PERSIST` env var (bool, default `false`). When on,
  every `run_write` / `run_ddl` call appends one row to
  `mcpg_audit.events` containing redacted arguments, status, error, and
  result. Persistence failures are swallowed so audit logging never
  masks the real write outcome.
- `run_ddl` gains optional `schema` / `table` hints. When both are
  supplied, the call snapshots the table's columns before and after the
  DDL and attaches the structured before/after lists to the result as a
  `SchemaDiffSnapshot`. The snapshot is also stored in the persisted
  audit row when `MCPG_AUDIT_PERSIST` is on.
- PostgreSQL 18 added to the CI test matrix (was 14–17; now 14–18). The
  integration suite runs against every supported version on every PR.
- `run_advisors` tool — runs a set of codified, catalog-driven lint
  rules against a schema and returns a typed report of findings. First
  cut covers: `missing_primary_key`, `unindexed_foreign_key` (leading-
  column heuristic), `duplicate_indexes` (same column-keys + access
  method), and `nullable_timestamp_without_tz`. Each finding carries a
  rule id, severity (`warning`/`info`), a qualified object name, and a
  human-readable message. Advisory only — no writes.
- `generate_prisma_schema` tool — read a PostgreSQL schema and emit a
  valid Prisma `.prisma` schema string, mirroring `prisma db pull` but
  driven by MCPg. Covers tables, columns, primary/foreign keys
  (including composite), unique constraints, secondary indexes, and
  enums; standard defaults (`nextval(...)` → `autoincrement()`, `now()`
  → `now()`, `gen_random_uuid()` → `uuid()`, literals) and array types
  are mapped; unmappable types (vectors, custom domains) fall back to
  `Unsupported("...")` exactly like `prisma db pull`. Views, foreign
  tables, partitions, triggers, functions, policies, and composite
  types are out of scope for v1. **First USP-tier tool — no other PG
  MCP server bridges to an ORM schema DSL.**
- `tune_vector_index` tool — recommends an `ivfflat` or `hnsw`
  configuration for a pgvector column. Reads the live row count
  (`pg_class.reltuples`) and column dimension, applies the standard
  pgvector heuristics (lists ≈ rows/1000 or sqrt for ivfflat; m
  scales with size, ef_construction with size for hnsw), and returns
  the parameters plus a ready-to-run `CREATE INDEX` statement.
- `vector_recall_at_k` tool — measures recall@k of an existing
  pgvector index by comparing its top-k results against a brute-force
  ground truth for the same query vectors. Uses pgvector's distance
  functions (`l2_distance` / `cosine_distance` / `inner_product`) as
  the non-indexed baseline; the operator form (`<->`, `<=>`, `<#>`)
  triggers the ANN index.
- `list_cron_jobs` tool — read pg_cron's `cron.job` catalog. Returns an
  empty list when pg_cron is not installed (graceful degradation).
- `schedule_cron_job` and `unschedule_cron_job` tools (write-gated) —
  thin wrappers over `cron.schedule()` / `cron.unschedule()`. Raise
  `CronError` when pg_cron is not installed.
- `partman_create_parent`, `partman_run_maintenance`,
  `partman_drop_partition` tools (write-gated) — pg_partman
  partition-set creation, periodic maintenance (forward partitions +
  retention drops), and explicit retention-based drops (time- or
  id-controlled). `partition_type` is allowlisted to
  range/list/native. Raise `PartmanError` when pg_partman is not
  installed.
- `pg_cron` and `pg_partman` added to `ENABLEABLE_EXTENSIONS` — agents
  can request enabling them (still gated on unrestricted mode +
  `MCPG_ALLOW_DDL`; pg_cron also requires server-side
  `shared_preload_libraries`).

## [0.3.0] - 2026-05-23

Twelve new MCP tools, closing Batch A of the post-0.2.0 roadmap
(`PLAN.md` §11): catalog completeness (Phase 16), schema visualisation
(Phase 17), and structural schema diff (Phase 18). Brings the total
MCP tool surface from 33 to 45 and lays the structural foundation for
Phase 27 shadow migrations.

### Added

- `list_foreign_keys` tool — every foreign key in a schema, resolved to
  its from-columns, referenced schema, referenced table, and
  to-columns. The two column arrays are aligned by ordinal position.
- `generate_schema_diagram` tool — renders a Mermaid ER diagram for a
  schema (entities with PK/FK column markers, edges parent → child).
  Views and foreign tables are excluded; partitions are excluded by
  default and can be included with ``include_partitions=true``.
- `compare_schemas` tool — structural diff between two schemas. Reports
  tables / columns / indexes / constraints / foreign keys as added,
  removed, or changed; column changes include the list of differing
  ColumnInfo fields. Object identity is by name; renames surface as a
  paired add + remove. Foundation for the Phase-27 shadow-migration
  workflow.
- `list_constraints` tool — a table's primary-key, foreign-key, unique,
  check, and exclusion constraints.
- `list_views` tool — the views and materialized views in a schema, with
  their definitions.
- `list_functions` tool — the functions and procedures in a schema, with
  kind, arguments, return type, and language.
- `list_triggers` tool — the user-defined triggers on a table.
- `list_sequences` tool — the sequences in a schema, with each sequence's
  data type, range, increment, cycle flag, and last value.
- `list_partitions` tool — how a table is partitioned (range, list, or
  hash) and its partitions, each with its bound expression.
- `list_policies` tool — the Row-Level-Security policies on a table, with
  each policy's command, permissive flag, roles, and predicates, plus
  whether row security is enabled on the table.
- `list_roles` tool — the database roles and their attributes (superuser,
  create-role/db, login, replication, bypass-RLS, connection limit, and
  role membership).
- `list_grants` tool — the privileges granted on a table, with each
  grant's grantee, privilege, grantable flag, and grantor.
- `list_active_queries` tool — the queries currently running on the
  server, each with its wait event, duration, and blocking PIDs.
- `check_database_health` gains two checks — replication lag (how far
  connected standbys trail) and table bloat (tables far larger than their
  estimated minimum size).
- `run_maintenance` tool — runs `VACUUM` or `ANALYZE` against one table;
  requires unrestricted mode. Runs on an autocommit connection, since
  `VACUUM` cannot run inside a transaction.
- `cancel_query` and `terminate_backend` tools — signal a backend PID to
  cancel its current query or close its connection; require unrestricted
  mode.
- `list_enums`, `list_domains`, `list_composite_types` tools — the
  user-defined types in a schema. Composite types report each attribute
  with its rendered type; the catalog's implicit table row-types are
  excluded.
- `list_foreign_data_wrappers`, `list_foreign_servers`,
  `list_foreign_tables`, `list_user_mappings` tools — the FDW catalog,
  with each entry's options array parsed into a typed dict.
- `list_publications` and `list_subscriptions` tools — read-only view of
  logical-replication publications (with the tables and operations they
  cover) and subscriptions; reading subscriptions requires superuser, by
  PostgreSQL design.
- `postgres_fdw` added to `ENABLEABLE_EXTENSIONS` — agents can now
  enable the wrapper they can already introspect (gated on unrestricted
  mode + `MCPG_ALLOW_DDL`).

### Changed

- `list_tables` now flags each table with `partitioned` (a partitioned
  parent) and `is_partition` (itself a partition).
- `list_indexes` now flags each index with `partitioned` (a
  partitioned-index template).
- `recommend_indexes` now rolls a flagged partition up to its partitioned
  parent — summing scan and row counts and setting a `partitioned` flag —
  since an index created on the parent propagates to every partition.
- The "every introspection tool is callable" check moved from the unit
  suite (fakes-only) to the integration suite — it now runs against the
  real catalog across the PG 14–17 CI matrix, closing a trust gap the
  unit-level fake driver couldn't reach.

## [0.2.0] - 2026-05-21

Extension support: index-method intelligence, extension management, and
similarity-search tools (trigram, full-text, pgvector, PostGIS) — six new
tools, each degrading gracefully when its extension is absent.

### Added

- `list_available_extensions` tool — lists every extension available to the
  database with its installed-vs-available status.
- `enable_extension` tool — enables an allowlisted PostgreSQL extension;
  requires unrestricted mode and `MCPG_ALLOW_DDL`.
- `fuzzy_search` tool — ranks a text column by `pg_trgm` trigram similarity
  to a search term, with a `word` mode (fragment matching, the default) and
  a `full` mode (whole-string comparison).
- `full_text_search` tool — ranks documents with PostgreSQL's built-in
  `tsvector`/`tsquery` full-text search.
- `vector_search` tool — finds the rows nearest to a query vector by
  `pgvector` distance (`l2`, `cosine`, or `inner_product`).
- `geo_search` tool — finds the rows nearest to a lon/lat point by PostGIS
  distance.

### Changed

- `list_indexes` now reports each index's access method (`btree`, `gin`,
  `gist`, `brin`, `hash`, `spgist`).
- `recommend_indexes` now suggests per-column index types from column data
  types — GIN for `jsonb`/array columns, trigram GIN for text columns.
- `describe_table` now reads the catalog directly and reports the
  `pgvector` dimension for `vector(N)` columns.
- Documentation reorganised into living guides: `docs/installation.md`,
  `docs/user-guide.md`, and `docs/architecture.md` (replacing `docs/usage.md`).

## [0.1.0] - 2026-05-21

First release: a production-grade PostgreSQL MCP server with 14 tools across
introspection, querying, writes, and tuning — read-only by default, every
statement validated, every tool call audited.

### Added

- Project plan, phased roadmap, and session-resume protocol (`PLAN.md`,
  `docs/PROGRESS.md`).
- ADR-0001 (build approach: hard-fork) and ADR-0002 (technology stack).
- Vendored the self-contained `sql/` SQL-safety kernel from
  `crystaldba/postgres-mcp` @ `07eb329` (MIT) into `src/mcpg/_vendor/sql/`,
  with the upstream unit tests that port cleanly.
- Project scaffold: `pyproject.toml`, packaging, `ruff`/`mypy`/`pytest`/
  coverage configuration, `NOTICE`.
- GitHub Actions CI (`.github/workflows/ci.yml`): lint, format, type-check,
  and test jobs.
- `CONTRIBUTING.md`, local `pre-commit` hooks, and GitHub issue/PR templates.
- Env-driven configuration (`mcpg.config`): `Settings`, `AccessMode`,
  `Transport`, and `load_settings`. Read-only is the default access mode and
  the settings repr redacts database credentials.
- Database connection lifecycle (`mcpg.database`): `Database` wraps the pool
  with connect/close, async-context-manager support, and a typed
  `DatabaseError`.
- MCP server bootstrap (`mcpg.server`): `create_server` builds a configured
  `FastMCP` whose lifespan owns the settings and database (no global state);
  `run` serves over the stdio, streamable-HTTP, or SSE transport.
- First MCP tool, `get_server_info` (`mcpg.tools`): reports the server
  version, access mode, transport, and database connection status.
- Console entry point: `mcpg` (and `python -m mcpg`) loads configuration
  and runs the server.
- CI now enforces the test-coverage gate (90% of authored code).
- Integration-test harness (`tests/integration/`) running against a live
  PostgreSQL; CI exercises the suite against PostgreSQL 14, 15, 16, and 17.
- Schema-introspection tools (`mcpg.introspection`): `list_schemas`,
  `list_tables`, `describe_table`, `list_indexes`, and `list_extensions`,
  using parameterised read-only catalog queries.
- Safe query execution (`mcpg.query`): the `run_select` tool validates
  agent-supplied SQL against an allowlist and runs it read-only, returning a
  typed result; unsafe statements are rejected.
- The `explain_query` tool returns a query's `EXPLAIN (FORMAT JSON)`
  execution plan without running the query.
- `run_select` caps results at a configurable `max_rows` (default 1000) and
  reports whether the result was `truncated`.
- Access-mode policy engine (`mcpg.policy`): tool registration is gated by
  capability, so the available tools depend on the configured access mode.
- Adversarial SQL-safety regression suite covering statement stacking,
  comment and transaction-control escapes, DDL/DML, `COPY`, and `DO` blocks.
- Audit logging (`mcpg.audit`): every tool invocation is logged to the
  `mcpg.audit` logger with its outcome and arguments, with secrets masked.
- Security documentation (`docs/security.md`): threat model, trust
  boundaries, mitigations, and operator responsibilities.
- Write execution (`mcpg.write`): the `run_write` tool executes a single
  validated INSERT/UPDATE/DELETE statement, available only in unrestricted
  access mode; statement stacking is rejected.
- The `run_ddl` tool executes a single validated DDL statement; it requires
  unrestricted access mode and the `MCPG_ALLOW_DDL` opt-in.
- Database health checks (`mcpg.health`): the `check_database_health` tool
  reports connection utilisation, buffer cache hit ratio, tables needing
  vacuum, and invalid indexes.
- Workload analysis (`mcpg.workload`): the `analyze_workload` tool reports
  the slowest queries via `pg_stat_statements`, degrading gracefully when
  the extension is not installed.
- Index recommendations (`mcpg.indexing`): the `recommend_indexes` tool
  flags large tables read mostly by sequential scan.
- Query plan analysis (`mcpg.query`): the `analyze_query_plan` tool
  summarises a query's execution plan — total cost, estimated rows, node
  types, and sequentially-scanned tables.
- Configurable connection-pool sizing via `MCPG_POOL_MIN_SIZE` and
  `MCPG_POOL_MAX_SIZE` (defaults 1 and 5).
- Multi-tenancy / Row-Level Security guidance in `docs/security.md`.
- Scaling documentation (`docs/scaling.md`) and a benchmark harness
  (`benchmarks/bench.py`).
- Usage guide (`docs/usage.md`), tool reference (`docs/tools.md`), and a
  `uv`-based `Dockerfile`.
