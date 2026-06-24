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
| 2.1 | Logical replication management writes (`create_publication`, `drop_publication`, `create_subscription`, `drop_subscription`) | M | Medium-High | Read tools already exist. Closes the loop on logical-replication ops; gated under `MCPG_ALLOW_DDL`. |
| 2.2 | ✅ **Shipped.** `pg_buffercache` integration (cache hit analysis at the buffer level) | S | Low-Medium | Niche. |
| 2.3 | ✅ **Shipped.** WAL inspection (`pg_walinspect`) | S | Low | Niche but useful for replication debugging. |
| 2.4 | ✅ **Shipped.** Deeper `pg_locks` walker — deadlock-cycle reconstruction beyond the current `find_blocking_chains` pair list | S-M | Medium | Live-ops complement. |
| 2.5 | **PG 19 PR-10 — small-tools batch.** Audit-row #14 autovacuum scoring read (`read_autovacuum_priority`), #13 password-expiration + MD5 warnings extension to `audit_database`, #21 `pg_get_acl()` migration in `list_grants` with PG ≤ 18 fallback, #18 NL→SQL emission patterns for `GROUP BY ALL` / temporal `UPDATE` / `ON CONFLICT DO SELECT`, #19 `effective_wal_level` exposure in `get_server_info`. Each item is small; bundled as PR-10 in the Phase 3 plan. Combined PO score ~46 across the audit. | M | Medium | Tracking under [`pg19-readiness.md`](plans/pg19-readiness.md) — five sub-items, each landable in its own PR. |
| 2.6 | **PG 19 PR-3 #15 — `EXPLAIN ANALYZE (IO)` capture.** Extend `analyze_query_plan` / `explain_query` with an `io=true` option that surfaces the new AIO cost breakdown in the plan output. Flagged in the Phase 2 audit, never PRed. | S | Medium | Pairs with the already-shipped `recommend_io_method` for closing the AIO observability loop. |

