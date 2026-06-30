# MCPg feature roadmap

Forward-looking candidates for new feature work, grouped by
area. Each entry shows rough effort (**S** / **M** / **L**),
user-facing value, and any prerequisite. Use this as a menu for
prioritisation.

Items that are already on `main` are deliberately **not** listed
here — for what shipped when, see
[`../CHANGELOG.md`](../CHANGELOG.md). For the live status of
security-hardening items specifically, see
[`security-hardening.md`](security-hardening.md).

> **Observation loop.** Gaps surfaced during PR reviews / Phase
> retrospectives land here as their own numbered row (under the
> section that best matches the area) with the date and source of
> the observation in the Notes column. New observations always
> get a row — we never lose a gap to a chat thread. Pick items
> off the list deliberately; mark with ✅ Shipped when the PR
> merges.

Effort scale (rough, single-session yardstick):

- **S** — 1 module, 1 PR, ≤ 1 day equivalent
- **M** — 2–3 modules or wider surface, 1–3 PRs
- **L** — new infrastructure (background workers, transport
  changes, cross-cutting refactors)

---

## 1. Observability

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 1.1 | ✅ **Shipped.** OpenTelemetry tracing — one span per `call_tool` invocation behind the `mcpg[otel]` extra. Spans live on the `mcpg.tools` tracer and carry `mcp.tool.name`, `mcp.tool.argument_count`, `mcp.tool.status`, plus `error.type` / `error.message` on failure. Raw argument values are deliberately not attached (PII / secrets). Standard `OTEL_*` env vars (endpoint, headers, sampler) take precedence; `MCPG_OTEL_SERVICE_NAME` is the only project-specific knob. Lives in `mcpg.otel_tracing`. | M | Medium-High | One span per `call_tool` + child spans for the actual query / subprocess. |
| 1.2 | ✅ **Shipped.** Structured JSON logging output toggle — `MCPG_LOG_FORMAT={text,json}` env var (validated at boot). When `json`, every `mcpg`-namespaced log record renders as a single-line JSON object via `JSONFormatter` in `mcpg.obs_logging`: RFC 3339 UTC `timestamp` (ms precision), `level`, `logger`, `message`, optional `exception`, plus — for audit records — every key from the `mcpg.audit` payload merged onto the top level so ELK / Datadog / Loki ingest without extra parsing. Default stays `text` for the existing operator-friendly stderr stream. | S | Medium | Wraps the existing `mcpg.audit` logger; coverage in `tests/unit/test_obs_logging.py` + `tests/unit/test_audit.py`. |
| 1.3 | ✅ **Shipped.** Slow-call logging from the MCP layer | S | Low | Per-tool latency log to flag slow MCPg-side calls (the existing `analyze_workload` covers PG-side timings). |
| 1.4 | ✅ **Shipped.** **Tool-bucket usage telemetry.** Every `call_tool` now carries the capability-bucket label on both the OpenTelemetry span (`mcp.tool.bucket` attribute) and the Prometheus counter (`mcpg_tool_calls_total{tool, bucket, status}`). Operators can aggregate by capability with `sum by (bucket) (rate(mcpg_tool_calls_total[5m]))` in PromQL or with the same field in any OTLP backend. Pre-this-change every bucket order in `describe_self` was hand-curated; with this telemetry the next iteration can drive the `headline_tools` selection empirically. Lookup uses the existing `mcpg.about.classify_tool` routing; `unknown` defaults keep label cardinality stable. | S | Medium | Realised alongside the 8.6 sweep. |

## 2. PostgreSQL feature coverage

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 2.1 | ✅ **Shipped (bundle).** **Logical replication management writes.** All four DDL-gated tools landed in a single bundle (`mcpg.logical_replication`): `create_publication(name, all_tables=False, tables=())` with FOR ALL TABLES / FOR TABLE rendering and per-piece identifier validation; `drop_publication(name, if_exists, cascade)`; `create_subscription(name, connection_string, publications, enabled, copy_data, create_slot, slot_name, synchronous_commit)` with full WITH-clause grammar — the libpq DSN is single-quote-doubled and the result's `__repr__` redacts the `CONNECTION '…'` literal so credentials don't leak into logs; `drop_subscription(name, if_exists)`. All four require `MCPG_ALLOW_DDL=true`, raise typed `LogicalReplicationError` on validation / driver failure, and clear the read cache on success so `list_publications` / `list_subscriptions` see the new state immediately. | M | Medium-High | Read tools already exist. Closes the loop on logical-replication ops; gated under `MCPG_ALLOW_DDL`. |
| 2.2 | ✅ **Shipped.** `pg_buffercache` integration (cache hit analysis at the buffer level) | S | Low-Medium | Niche. |
| 2.3 | ✅ **Shipped.** WAL inspection (`pg_walinspect`) | S | Low | Niche but useful for replication debugging. |
| 2.4 | ✅ **Shipped.** Deeper `pg_locks` walker — deadlock-cycle reconstruction beyond the current `find_blocking_chains` pair list | S-M | Medium | Live-ops complement. |
| 2.5 | ✅ **Shipped (full batch).** **PG 19 PR-10 — small-tools batch.** All five sub-items landed: ✅ #19 `effective_wal_level` exposure in `get_server_info`; ✅ #14 `read_autovacuum_priority`; ✅ #13 password-expiration + MD5-deprecation warnings in `audit_database`; ✅ #21 `pg_get_acl()` in `list_grants` with PG ≤ 18 fallback; ✅ #18 NL→SQL emission patterns (`GROUP BY ALL` advertised in the system prompt when the server reports ≥ 19.0). | M | Medium | Five sub-items, each landed in its own PR. PG ≤ 18 paths preserved via probe-driven fallback throughout. |
| 2.6 | ✅ **Shipped.** `explain_query` / `analyze_query_plan` gain `io=True` — switches from `EXPLAIN (FORMAT JSON)` (plan only) to `EXPLAIN (ANALYZE, BUFFERS, TIMING)` (executes + reports buffer / IO timing per node, with PG 19 asynchronous-I/O block-count rollups). `QueryPlanAnalysis` extended with 8 optional rollup fields; AIO fields stay `None` on PG ≤ 18 to distinguish "no observations" from "zero". Pre-flight validates the inner SQL via the safety allowlist so writes / DDL are still rejected even with `io=True`. Closes the AIO observability loop alongside the already-shipped `recommend_io_method`. | S | Medium | Pairs with the already-shipped `recommend_io_method` for closing the AIO observability loop. |

