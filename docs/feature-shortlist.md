# Post-v0.4.0 feature shortlist

Candidates for new feature work, organised by area. Each entry shows
rough effort (**S/M/L**), the user-facing value, and any prerequisite
that might block it. Use this as a menu for prioritisation.

Effort scale (rough, single-session yardstick):
- **S** = 1 module, 1 PR, ≤ 1 day equivalent
- **M** = 2–3 modules or wider surface, 1–3 PRs
- **L** = new infrastructure (background workers, transport changes,
  cross-cutting refactors)

---

## 1. Transport & deployment hardening

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 1.1 | **HTTP transport auth** (bearer / API key + middleware) | M | High | Currently `streamable-http` / `sse` have **no auth** — a real-world deployment blocker. Open question in PLAN.md §0.1. |
| 1.2 | TLS / mTLS for HTTP transport | S | Medium | Mostly config + cert wiring. Often handled at the reverse-proxy layer instead. |
| 1.3 | Rate limiting per client / per tool | M | Medium | Pairs naturally with 1.1. |
| 1.4 | **Per-request `SET ROLE`** for multi-tenancy | M | High | Deferred in Phase 6.2 with a note that document-only is the v1 stance. Re-opens multi-tenant deployments. |
| 1.5 | Multi-database support (one server, many DBs) | L | Medium | Today: one server = one `MCPG_DATABASE_URL`. Multi-DB means a tool param, a pool-per-DB, and rethinking gates. |
| 1.6 | Read-replica routing for read tools | M | Low-Medium | Deferred in Phase 6.4. Marginal at MCPg's scale until 1.4 lands. |

## 2. Observability

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 2.1 | **Prometheus `/metrics` endpoint** | S-M | High | Tool-call count / latency / error rate per tool. Pairs with the HTTP transport. |
| 2.2 | **OpenTelemetry spans** per tool call | M | Medium-High | One span per `call_tool` + child spans for the actual query / subprocess. |
| 2.3 | Structured JSON logging output | S | Medium | Optional — wraps the existing `mcpg.audit` logger. |
| 2.4 | k8s-style `/healthz` + `/readyz` endpoints | S | Medium | Just on the HTTP transport. |
| 2.5 | Slow-query logging from the MCP layer | S | Low | The existing `analyze_workload` already covers PG-side timings; this would be a per-tool latency log. |

## 3. Performance & scaling

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 3.1 | **Server-side cursors** for streaming large `run_select` results | M | Medium-High | Listed as Phase 6.4 deferred work. Lets agents fetch result sets larger than `max_rows`. |
| 3.2 | Connection-pool tuning advisor (recommend min/max from observed load) | S | Low-Medium | Reuses existing `check_database_health` plumbing. |
| 3.3 | Bulk-update / bulk-delete tools (parametrised, gated) | S | Low | Sibling to `import_csv` for writes. |
| 3.4 | Parallel query execution helper (`run_selects_parallel`) | S | Medium | Useful for an agent fanning out across schemas. |

## 4. PostgreSQL feature coverage

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 4.1 | **Logical replication management writes** — `create_publication`, `drop_publication`, `create_subscription`, `drop_subscription` | M | Medium-High | Read tools already exist (Phase 16). Closes the loop on logical-replication ops. Gated under DDL. |
| 4.2 | **TimescaleDB hypertable wrappers** | M | High | Most popular PG extension after pgvector/PostGIS. `create_hypertable`, `add_dimension`, `set_chunk_time_interval`, `compress_chunk`, retention policies. |
| 4.3 | `pg_stat_io` exposure (PG 16+) | S | Medium | Big I/O visibility win on modern PG. |
| 4.4 | `pg_buffercache` integration (cache hit analysis at the buffer level) | S | Low-Medium | Niche. |
| 4.5 | `pg_locks` deep inspection / deadlock detector | S | Medium | Live-ops complement. |
| 4.6 | WAL inspection (`pg_walinspect`) | S | Low | Niche but useful for replication debugging. |
| 4.7 | Generated-column awareness in `describe_table` | S | Low-Medium | Currently we don't surface `GENERATED ALWAYS AS ...` columns specially. |
| 4.8 | Row-level security policy tester (`would_this_row_be_visible_to_role`) | M | Medium | Hard-to-test feature; great agentic UX. |

