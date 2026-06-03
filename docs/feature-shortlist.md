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

Effort scale (rough, single-session yardstick):

- **S** — 1 module, 1 PR, ≤ 1 day equivalent
- **M** — 2–3 modules or wider surface, 1–3 PRs
- **L** — new infrastructure (background workers, transport
  changes, cross-cutting refactors)

---

## 1. Observability

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 1.1 | OpenTelemetry spans per tool call | M | Medium-High | One span per `call_tool` + child spans for the actual query / subprocess. |
| 1.2 | Structured JSON logging output toggle | S | Medium | Wraps the existing `mcpg.audit` logger. |
| 1.3 | ✅ **Shipped.** Slow-call logging from the MCP layer | S | Low | Per-tool latency log to flag slow MCPg-side calls (the existing `analyze_workload` covers PG-side timings). |

## 2. PostgreSQL feature coverage

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 2.1 | Logical replication management writes (`create_publication`, `drop_publication`, `create_subscription`, `drop_subscription`) | M | Medium-High | Read tools already exist. Closes the loop on logical-replication ops; gated under `MCPG_ALLOW_DDL`. |
| 2.2 | `pg_buffercache` integration (cache hit analysis at the buffer level) | S | Low-Medium | Niche. |
| 2.3 | WAL inspection (`pg_walinspect`) | S | Low | Niche but useful for replication debugging. |
| 2.4 | ✅ **Shipped.** Deeper `pg_locks` walker — deadlock-cycle reconstruction beyond the current `find_blocking_chains` pair list | S-M | Medium | Live-ops complement. |

## 3. Developer experience

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 3.1 | Auto-generated tool examples in MCP tool descriptions | S | Low-Medium | Helps agents pick the right tool. |
| 3.2 | Sample-data generator that writes (`seed_table_with_sample_data`) | M | Medium | Sibling of the current `generate_test_data` (synthetic INSERT statements; does not execute). Gated under WRITE. |

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
| 4.3 | IP allowlist for HTTP transport | S | Low | Tiny middleware. Often handled at the reverse-proxy layer instead. |
| 4.4 | mTLS for the HTTP transport | S | Medium | Cert wiring; commonly done at the proxy layer. |

## 5. Backups & DR

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 5.1 | Scheduled logical backups via `pg_cron` + `dump_database` | S | Medium | Composes existing tools. |
| 5.2 | WAL archive inspection | M | Low | Niche; only useful where WAL archiving is configured. |
| 5.3 | Point-in-time recovery prep helpers | M | Low-Medium | Heavy lift for a narrow audience. |

## 6. Schema design / quality

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 6.1 | Over-indexed detector (sibling to `recommend_indexes` but for what to drop) | S | Medium | Currently `recommend_indexes` only adds, never removes. |

## 7. Migration ecosystem integration

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 7.1 | Alembic / Flyway / Liquibase migration-script ingestion (parse + apply through `prepare_migration`) | M-L | Medium | Big agentic win for projects with existing migration history. |
| 7.2 | Pre-deployment migration validation (target schema vs production snapshot) | M | High | Composes `compare_schemas` + shadow workflow. |
| 7.3 | Migration history table integration (read Alembic / Flyway / Diesel native tables) | S | Medium | Reads existing tooling's bookkeeping. |
| 7.4 | Zero-downtime migration cookbook | S | Medium-High | Pure docs (patterns, not code). |

## 8. AI / agent-specific

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 8.1 | Test-data factory using catalog + heuristics (`generate_test_row_for(schema, table)`) | M | Medium-High | Pairs with the shadow-migration workflow. |
| 8.2 | ✅ **Shipped.** Schema-documentation generator (Markdown table reference from catalog) | S | Medium | Sibling of `generate_schema_diagram`. |

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
| 9.3 | `cluster_vectors` — k-means cluster a vector column, return centroids + per-row labels | M | Medium-High | Exploration / segmentation tool. |
| 9.4 | `detect_vector_outliers` — flag rows whose embedding is far from any cluster centroid | S-M | Medium-High | Data quality + content moderation. |
| 9.5 | `monitor_embedding_drift` — compare distributional stats of vectors over time windows | M | Medium | Ops / model-quality monitoring. |
| 9.6 | ✅ **Shipped.** `import_vectors` — bulk-load embeddings from JSON/CSV into a pgvector `vector(N)` column; reads the declared `N` from the catalog and validates every row before any INSERT runs. Optional parallel `id_column`. | S | Medium | Sibling of `import_csv` specialised for vector columns. |
| 9.7 | `cross_table_similarity` — given a row in table A, find the k most similar rows in table B (different embedding source, same dim) | S | Medium | Useful for entity resolution / linking across tables. |
| 9.8 | `analyze_distance_metric` — recommend cosine vs L2 vs inner-product based on vector-magnitude distribution | S | Medium | Concrete advice when the user hasn't decided yet. |
| 9.9 | `monitor_index_build` — surface HNSW / IVFFlat build progress for long-running index creations | S | Medium | Lives next to `list_active_queries`; useful for big-table index work. |
| 9.10 | `migrate_vector_to_halfvec` — DDL generator to convert a `vector(N)` column to `halfvec(N)` (or `bit`) safely | S-M | Medium | Pairs with `recommend_vector_quantization`. Uses the existing shadow workflow. |

## 10. Multi-database support

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 10.1 | One MCPg server, multiple `MCPG_DATABASE_URL`s — tool-level db selector | L | Medium | Today: one server = one DSN. Multi-DB means a per-tool param, a pool-per-DB, and rethinking gates. Big lift; no concrete demand yet. |

---

## Currently deferred (no commitments)

- **Multi-database support** (10.1 above) — very ambitious;
  preferred shape today is one MCPg instance per database.
- **Backups & DR** beyond what `dump_database` /
  `restore_database` already cover — narrow audience.
- **Alembic / Flyway / Liquibase script ingestion** (7.1) — large
  surface; `validate_migration` + `prepare_migration` already
  cover the high-value reviewer workflow.

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