## 3. Developer experience

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 3.1 | ✅ **Shipped (first wave).** Auto-generated tool examples in MCP tool descriptions — the `_with_example(description, example)` helper in `mcpg.tools` wraps every wrapped description with a canonical pseudo-Python invocation hint (``Example: `tool(arg=value)```). ~25 high-traffic tools across introspection / query / composite / health / search / diagrams / schema-diff / vector analytics / migrations / data movement now ship examples. The helper is the contract for new tools. | S | Low-Medium | Helps agents pick the right tool. |
| 3.2 | ✅ **Shipped.** Sample-data generator that writes (`seed_table_with_sample_data`) | M | Medium | Sibling of the current `generate_test_data` (synthetic INSERT statements; does not execute). Gated under WRITE. |
| 3.3 | ✅ **Shipped.** Benchmark harness — `scripts/benchmark_pg19.py` + `scripts/benchmark_pg19.sh` launcher. Quantifies three measurable-without-restart PG 19 wins: skip-scan vs dedicated single-column index, REPACK CONCURRENTLY vs VACUUM FULL, LZ4 vs pglz TOAST. AIO `io_uring` vs `worker` lives as a manual recipe in `pg19-operations-playbook.md` because `io_method` is `PGC_POSTMASTER` and the smoke loop deliberately stays in one container. Server-side timing (`EXPLAIN ANALYZE` + `clock_timestamp()`) so the numbers don't include psycopg / network noise. | M | Medium-High | Maps to the GA-day-0 verification milestone in `pg19-readiness.md`. |
| 3.4 | ✅ **Shipped (bundle).** **PG 19 PR-11 — characterisation tests.** `tests/contract/test_pg19_sql_characterisation.py` feeds every SQL string MCPg's PG 19 modules emit through `pglast.parse_sql` (libpg_query bindings). Two catalogues — `_PARSE_OK_CATALOGUE` (catalogue SELECTs + version probes that must parse on the pinned pglast) and `_PARSE_FAIL_CATALOGUE` (PG 19-only grammar pinned to the exact token pglast 7.x trips on, so the test flips when pglast picks up PG 19's parser source). A coverage guard ensures every PG 19 module gets at least one entry. `read_pg_stat_lock` and `read_pg_stat_recovery` now select `stats_reset::text` and surface it on the result dataclasses with a `None` default — closes audit row #16. `docs/postgres-fdw-pushdown.md` characterises how PG 19 widens postgres_fdw pushdown and the operator-managed setup recipe MCPg supports — closes audit row #20. | M | Medium | Bundles audit rows #16, #17, #20. Catches drift between the assumed PG 19 grammar and the real one. |

## 4. Security & compliance

The full security-hardening roadmap (HTTP request limits, security
headers, CORS allowlist, audit-log HMAC integrity chain, pluggable
secrets backend, subprocess hardening, graceful shutdown) lives in
[`security-hardening.md`](security-hardening.md). Forward items
not covered there:

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 4.1 | ✅ **Shipped.** Connection-encryption verification tool (`verify_connection_encryption`) — reports `ssl` + protocol/cipher/bits for MCPg's own link plus a cluster-wide encrypted/unencrypted backend tally, from `pg_stat_ssl`. | S | Medium | Composes with the existing TLS-enforcement startup check. |
| 4.2 | ✅ **Shipped.** Audit-log retention via `prune_audit_events(older_than_days)` — deletes old `mcpg_audit.events` rows (cron-friendly). Refuses when `MCPG_AUDIT_INTEGRITY` is on, since pruning would break the HMAC chain. | S | Medium | — |
| 4.3 | ✅ **Shipped.** IP allowlist for HTTP transport (`MCPG_HTTP_IP_ALLOWLIST`) — comma-separated IP / CIDR list, validated at boot. Tiny ASGI middleware sits at the outermost layer so denied clients never reach the auth / size-limit stack. `X-Forwarded-For` is intentionally not honoured (spoofing); reverse-proxy deployments should enforce there. Lives in `mcpg.http_runtime`. | S | Low | Tiny middleware. Often handled at the reverse-proxy layer instead. |
| 4.4 | ✅ **Shipped.** TLS / mTLS for the HTTP transport (`MCPG_HTTP_TLS_CERTFILE` / `MCPG_HTTP_TLS_KEYFILE` / `MCPG_HTTP_TLS_CA_CERTS` / `MCPG_HTTP_TLS_CLIENT_CERT_REQUIRED`) — uvicorn terminates TLS itself; with `client_cert_required=true` + a CA bundle it's full mutual TLS. Cross-validated at boot (cert+key both set, mTLS needs CA, paths must exist). Lives in `mcpg.http_runtime`. | S | Medium | Cert wiring; commonly done at the proxy layer. |

## 5. Backups & DR

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 5.1 ✅ | Scheduled logical backups via `pg_cron` + `dump_database` | S | Medium | Shipped as `schedule_logical_backup`: composes `cron.schedule` with `COPY TO PROGRAM 'pg_dump ...'`. Tight allowlist on `destination`/`pg_dump_path`/`database`. PostgreSQL-superuser-only at runtime. |
| 5.2 | ✅ **Shipped.** **`get_wal_archive_status`** — WAL-archiving health from `pg_stat_archiver` + the archive-mode GUCs. The early-warning signal for a failing `archive_command` / `archive_library` (full volume, bad object-store credentials, network partition) that otherwise silently accumulates WAL in `pg_wal/` until the volume fills. Computes a `healthy` verdict (false when archiving is on and the latest attempt failed) + human-readable `detail`. The `archive_command` string is never echoed (credentials) — only a boolean `archive_command_set`. Read-only; never raises. Lives in `mcpg.wal_archive`; `operations_and_health` bucket; typed return → emits an `outputSchema`. | M | Low | Niche; only useful where WAL archiving is configured. |
| 5.3 | ✅ **Shipped.** **`check_pitr_readiness`** — composes `get_wal_archive_status` (5.2) with the PITR-gating GUCs (`wal_level` >= replica, `max_wal_senders` >= 1, `full_page_writes` on) into a single readiness verdict + per-gate breakdown + ordered remediation list. Read-only advisor; scoped as a focused readiness checker rather than full recovery orchestration. Lives in `mcpg.pitr`; `operations_and_health` bucket; typed return. Completes §5. | M | Low-Medium | Heavy lift for a narrow audience. |