## 5. Developer experience

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 5.1 | **Cookbook of common agent flows** in docs (e.g. "review a migration", "find slow queries", "generate ORM models") | S | Medium-High | Pairs with the tour we just shipped. Pure docs. |
| 5.2 | `summarize_table` composite tool (describe + sample rows + stats in one call) | S | High | Big agent UX win — replaces 4–5 tool calls with one. |
| 5.3 | Sample-data generator (`seed_table_with_sample_data`, gated under WRITE) | M | Medium | Useful for shadow-migration testing and demos. |
| 5.4 | Auto-generated tool examples in MCP tool descriptions | S | Low-Medium | Helps agents pick the right tool. |

## 6. Security & compliance

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 6.1 | **Connection encryption verification tool** — reports `ssl=on/off` + cipher | S | Medium | One-liner check. |
| 6.2 | Sensitive-column heuristic discovery (emails, phone, SSN, credit card by regex + name) | M | Medium-High | Pairs with audit + compliance workflows. |
| 6.3 | Audit log retention / rotation policy | S | Medium | `mcpg_audit.events` grows unbounded today. |
| 6.4 | IP allowlist for HTTP transport | S | Low | Tiny middleware. |
| 6.5 | OIDC / SSO for HTTP transport | L | Medium | Bigger commitment than the simpler 1.1 bearer-token path. |

## 7. Backups & DR

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 7.1 | Scheduled logical backups via `pg_cron` + `dump_database` | S | Medium | Composes existing tools. |
| 7.2 | WAL archive inspection | M | Low | Niche; only useful where WAL archiving is configured. |
| 7.3 | Point-in-time recovery prep helpers | M | Low-Medium | Heavy lift for a narrow audience. |

## 8. Schema design / quality

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 8.1 | **Naming-convention linter** (snake_case, prefix conventions, FK suffix, ...) | S | Medium | Quick win. |
| 8.2 | **Unused-table / unused-column finder** via `pg_stat_user_tables` | S | High | Excellent agent UX for "what can I drop?". |
| 8.3 | Missing-index dead-code detector (sibling to `recommend_indexes` but for *over*-indexed) | S | Medium | Currently `recommend_indexes` only adds, never removes. |
| 8.4 | N+1 query pattern detector via `pg_stat_statements` | M | Medium-High | Composes `analyze_workload` with grouping heuristics. |
| 8.5 | FK cascade visualisation (diagram of what cascades from a delete) | S | Medium | Pairs with `generate_schema_diagram`. |

## 9. Migration ecosystem integration

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 9.1 | Alembic / Flyway / Liquibase migration script ingestion (parse + apply through `prepare_migration`) | M-L | Medium | Big agentic win for projects with existing migration history. |
| 9.2 | Pre-deployment migration validation (target schema vs production snapshot) | M | High | Composes `compare_schemas` + shadow workflow. |
| 9.3 | Migration history table integration (Alembic / Flyway / Diesel native tables) | S | Medium | Reads existing tooling's bookkeeping. |
| 9.4 | Zero-downtime migration cookbook | S | Medium-High | Pure docs (patterns, not code). |

## 10. AI / agent-specific

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 10.1 | **`why_is_this_slow` composite tool** (EXPLAIN + active queries + locks + cache hit + suggestions in one call) | M | Very High | Agent magnet. |
| 10.2 | Natural-language → SQL helper (gated, returns SQL + EXPLAIN for review before run) | M | High | Different shape than the existing tools — calls out to a model. |
| 10.3 | Test-data factory (`generate_test_row_for(schema, table)` using catalog + heuristics) | M | Medium-High | Pairs with the shadow-migration workflow. |
| 10.4 | Schema documentation generator (Markdown table reference from catalog) | S | Medium | Sibling of `generate_schema_diagram`. |

---

## 11. pgvector extensions