## 3. Developer experience

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 3.1 | ✅ **Shipped (first wave).** Auto-generated tool examples in MCP tool descriptions — the `_with_example(description, example)` helper in `mcpg.tools` wraps every wrapped description with a canonical pseudo-Python invocation hint (``Example: `tool(arg=value)```). ~25 high-traffic tools across introspection / query / composite / health / search / diagrams / schema-diff / vector analytics / migrations / data movement now ship examples. The helper is the contract for new tools. | S | Low-Medium | Helps agents pick the right tool. |
| 3.2 | ✅ **Shipped.** Sample-data generator that writes (`seed_table_with_sample_data`) | M | Medium | Sibling of the current `generate_test_data` (synthetic INSERT statements; does not execute). Gated under WRITE. |
| 3.3 | ✅ **Shipped.** Benchmark harness — `scripts/benchmark_pg19.py` + `scripts/benchmark_pg19.sh` launcher. Quantifies three measurable-without-restart PG 19 wins: skip-scan vs dedicated single-column index, REPACK CONCURRENTLY vs VACUUM FULL, LZ4 vs pglz TOAST. AIO `io_uring` vs `worker` lives as a manual recipe in `pg19-operations-playbook.md` because `io_method` is `PGC_POSTMASTER` and the smoke loop deliberately stays in one container. Server-side timing (`EXPLAIN ANALYZE` + `clock_timestamp()`) so the numbers don't include psycopg / network noise. | M | Medium-High | Maps to the GA-day-0 verification milestone in `pg19-readiness.md`. |
| 3.4 | **PG 19 PR-11 — characterisation tests.** Defensive coverage that asserts the SQL each Phase 3 tool emits actually parses on PG 14-18 + PG 19. Today's unit tests use mocked drivers so a syntax-level drift could slip through. The existing smoke harness covers PG 19 only and only the live-cluster paths. | M | Medium | Bundles audit rows #16, #17, #20. Catches drift between the assumed PG 19 grammar and the real one. |

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
| 5.2 | WAL archive inspection | M | Low | Niche; only useful where WAL archiving is configured. |
| 5.3 | Point-in-time recovery prep helpers | M | Low-Medium | Heavy lift for a narrow audience. |

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
| 8.1 | Test-data factory using catalog + heuristics (`generate_test_row_for(schema, table)`) | M | Medium-High | Pairs with the shadow-migration workflow. |
| 8.2 | ✅ **Shipped.** Schema-documentation generator (Markdown table reference from catalog) | S | Medium | Sibling of `generate_schema_diagram`. |
| 8.3 | ✅ **Shipped.** **MCP resources (`mcpg://…`).** Four preload-on-connect URIs registered alongside the tool surface: `mcpg://about/index` (full describe_self payload), `mcpg://capabilities/index` (compact bucket list), `mcpg://capabilities/{bucket_id}` (per-bucket detail), `mcpg://schema/{schema_name}` (compact Markdown schema dump). All emit `application/json`. Lives in `mcpg.resources`; registered from `mcpg.tools._register_resources`. Contract test at `tests/contract/test_mcp_resources.py` pins the URI set + variable shape + uniform MIME type. Closes the `about.py`-docstring gap that's been open since the introspection-review work. | M | High | First substantive use of MCP resources in MCPg. Pairs with PR-13 (`outputSchema`). |
| 8.4 | ✅ **Shipped.** MCP prompts surface — three pre-built investigation playbooks (`diagnose_slow_query`, `bisect_slow_migration`, `review_rls_policy`) exposed via the standard MCP `prompts/list` + `prompts/get` primitives. Each body lists the canonical MCPg tool names step-by-step so an agent following the plan literally just works against the current surface. Diagnosis-only — proposes `CREATE POLICY` / index / rollback SQL but never applies. Lives in `mcpg.prompts` + `mcpg.tools._register_prompts`; contract test pins names + argument shapes. Completes the MCP primitive triad alongside resources (8.3) and tools. | M | High | Lets us ship pre-built workflows agents can pick up by reference instead of reconstructing each session. |
| 8.5 | **`describe_tool(name)`.** Runtime introspection for an agent that hit a tool error and wants the schema / preconditions without re-walking the full `describe_self` payload. Returns the same shape that `tools/list` would expose for one tool, but as a tool itself (handy when the MCP client transport only surfaces tool calls, not `tools/list`). | S | Medium | Closes the agent self-recovery loop. |
| 8.6 | **`outputSchema` sweep — remaining ~200 tools.** PR-13 landed the typed-return pattern + manifest infra on the PG 19 DDL family (5 tools). Each remaining module is a mechanical sweep per the checklist in [`contributing/adding-tools.md`](contributing/adding-tools.md). Order by module size: `pg19_partitions` / `pg19_runtime` / `pg19_stats` / `pg19_skip_scan` / `wait_for_lsn` / `repack` / `aio` / `pgq` first (one PR each); then the long-tail introspection / advisor / data-movement families. | L | Medium-High | The headline LangChain / LangGraph integration story — every sweep PR moves the floor in `test_tool_output_schemas.py`. |
| 8.7 | **Session-scope cost advisor.** "You've called `list_tables` 47 times this session — one `get_compact_schema` would have sufficed." Reads the audit log within the session and surfaces hot-path inefficiencies. Real money for token-conscious deployments. | M | Medium-High | New advisor on the existing `mcpg_audit.events` table. |
| 8.8 | **Session-intent handshake (`begin_session(intent='migration')`).** Lets the agent declare its high-level goal at connect time; we narrow the tool surface to the relevant capability buckets. Big prompt-injection resilience win — a "look up a user record" agent can't be tricked into calling `drop_database` because that tool isn't on the wire at all. | M | High | New MCP-level primitive; pairs with 8.3 / 8.4. |

## 9. pgvector extensions