## 6. Schema design / quality

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 6.1 | ✅ **Shipped.** `recommend_index_drops` — sibling of `recommend_indexes` for what to drop. Walks `pg_stat_user_indexes` + `pg_stat_user_tables` for indexes that look like pure cost. Three reason codes (descending strength): `never_used` (idx_scan = 0), `scan_no_fetch` (planner picks it but it returns no rows — existence-check pattern), `rarely_used` (scan rate below `low_scan_ratio` of the table's total scan activity). Excludes primary/unique/exclusion-constraint indexes. Returns a ready-to-run `DROP INDEX CONCURRENTLY` per candidate. Lives in `mcpg.indexing`. | S | Medium | Sibling of `recommend_indexes`. |

## 7. Migration ecosystem integration

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 7.1 | ✅ **Shipped (first cut).** `list_unapplied_migration_scripts` — walks a Flyway / Alembic / Liquibase scripts directory and cross-references each script's identifier against the framework's history table (`flyway_schema_history` / `alembic_version` / `databasechangelog`). Reports pending + applied with a one-line first-comment preview per script. `available=false` distinct from `pending_count=0` for greenfield databases. Filesystem access is gated by `MCPG_MIGRATION_SCRIPTS_ROOTS` (refuses every path by default). DDL-gated. Lives in `mcpg.migration_ingestion`. Execution via `prepare_migration` is the natural follow-up. | M-L | Medium | Big agentic win for projects with existing migration history. |
| 7.2 | ✅ **Shipped.** Pre-deployment migration validation (target schema vs production snapshot) | M | High | Composes `compare_schemas` + shadow workflow. |
| 7.3 | ✅ **Shipped.** Migration history table integration (read Alembic / Flyway / Diesel native tables) | S | Medium | Reads existing tooling's bookkeeping. |
| 7.4 | ✅ **Shipped.** Zero-downtime migration cookbook | S | Medium-High | Pure docs (patterns, not code). |

## 8. AI / agent-specific

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 8.1 | ✅ **Shipped (bundle with 8.7).** **Test-row factory** (`generate_test_row_for(schema, table)`). Produces ONE realistic row designed to insert cleanly against the catalogue as it stands today. Identity / `GENERATED ALWAYS` columns are skipped (server fills them in); FK columns sample one existing row from the referenced table so composite FKs stay consistent; column-name patterns (`*_email`, `*_url`, `*_at`, `*_phone`, `country_code`, `currency_code`, `first_name` / `last_name` / `full_name`, `slug`, `ip_address`) produce realistic values; type-driven synthesis is the fallthrough. Returns a `GeneratedTestRow` with one ready-to-execute INSERT plus a per-column `ColumnFill` carrying the chosen heuristic. Lives in `mcpg.test_row_factory`; routed to `data_movement` bucket. Deliberately the sibling of (not replacement for) `generate_test_data` — purpose-built for the shadow-migration workflow where ONE row that actually inserts matters more than volume. | M | Medium-High | Pairs with the shadow-migration workflow. |
| 8.2 | ✅ **Shipped.** Schema-documentation generator (Markdown table reference from catalog) | S | Medium | Sibling of `generate_schema_diagram`. |
| 8.3 | ✅ **Shipped.** **MCP resources (`mcpg://…`).** Four preload-on-connect URIs registered alongside the tool surface: `mcpg://about/index` (full describe_self payload), `mcpg://capabilities/index` (compact bucket list), `mcpg://capabilities/{bucket_id}` (per-bucket detail), `mcpg://schema/{schema_name}` (compact Markdown schema dump). All emit `application/json`. Lives in `mcpg.resources`; registered from `mcpg.tools._register_resources`. Contract test at `tests/contract/test_mcp_resources.py` pins the URI set + variable shape + uniform MIME type. Closes the `about.py`-docstring gap that's been open since the introspection-review work. | M | High | First substantive use of MCP resources in MCPg. Pairs with PR-13 (`outputSchema`). |
| 8.4 | ✅ **Shipped.** MCP prompts surface — three pre-built investigation playbooks (`diagnose_slow_query`, `bisect_slow_migration`, `review_rls_policy`) exposed via the standard MCP `prompts/list` + `prompts/get` primitives. Each body lists the canonical MCPg tool names step-by-step so an agent following the plan literally just works against the current surface. Diagnosis-only — proposes `CREATE POLICY` / index / rollback SQL but never applies. Lives in `mcpg.prompts` + `mcpg.tools._register_prompts`; contract test pins names + argument shapes. Completes the MCP primitive triad alongside resources (8.3) and tools. | M | High | Lets us ship pre-built workflows agents can pick up by reference instead of reconstructing each session. |
| 8.5 | ✅ **Shipped.** `describe_tool(name)` — single-tool deep-dive introspection for the agent self-recovery loop. Returns the registered description, `inputSchema`, `outputSchema`, and bucket metadata for one tool by name. Unknown names come back with `registered=false` plus a `did_you_mean` list (close matches via `difflib`; empty list distinguishes "typo with obvious fix" from "wrong server"). Lives in `mcpg.tool_introspection`. Read-only, no DB access. Classified under the `observability` bucket. | S | Medium | Closes the agent self-recovery loop. |
| 8.6 | ✅ **Shipped (7 batches; mechanical sweep complete).** **`outputSchema` sweep.** PR-13 landed the typed-return pattern + manifest infra on the PG 19 DDL family (5 tools); PR-158 swept the 8 PG 19 modules (`pgq` / `pg19_runtime` / `pg19_skip_scan` / `pg19_partitions` / `pg19_stats` / `wait_for_lsn` / `aio` / `repack`, 28 tools); **batch 3** swept the live-ops / health long tail (`locks` / `walinspect` / `io_stats` / `migration_history` / `liveops` / `health` / `cron` / `partman` / `extensions`, 18 tools); **batch 4** swept the full `introspection` catalogue-read family (26 tools); **batch 5** swept the vector / RAG / search family (`vector_ops` / `vector_tuning` / `vector_tuner_advanced` / `rag_efficiency` / `rag_telemetry` / `pg_search` / `textsearch` / `timescaledb`, 42 tools); **batch 6** swept the FDW / extensions family (`pg_prewarm` / `turboquant` / `redis_fdw`, 26 tools); **batch 7** swept the advisor / ops / data / write remainder (47 tools across ~23 modules). The mechanical sweep is complete — the ~17 remaining `dict[str, Any]` tools are code-emitting / result-restructuring handlers (ORM generators, `describe_self`, `prepare_migration` / `validate_migration_schema` which flatten their result) that legitimately can't auto-derive a clean schema; they stay opaque by design. Manifest floor now **192**. Mechanical per the checklist in [`contributing/adding-tools.md`](contributing/adding-tools.md): drop `slots=True`, change the handler return annotation to the dataclass, drop `asdict()`, add the field set to the manifest, bump the floor. **Note** (corrected during batch 3): a `schema` dataclass field is NOT a Pydantic blocker — `repack_table.schema` already ships fine; the earlier `table_schema` rename guidance was over-cautious. Remaining: the advisor / data-movement / introspection long tail. | L | Medium-High | The headline LangChain / LangGraph integration story — every sweep PR moves the floor in `test_tool_output_schemas.py`. |
| 8.7 | ✅ **Shipped (bundle with 8.1).** **Session-scope cost advisor** (`analyze_session_cost(lookback_minutes=60, hot_threshold=10)`). Reads `mcpg_audit.events` over the configured window and emits findings: `redundant_listing` (catalogue-listing tools called > threshold → suggests `get_compact_schema` instead), `hot_repeated_call` (any other tool called > threshold → suggests caching in conversation memory), `idle_session` (window had no events). Returns `audit_table_present=False` with a diagnostic when the audit subsystem isn't on, distinguishing it from genuine idle. Lookback lands as a bound parameter via `make_interval(mins => %s)` — no f-string composition. Lives in `mcpg.session_advisor`; routed to `observability` bucket. | M | Medium-High | New advisor on the existing `mcpg_audit.events` table. |
| 8.8 | ✅ **Shipped (bundle with 14.4).** **Session-intent surface filter** (`MCPG_SESSION_INTENT` env var). Operator declares the agent's high-level goal at server-start time; MCPg filters its tool surface to the capability buckets relevant to that intent BEFORE the first `tools/list` request. Five built-in presets — `lookup` (read + observability), `migration` (schema + migrations + audit), `vector_rag` (catalogue + query + vector / text search + RAG telemetry), `monitor` (ops + advisors), `admin` (no filter sentinel). Raw bucket ids accepted alongside presets. `describe_self` and `describe_tool` are always kept as the introspection escape hatch. The defence is structural — adversary can't talk the agent into a tool that isn't on the wire. Lives in `mcpg.session_intent`. **Scope note**: shipped as launch-time env var rather than a runtime `begin_session()` MCP tool — the MCP transport advertises tools on connect, so a call-time policy gate would still leak the attack surface in `tools/list`. The launch-time filter is the only way to make the tools truly invisible. | M | High | New MCP-level primitive; pairs with 8.3 / 8.4. |

## 9. pgvector extensions

Building on the already-shipped `vector_search`,
`vector_range_search`, `hybrid_search`, `recommend_vector_index`,
`recommend_vector_quantization`, `analyze_vector_search`,
`analyze_vector_table`, `describe_table` vector-dimension
awareness, and HNSW/IVFFlat detection in `list_indexes`:

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 9.1 | ✅ **Shipped.** HNSW recall/speed tuner. The raw single-query curve (`analyze_hnsw_recall`) was already registered; the actionable advisor **`recommend_hnsw_ef_search`** completes the row — samples N query vectors, builds exact brute-force ground truth per query, sweeps `ef_values` measuring **mean recall@k + p50/p95 latency**, and recommends the smallest `ef_search` clearing `target_recall` (default 0.95). Verifies an HNSW index actually exists on the column (the single-query tool can't tell a real index from a seq scan → silently reports recall 1.0). Query row excluded from its own results. Returns typed `HnswRecallRecommendation`. Lives in `mcpg.vector_tuner_advanced`; `vector_search` bucket. Coexists with `analyze_hnsw_recall` (no-deprecation). | M | High | Lets agents pick the right speed/quality knob without manual tuning. |
| 9.2 | ✅ **Shipped.** `mmr_search` — Maximal Marginal Relevance re-ranking on top of vector_search for result diversity. `lambda_mult` trades relevance for diversity; cosine over candidate embeddings, metric-independent. | S-M | Medium-High | Quality of agentic RAG flows. |
| 9.3 | ✅ **Shipped.** `cluster_vectors` — k-means (Lloyd + k-means++ seeding) over up to 5000 sampled rows of an embedding column. Returns centroids + per-row assignments; deterministic via `seed`; `metric` supports `l2` (default) or `cosine` (vectors normalised, centroids re-normalised each iteration). Lives in `mcpg.vector_ops`. | M | Medium-High | Exploration / segmentation tool. |
| 9.4 | ✅ **Shipped.** `detect_vector_outliers` — k-means clusters the sample (same engine as `cluster_vectors`) and flags rows whose distance to their cluster centroid sits more than `zscore_threshold` (default 3.0) standard deviations above the per-cluster mean. Singleton clusters automatically flag their lone member. Returns outliers sorted by z-score (capped at `max_results`), unclipped `total_outliers`, and per-cluster mean / std stats. Read-only; `available=false` without pgvector. Lives in `mcpg.vector_ops`. | S-M | Medium-High | Data quality + content moderation. |
| 9.5 | ✅ **Shipped.** `monitor_embedding_drift` — samples a baseline + current window of an embedding column (filtered by a timestamp column), computes the per-dimension centroid and L2-norm distribution of each, and reports the cosine distance between the two centroids plus relative changes in the norm mean/std. `drift_detected` flips when cosine distance exceeds `drift_threshold` (default 0.05). Windows are half-open `[start, end)`. Returns `insufficient_data=true` distinctly from `drift_detected=false` when either window is empty. Read-only; lives in `mcpg.vector_ops`. | M | Medium | Ops / model-quality monitoring. |
| 9.6 | ✅ **Shipped.** `import_vectors` — bulk-load embeddings from JSON/CSV into a pgvector `vector(N)` column; reads the declared `N` from the catalog and validates every row before any INSERT runs. Optional parallel `id_column`. | S | Medium | Sibling of `import_csv` specialised for vector columns. |
| 9.7 | ✅ **Shipped.** `cross_table_similarity` — locates a row in table A by id, then issues a pgvector k-NN against table B. Catalog-verifies both columns are `vector(N)` with matching N up front so dimension mismatches fail with a clear error. Lives in `mcpg.vector_ops`. | S | Medium | Useful for entity resolution / linking across tables. |
| 9.8 | ✅ **Shipped.** `analyze_distance_metric` — samples up to 1000 rows of an embedding column, computes the L2-norm distribution, and recommends cosine / l2 / inner_product (pre-normalised → inner_product; constant magnitude → cosine; variable magnitude → cosine). Lives in `mcpg.vector_ops`. | S | Medium | Concrete advice when the user hasn't decided yet. |
| 9.9 | ✅ **Shipped.** `monitor_index_build` — surfaces every active `CREATE INDEX` from `pg_stat_progress_create_index` (PG12+), with resolved `schema.relation.index_name`, phase label, and a computed `progress_pct` (blocks first, tuples as fallback). Lives in `mcpg.liveops`. | S | Medium | Lives next to `list_active_queries`; useful for big-table index work. |
| 9.10 | ✅ **Shipped.** `migrate_vector_to_halfvec` — read-only DDL planner that converts a `vector(N)` column to `halfvec(N)`. Reads the column's type + dimension + row count + every index from the catalog and emits an ordered `migration_sql` plan (drop affected indexes, `ALTER COLUMN` via `USING col::halfvec(N)`, recreate each index with its `halfvec_*_ops` sibling) plus a mirror `rollback_sql`. Returns `already_halfvec=true` (empty plan) when the column is already at the target type, and refuses any ANN index whose opclass has no halfvec sibling rather than rewrite it incorrectly. Caller is expected to validate via the shadow-migration workflow before applying. Lives in `mcpg.vector_tuning`. | S-M | Medium | Pairs with `recommend_vector_quantization`. Uses the existing shadow workflow. |

## 10. pg_turboquant integration

The full phased plan lives in
[`plans/pg_turboquant-integration.md`](plans/pg_turboquant-integration.md).
The cross-backend retrieval-quality and rerank-analytics work that
composes with this lives in
[`plans/rag-efficiency-suite.md`](plans/rag-efficiency-suite.md).

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 10.1 | ✅ **Shipped (TQ-1).** Read advisors for [pg_turboquant](https://github.com/mayflower/pg_turboquant): `list_turboquant_indexes`, `get_turboquant_index_metadata`, `get_turboquant_heap_stats`, `get_turboquant_last_scan_stats`. Defensive JSON parsing — documented fields are typed; the full upstream payload is preserved in `raw_metadata` / `raw` so future-added fields stay reachable. Each index info also carries `index_options` — the `WITH (...)` build-time options (`bits`, `lists`, `transform`, `normalized`) parsed from `pg_class.reloptions` into typed values. Returns empty list / `None` when the extension is absent. Lives in `mcpg.turboquant`. | S | Medium | Mirrors how `cron` / `partman` are surfaced. `pg_turboquant` added to `ENABLEABLE_EXTENSIONS`. |
| 10.2 | ✅ **Shipped (TQ-5, post-investigation).** Three read-only query tools: `turboquant_approx_candidates`, `turboquant_rerank_candidates`, `recommend_turboquant_query_knobs`. Argument types + return shapes taken verbatim from upstream's `sql/pg_turboquant--0.1.0.sql` (no speculation). `metric` accepts the same public-facing names as TQ-4 (`cosine` / `inner_product` / `l2`) and translates internally to upstream's lexical token via a new `_TQ_METRIC_TEXT_FOR_METRIC` mapping. `half_precision=True` switches to the upstream `halfvec` overload. The knob advisor dispatches between upstream's plain and index-aware overloads based on whether `index_schema`/`index_name` are supplied. Un-defers the RAG efficiency suite's turboquant arm. | M | High | See `plans/pg_turboquant-integration.md` Phase 5. |
| 10.3 | ✅ **Shipped (TQ-2 + post-investigation field-alignment).** `recommend_turboquant_maintenance` advisor + `audit_turboquant_indexes` category wired into `audit_database`. Stable-coded rules: `prerequisites_unmet` (CRITICAL — pgvector missing) and `delta_tier_large` (WARNING — upstream's own `delta_health.merge_recommended` advisory). Three earlier rules (`format_v1_reindex_needed`, `maintenance_due`, `fast_path_ineligible`) were removed in the field-alignment pass when a direct read of `src/tq_extension.c` confirmed their source fields don't appear in upstream's actual JSON payload — they were extracted from README prose. Category returns `None` when the extension is absent so `audit_database` cleanly omits it on stock clusters. Lives in `mcpg.turboquant`. | S-M | Medium-High | Lives in `mcpg.turboquant`. |
| 10.4 | ✅ **Shipped (TQ-3).** `maintain_turboquant_index` — write tool wrapping `tq_maintain_index(...)`. Identifier validation + catalog pre-flight (`pg_index ⨝ pg_am`) confirms the named index is actually a turboquant index before invoking upstream. Client-side wall-time measurement; upstream's PG return value is intentionally not parsed (no documented return shape). WRITE-gated. Lives in `mcpg.turboquant`. | S | Medium | Composes directly on TQ-2's `maintenance_due` suggested action. |
| 10.5 | ✅ **Shipped (TQ-4).** `create_turboquant_index` + `reindex_turboquant_index` DDL tools. `metric` allowlist of 3 → single-source-of-truth opclass dict (`tq_cosine_ops` / `tq_inner_product_ops` / `tq_l2_ops`); `bits` bounded `1..64`; `lists` bounded `0..1_000_000`; `transform` allowlist of `{'hadamard'}` only (no `'none'` guess); `normalized` strict bool. Any option not supplied is omitted from the WITH clause so upstream's default applies. Identifier safety via `_validate_identifier` + `_pg_quote_ident`. Runs via `Database.run_unmanaged` (CONCURRENTLY needs autocommit). Rendered SQL preserved on the result for auditability. Unrestricted + `MCPG_ALLOW_DDL`. Lives in `mcpg.turboquant`. | M | Medium-High | Completes the pg_turboquant integration. |

## 11. RAG efficiency suite

Cross-backend retrieval-quality + cross-encoder rerank analytics.
Full design plan in
[`plans/rag-efficiency-suite.md`](plans/rag-efficiency-suite.md).

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 11.1 | ✅ **Shipped (RAG-A).** `analyze_vector_search_efficiency` — one report, three backends (HNSW / IVFFlat / turboquant). Sweeps a candidate-multiplier axis mapped to the right per-backend knob (`ef_search` / `probes` / `candidate_limit`); reports recall@k, Spearman + Kendall rank correlation, p50/p95 wall-clock latency, and (turboquant only) page-pruning ratio. Five rule codes: `baseline_recall_low` (CRITICAL), `rerank_lift_flat`, `rerank_lift_steep`, `ranking_degraded`, `pruning_ineffective` (WARNING). Pure-Python statistics. `inner_product` deferred to a follow-up (operator/function ordering convention needs end-to-end verification). | M | High | Phase A of the suite. Lives in `mcpg.rag_efficiency`. |
| 11.2 | ✅ **Shipped (RAG-B).** `audit_vector_indexes` category — folds `analyze_vector_search_efficiency` into `audit_database`. Walks `pg_index` for HNSW / IVFFlat / turboquant in user schemas, runs a small per-index sweep (sample_size=10, multipliers=(1, 4)). Composite-PK and PK-less tables skipped silently with a GOOD baseline metric. Per-index failures isolated. Lives in `mcpg.rag_efficiency`. | S | Medium | Phase B. |
| 11.3 | ✅ **Shipped (RAG-C).** `mcpg_rag.rerank_events` schema + `setup_rag_telemetry` (DDL-gated; idempotent — reports first-run vs no-op) + `log_rerank_event` (WRITE-gated; 11 typed fields + optional `extra` JSONB). Three indexes: `occurred_at`, `query_hash`, composite `(reranker_model, occurred_at)`. `query_hash` is the join key so PII stays out by default. Lives in `mcpg.rag_telemetry`. | S | Medium | Phase C — storage layer for Phase-D analytics. |
| 11.4 | ✅ **Shipped (RAG-D).** `analyze_reranker_lift`, `analyze_topk_stability`, `analyze_rerank_score_distribution`, `analyze_rerank_ndcg`, `recommend_rerank_strategy`, plus the `audit_rag_pipeline` category in `audit_database`. Reads from `mcpg_rag.rerank_events` (RAG-C); zero-config when the table doesn't exist (audit category cleanly omitted). Five rule codes: `reranker_idle`, `topk_stable`, `score_clustering`, `rerank_hurts_ndcg`, `rerank_lifts_ndcg`. Pure-Python Jaccard / NDCG / histogram helpers. | M | High | Phase D. Lives in `mcpg.rag_efficiency`. |
| 11.5 | ✅ **Shipped (RAG-E).** `mcpg_rag.efficiency_observations` table + `setup_efficiency_observations` (DDL) + `record_efficiency_observation` (write) + `recommend_efficiency_thresholds` (read). `analyze_vector_search_efficiency` gains an optional `thresholds` kwarg; rules read adapted values via a new `_threshold` helper with silent fallback to defaults. Three of seven thresholds adapted in this phase (`baseline_recall_low`, `ranking_degraded_spearman`, `pruning_ineffective`); minimum corpus size 30 before adapting. Lives in `mcpg.rag_telemetry`. | M | Medium-High | Self-learning framework. |

## 12. BM25 sparse search

Full planning doc in
[`plans/bm25-integration.md`](plans/bm25-integration.md). A three-way
comparison of `pg_search` (ParadeDB), `pg_textsearch` (Tiger Data),
and `pg_tokenizer` + `vchord_bm25` (VectorChord) selected `pg_search`
as the first integration target. The other two are deferred with
documented return conditions.

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 12.1 | ✅ **Shipped.** **`pg_search` (ParadeDB) wrapper** — five-phase integration, all live in `mcpg.pg_search` (155 unit tests): BM-1 observability (`list_pg_search_indexes`, `get_pg_search_index_metadata`), BM-2 search execution (`pg_search_run`, `pg_search_more_like_this`, `pg_search_parse_query`), BM-3 hybrid BM25+pgvector composition (`hybrid_bm25_vector_search`), BM-4 DDL (`create_pg_search_index`, `reindex_pg_search_index`), BM-5 advisor (`recommend_pg_search_maintenance`) + audit category (`audit_pg_search_indexes`, wired into `audit_database`). Composes naturally with the RAG efficiency suite. | L | High | Selected over the alternatives for PG 14-18 coverage + pre-built binaries + stable v2 API + documented hybrid pattern. |
| 12.2 | **`pg_textsearch` (Tiger Data) wrapper — deferred.** PG 17/18-only today; no phrase queries. Returns when PG 14-16 support lands or when TimescaleDB integration motivates the lineage match. | L | Medium-High | Strong fallback. |
| 12.3 | **`pg_tokenizer` + `vchord_bm25` wrapper — deferred.** Pre-1.0 (0.3.0); split-extension install; no documented pgvector hybrid pattern. Strong CJK tokenizer story (Jieba, Lindera, BERT). Returns when CJK / multilingual is a top-line goal. | L | Medium | Strong third choice. |

## 13. Multi-database support

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 13.1 | One MCPg server, multiple `MCPG_DATABASE_URL`s — tool-level db selector | L | Medium | ✅ Shipped. `MCPG_SECONDARY_DATABASE_URLS` adds named, **read-only** secondary databases; every read-capable tool takes an optional `database` arg, `list_databases` discovers them. Read-only is PostgreSQL-enforced (every secondary query runs in a `READ ONLY` transaction), which sidesteps per-DB write/DDL gating — writes / DDL / shell / migrate stay primary-only. |

## 14. Release engineering & hygiene

Operational papercuts surfaced during Phase 3 — none feature-shaped,
all worth a tiny PR each so they stop generating noise on every
in-flight PR.

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 14.1 | ✅ **Resolved.** **PG 19 CI matrix apt-package noise.** The `Tests (PG 19)` job now probes `apt-cache show postgresql-client-19` before installing: a "package not yet published" miss is downgraded to a `::warning::` (no failure webhook), while genuine apt errors stay fatal so a real mirror/network outage isn't masked (in the `Tests (PG 19)` client-install step of `ci.yml`). The matrix entry remains `continue-on-error: true`. | S | Low (but high noise) | Surfaced 2026-06-22 on PRs #144 / #145 / #146 / #148 / #149 / #150; fixed per Sourcery feedback on #152. |
| 14.2 | ✅ **Resolved.** **CHANGELOG `[Unreleased]` bloat.** The accumulated Phase-3 entries were folded into `[0.6.4] - 2026-06-27` when v0.6.4 was cut (patch-level, not 0.7.0 — per maintainer direction), and the later 5.2/5.3 entries were folded in too (#192). `[Unreleased]` is now empty for the next cycle. | S | Low | Superseded by the 0.6.4 release-cut (14.5) rather than the originally-anticipated v0.7.0. |
| 14.3 | ✅ **Resolved.** **Sourcery review rate-limit noise.** Sourcery's quota recovered — it has delivered normal, substantive reviews on every recent PR (#190–#194) with no rate-limit notices. No cron/label gating was needed; the noise was transient to the Phase-3 burst. | S | Low | Surfaced 2026-06-22; self-resolved by 2026-06-30. |
| 14.4 | ✅ **Shipped (bundle with 8.8).** **`recommend_headline_tools(lookback_days=7, top_n=6)`** — empirical curation of `describe_self`'s per-bucket `headline_tools` from `mcpg_audit.events`. Reads successful events over the window, ranks by call count per bucket, reports `recommended` tuples plus `newcomers` (recommended but not in current curated list) and `departures` (currently headlined but not in recommendation). Reviewable recommendation, NOT an auto-applied override — operators decide whether to update the curated tuples. Returns `audit_table_present=False` with a diagnostic when the audit subsystem is off. Lives in `mcpg.headline_curator`; routed to `observability` bucket. | S | Low | Couples to 1.4. |
| 14.5 | ✅ **Shipped (v0.6.4).** Release-cut PR: bumped `pyproject.toml` + `__version__` to **0.6.4** (patch-level, not 0.7.0 — per maintainer direction), merged the two accumulated `[Unreleased]` blocks into `[0.6.4] - 2026-06-27` in `CHANGELOG.md`, added `docs/release-notes-0.6.4.md`, linked it from `docs/index.md`. `python -m build` + `twine check` produce a clean `mcpg-0.6.4` sdist + wheel. The `v0.6.4` tag (which triggers the PyPI publish workflow) is created manually after merge. | S | High | Maps to the GA-day-0 milestone in `pg19-readiness.md`. |
| 14.6 | ✅ **Shipped.** **Roadmap-row linkage from PRs.** The PR template (`.github/PULL_REQUEST_TEMPLATE.md`) now prompts for `Advances roadmap row: N.M`, and `tools/roadmap_linkage.py` parses `feature-shortlist.md` into `{row_id: status}` so a cited row can be validated at review/merge (`check N.M --open` exits non-zero if the row is missing or already shipped; `list` prints every id + shipped/in_progress/open status). The convention is documented in `docs/contributing/adding-tools.md` §12a. PRs that complete a row flip its ✅ marker in the same PR. | S | Low-Medium | Process change + a tiny validator. |

## 15. WarehousePG (Greenplum-derived MPP) coverage

[WarehousePG](https://github.com/WarehousePG/warehousepg) is the
community fork of Greenplum — a massively-parallel, analytical-
workload Postgres fork (distributed coordinator + segments,
append-optimized + column-oriented tables, `DISTRIBUTED BY`,
resource groups, `gp_*` catalog surface) that picked up the
mantle after the upstream Greenplum project changed its licensing.
Since it's wire-compatible with libpq and the core SQL surface is
PostgreSQL, most MCPg tools already work. The roadmap items below
cover the MPP-specific surface that doesn't exist on vanilla PG.

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 15.1 | ✅ **Shipped.** `get_warehousepg_status` status probe. Detects WarehousePG vs vanilla PG via TWO signals that must agree (case-insensitive `WarehousePG` / `Greenplum` substring in `version()` AND `pg_catalog.gp_segment_configuration` view presence via `to_regclass`). Returns `available`, `version`, `coordinator_role` (`coordinator` / `master`), `segment_count` (primary only, excludes mirrors + coordinator), `mirroring` (bool). Read-only; never raises — driver failures surface as `available=False` with the actual error in `detail`. Lives in `mcpg.warehousepg`; classified under `operations_and_health`. | S | High (gate for the rest) | Mirrors `mcpg.pgq.get_pgq_status` / `mcpg.pg19_runtime.get_logical_replication_status`. |
| 15.2 | ✅ **Shipped (Bundle A).** `list_distribution_policies(schema)` — joins `gp_distribution_policy` to `pg_class` + `pg_attribute` for per-table `method` (HASH / RANDOM / REPLICATED), `distribution_columns` (in catalog order), `num_segments`. Pure read; available=False on vanilla PG via 15.1 gate. | M | High | Pure read; no caller-supplied identifiers in identifier slots. |
| 15.3 | ✅ **Shipped (Bundle A).** `check_segment_health()` — walks `gp_segment_configuration` and rolls up `unhealthy_count` (status != 'u' OR role != preferred_role) + `out_of_sync_count` (mode = 'n'). Pure read; available=False on vanilla PG. | M | High | Read-only; classified under `operations_and_health`. |
| 15.4 | ✅ **Shipped (Bundle A).** `describe_ao_table(schema, table)` — reads `pg_appendonly` for AO / AO-CO storage metadata: row vs column orientation, compression type / level, block size, checksum. Returns `is_ao=false` cleanly for heap tables. Per-segment bloat (pg_aocsseg_*) deferred to a future PR. | M | Medium | Read-only. |
| 15.5 | ✅ **Shipped (Bundle A).** `list_resource_groups()` — reads `gp_toolkit.gp_resgroup_status` for `concurrency`, `cpu_max_percent`, `cpu_weight`, `memory_limit`, `memory_shared_quota`, plus live `num_running` / `num_queueing`. Pure read; available=False on vanilla PG. | S-M | Medium | Read-only. |
| 15.6 | ✅ **Shipped (Bundle B).** `analyze_mpp_query_plan(sql)` — runs `EXPLAIN (ANALYZE, FORMAT JSON)` via `explain_query(io=True)` (same safety pre-flight) and rolls up MPP plan facts: slice count, motion-node inventory (Redistribute / Broadcast / Gather), per-motion senders/receivers/rows. `redistribute_count > 0` is the canonical "data isn't co-located with the join key" signal. | M | High | Pairs with 15.2 — agents diagnose "this query redistributes because it's not co-located with its join". |
| 15.7 | ✅ **Shipped (Bundle B).** `recommend_redistribute(schema, table)` — pure catalog-stats advisor. Reads current `gp_distribution_policy` + `pg_stats.n_distinct` per column; ranks candidates by approximate distinct count (handles negative-encoded fractions via reltuples); flags rewrite when current key has < `segment_count * 10` distinct values AND a better candidate exists. Emits ready-to-review `ALTER TABLE … SET WITH (REORGANIZE=TRUE) DISTRIBUTED BY (…)` DDL — never executes. Per-segment row-count scan deferred. | M-L | Medium-High | Composes 15.2 catalogue read with pg_stats. |
| 15.8 | ✅ **Shipped (Bundle C).** `warehousepg-latest` matrix entry in `.github/workflows/ci.yml`, with a dedicated `.github/ci-postgres-warehousepg.Dockerfile` pulling the community image. Gated `continue-on-error: true` via `experimental: true` on the matrix entry so upstream image churn doesn't block the main suite. PostgreSQL client tooling install is skipped for the WarehousePG lane (wire-compatible with libpq). Closes section §15 in full. | M | Medium | Sibling of roadmap 3.4. |

**Effort note**: 15.1 is the gating prerequisite for the rest — without the status probe each tool would have to repeat the version sniff. Bundle 15.1 + 15.2 as the first PR (the read surface stabilises the catalogue shape we're targeting); subsequent rows split per the M / S-M effort.

**Backward compatibility**: every 15.x tool follows the established `available=False` convention on non-MPP servers — they appear in `describe_self` but return a clear "this is a vanilla PG cluster" diagnostic when called against one. No tool surface gets hidden conditionally.

## 16. Configuration & sizing advisors (pghero / pgtune coverage)

[pghero](https://github.com/ankane/pghero) (Ankane) and [pgtune](https://pgtune.leopard.in.ua/) (le0pard) cover two
operator-facing gaps that MCPg's existing `audit_database` does not:
**sequence-overflow detection**, a **general `postgresql.conf` sanity
sweep**, and **greenfield sizing recommendations**. A short audit
(2026-06-26) confirmed the rest of pghero's surface — index
suggestions, slow queries, long-running queries, bloat, replication
lag, vacuum statistics, password expiration — is already covered by
existing tools (`recommend_indexes` / `recommend_index_drops` /
`analyze_workload` / `list_active_queries` / `recommend_repack` /
`get_logical_replication_status` / `read_autovacuum_priority` /
`audit_database`'s password warnings). The three rows below are the
non-overlapping gaps worth filling.

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 16.1 | ✅ **Shipped (bundle).** **`audit_sequences(warning_pct=80, critical_pct=95)`** — walks `pg_sequences` for serial / identity / explicit sequences nearing their ceiling; computes `last_value / max_value` and flags above `WARNING` / `CRITICAL` thresholds. Lists only at-risk sequences (sorted by `used_pct` desc) with absolute `remaining` headroom; never-advanced sequences (NULL `last_value`) are counted but not flagged. `available=false` on PG < 10. Lives in `mcpg.config_advisor`; classified `advisors`. Shipped as a standalone tool (not an `audit_database` category) — the fold-in is a deferred follow-up. | S | High | Pure-SQL check against `pg_sequences`. |
| 16.2 | ✅ **Shipped (bundle).** **`audit_settings(total_ram_mb=None)`** — `postgresql.conf` sanity sweep via `pg_settings`. Flags dangerous toggles (`fsync=off`, `full_page_writes=off`, `autovacuum=off`, `synchronous_commit=off`), cross-setting issues (`maintenance_work_mem` < `work_mem`, tiny `shared_buffers`, low `checkpoint_completion_target`), and — when `total_ram_mb` is supplied (PG can't see host RAM itself) — RAM-relative ratios for `shared_buffers` / `effective_cache_size`. Memory GUCs read in bytes via `pg_size_bytes(current_setting(...))`. Each finding carries a stable `code` + one-line `suggestion`. Lives in `mcpg.config_advisor`; classified `advisors`. | M | Medium-High | Standalone tool; `random_page_cost`-vs-storage check deferred (needs the storage hint that 16.3 takes as an arg). |
| 16.3 | ✅ **Shipped (bundle).** **`recommend_postgres_conf(total_ram_mb, cpu_count=4, workload='mixed', storage='ssd', max_connections=None)`** — pure pgtune-style calculator (no DB connection). Returns recommended `shared_buffers`, `effective_cache_size`, `work_mem`, `maintenance_work_mem`, `wal_buffers`, `min_wal_size` / `max_wal_size`, `checkpoint_completion_target`, `default_statistics_target`, `random_page_cost`, `effective_io_concurrency`, and the parallel-worker knobs (lifted above PG defaults only when `cpu_count >= 4`). Memory fields are postgres-ready strings; `settings` mirrors everything as a flat `{guc: value}` dict. `workload` ∈ `{web, oltp, dw, desktop, mixed}`; `storage` ∈ `{ssd, hdd, san}`. Lives in `mcpg.config_advisor`; classified `advisors`. Pairs with 16.2 — audit first, then size. | S | Medium-High | Pure calculator. |

**Shipped note**: all three landed in ONE PR (§16 bundle) per the bundling preference — `audit_sequences` + `audit_settings` + `recommend_postgres_conf`, 30 unit tests, `mcpg.config_advisor` module.

**Why not a wholesale pghero port**: pghero's "queries", "indexes", "vacuum", "connections", "replication" panels overlap directly with existing MCPg tools; porting them would create surface duplication that violates the no-deprecation rule (callers couldn't tell `recommend_indexes` apart from a hypothetical `pghero_recommend_indexes`). The three rows above are the strict net-new surface.

---

## Currently deferred (no commitments)

- **Multi-database support beyond read-only secondaries** — 13.1 ships
  named, read-only secondaries (`MCPG_SECONDARY_DATABASE_URLS`); writable
  secondaries with per-DB write/DDL gating remain deferred (the
  read-only boundary deliberately sidesteps that complexity for now).
- **Backups & DR** beyond what `dump_database` /
  `restore_database` already cover — narrow audience.
- **Alembic / Flyway / Liquibase script ingestion** (7.1) — large
  surface; `validate_migration` + `prepare_migration` already
  cover the high-value reviewer workflow.
_(`delta_tier_large` and TQ-5, previously listed here, were
un-deferred after a focused upstream investigation read the C source
and SQL definitions directly — see CHANGELOG for the
post-investigation enhancements PR.)_

---

## See also

- [`parallel-roadmap.md`](parallel-roadmap.md) — how to pick these
  items up as **independent parallel PRs**: conflict map, workstreams,
  and a suggested first batch.
- [`security-hardening.md`](security-hardening.md) — security
  hardening status with ✅ / 🟡 / ⬜ markers.
- [`tour.md`](tour.md) and [`tools.md`](tools.md) — current tool
  surface.
- [`../CHANGELOG.md`](../CHANGELOG.md) — chronological record of
  what shipped when.