Building on what's already shipped (`vector_search`,
`recommend_vector_index`, `analyze_vector_search`,
`analyze_vector_table`, `describe_table` vector-dimension awareness,
HNSW/IVFFlat detection in `list_indexes`):

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 11.1 | **`hybrid_search`** — combine vector + full-text scoring via reciprocal-rank fusion | M | Very High | Single biggest unmet need in agentic retrieval. Pairs the existing `vector_search` and `full_text_search` into one ranked result set. |
| 11.2 | **`range_search`** — find rows within a distance threshold (not top-k) | S | High | Different query shape than k-NN; common for de-dup / similarity gating. |
| 11.3 | **`recommend_vector_quantization`** — flag tables where `halfvec` (16-bit) or `bit` quantization would cut storage 2-32× | S-M | High | pgvector v0.7+. Compares dim × row-count × bytes vs working-set memory; concrete cost win. |
| 11.4 | **HNSW recall/speed tuner** (`analyze_hnsw_recall`) — sweep `ef_search` against a ground-truth set, return recall@k curves | M | High | Lets agents pick the right speed/quality knob without manual tuning. |
| 11.5 | **`mmr_search`** — Maximal Marginal Relevance re-ranking on top of vector_search for result diversity | S-M | Medium-High | Quality of agent RAG flows improves materially. |
| 11.6 | **`cluster_vectors`** — k-means cluster a vector column, return centroids + per-row labels | M | Medium-High | Exploration / segmentation tool; uses ``cube_distance`` or in-memory clustering. |
| 11.7 | **`detect_vector_outliers`** — flag rows whose embedding is far from any cluster centroid | S-M | Medium-High | Data quality + content moderation. |
| 11.8 | **`monitor_embedding_drift`** — compare distributional stats of vectors over time windows | M | Medium | Ops / model-quality monitoring. |
| 11.9 | **`import_vectors`** — bulk-load embeddings from JSON/CSV with `vector(N)` column-type validation | S | Medium | Sibling of `import_csv` specialised for vector columns. |
| 11.10 | **`cross_table_similarity`** — given a row in table A, find the k most similar rows in table B (different embedding source, same dim) | S | Medium | Useful for entity resolution / linking across tables. |
| 11.11 | **`analyze_distance_metric`** — heuristically recommend cosine vs L2 vs inner-product based on vector magnitude distribution | S | Medium | Concrete advice when the user hasn't decided yet. |
| 11.12 | **`monitor_index_build`** — surface HNSW / IVFFlat build progress for long-running index creations | S | Medium | Lives next to `list_active_queries`; useful for big-table index work. |
| 11.13 | **`migrate_vector_to_halfvec`** — generate the DDL to convert a `vector(N)` column to `halfvec(N)` (or `bit`) safely | S-M | Medium | Pairs with 11.3. Uses the existing migration shadow workflow for the structural review. |

**Suggested pgvector picks for Tier A:**
- **11.1 `hybrid_search`** (Very High value, M effort) — biggest agentic-RAG win
- **11.3 `recommend_vector_quantization`** (High value, S-M effort) — concrete cost savings
- **11.2 `range_search`** (High value, S effort) — quick win that fills a real gap

These three would bring the pgvector tool surface from 4 → 7 and
cover the most common gaps agentic-RAG workflows hit.

---

## Suggested priority tiers

These are my recommendations to bucket the list — feel free to override.

**Tier A — high-value, modest effort, unblock real deployments:**
- 1.1 HTTP transport auth (M, High)
- 2.1 Prometheus metrics (S-M, High)
- 5.2 `summarize_table` composite tool (S, High)
- 8.2 Unused-table / column finder (S, High)
- 10.1 `why_is_this_slow` composite tool (M, Very High)
- 4.2 TimescaleDB wrappers (M, High)
- **11.1 Hybrid (vector + FTS) search** (M, Very High)
- **11.3 Vector quantization advisor** (S-M, High)
- **11.2 Vector range search** (S, High)

**Tier B — strong UX or coverage wins:**
- 1.4 Per-request `SET ROLE` multi-tenancy (M, High)
- 4.1 Logical replication writes (M, Medium-High)
- 5.1 Agent cookbook (S, Medium-High)
- 6.2 Sensitive-column heuristics (M, Medium-High)
- 8.4 N+1 detector (M, Medium-High)
- 9.2 Migration validation against production snapshot (M, High)

**Tier C — nice to have:**
- 3.1 Server-side cursors (M, Medium-High)
- 3.4 Parallel select helper (S, Medium)
- 4.3 `pg_stat_io` (S, Medium)
- 4.5 `pg_locks` deeper inspection (S, Medium)
- 4.7 Generated columns (S, Low-Medium)
- 4.8 RLS policy tester (M, Medium)
- 8.1 Naming-convention linter (S, Medium)
- 8.5 FK cascade visualisation (S, Medium)
- 10.3 Test-data factory (M, Medium-High)

**Defer for now:**
- 1.5 Multi-database support — L; very ambitious
- 1.6 Read-replica routing — won't move the needle until 1.4
- 6.5 OIDC / SSO — start with 1.1 bearer-token first
- 7.x Backups & DR family — narrow audience
- 9.1 Migration script ingestion — big surface; wait for demand
- 10.2 NL → SQL — different shape, needs a model dependency