Building on the already-shipped `vector_search`,
`vector_range_search`, `hybrid_search`, `recommend_vector_index`,
`recommend_vector_quantization`, `analyze_vector_search`,
`analyze_vector_table`, `describe_table` vector-dimension
awareness, and HNSW/IVFFlat detection in `list_indexes`:

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 9.1 | HNSW recall/speed tuner (`analyze_hnsw_recall`) — sweep `ef_search` against a ground-truth set, return recall@k curves | M | High | Lets agents pick the right speed/quality knob without manual tuning. |
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
| 12.1 | **`pg_search` (ParadeDB) wrapper** — five-phase integration: BM-1 observability, BM-2 search execution (`pg_search_run`, `pg_search_more_like_this`, `pg_search_parse_query`), BM-3 hybrid BM25+pgvector composition, BM-4 DDL (`create_pg_search_index`, `reindex_pg_search_index`), BM-5 advisor + audit category. Composes naturally with the RAG efficiency suite. | L | High | Selected over the alternatives for PG 14-18 coverage + pre-built binaries + stable v2 API + documented hybrid pattern. |
| 12.2 | **`pg_textsearch` (Tiger Data) wrapper — deferred.** PG 17/18-only today; no phrase queries. Returns when PG 14-16 support lands or when TimescaleDB integration motivates the lineage match. | L | Medium-High | Strong fallback. |
| 12.3 | **`pg_tokenizer` + `vchord_bm25` wrapper — deferred.** Pre-1.0 (0.3.0); split-extension install; no documented pgvector hybrid pattern. Strong CJK tokenizer story (Jieba, Lindera, BERT). Returns when CJK / multilingual is a top-line goal. | L | Medium | Strong third choice. |

## 13. Multi-database support

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 13.1 | One MCPg server, multiple `MCPG_DATABASE_URL`s — tool-level db selector | L | Medium | Today: one server = one DSN. Multi-DB means a per-tool param, a pool-per-DB, and rethinking gates. Big lift; no concrete demand yet. |

## 14. Release engineering & hygiene

Operational papercuts surfaced during Phase 3 — none feature-shaped,
all worth a tiny PR each so they stop generating noise on every
in-flight PR.

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 14.1 | **PG 19 CI matrix apt-package noise.** The `postgresql-client-19` install in the `Tests (PG 19)` job has failed `apt-package` lookup on every Phase 3 PR (~15 webhook events in 24h). Fix: either build the client inside `.github/ci-postgres-pg19.Dockerfile` (where pgvector already builds from source), suffix the apt install with `|| true`, or gate the install behind the matrix entry's `continue-on-error` so it stops emitting failure webhooks. | S | Low (but high noise) | Surfaced 2026-06-22 on PRs #144 / #145 / #146 / #148 / #149 / #150. |
| 14.2 | **CHANGELOG `[Unreleased]` bloat.** Section now spans 7 Phase-3 PRs and growing. Split into a `### PG 19 readiness` heading inside `[Unreleased]` so the eventual v0.7.0 release notes write themselves. | S | Low | Pairs with the v0.7.0 release-cut PR (14.5). |
| 14.3 | **Sourcery review rate-limit noise.** Sourcery has been weekly-rate-limited on every Phase 3 PR for two days — every "review left" webhook is a rate-limit notice we still inspect per the PR-watcher SOP. Either move sourcery to a once-weekly cron, gate it behind a label, or drop it from the rotation entirely until the quota is sorted. | S | Low | Surfaced 2026-06-22. |
| 14.4 | **`describe_self` count drift.** The `mcpg.about.build_capability_summary()` payload uses live tool counts but the curated `headline_tools` list per bucket is hand-maintained. As we add tools, headline_tools should auto-include the most-used ones (top-N by 1.4 telemetry once that ships). | S | Low | Couples to 1.4. |
| 14.5 | **v0.7.0-pg19-ready release-cut PR.** Snapshots the current `[Unreleased]` block into a tagged release notes file, bumps the version, flips the PyPI classifier to mention PG 19. Acts as the anchor for GA-day-0 verification work. | S | High | Maps to the GA-day-0 milestone in `pg19-readiness.md`. |
| 14.6 | **Roadmap-item linkage from PRs.** Every Phase 3 PR could cite the matching roadmap row in its body (e.g. "advances 8.6"); the contract review at merge time then explicitly clears the row. Today the linkage is implicit. | S | Low-Medium | Process change, not code. |

---

## Currently deferred (no commitments)

- **Multi-database support** (13.1 above) — very ambitious;
  preferred shape today is one MCPg instance per database.
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
