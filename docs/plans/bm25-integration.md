# BM25 sparse-search integration — planning doc

**Target:** `pg_search` (ParadeDB, Tantivy-based) — selected after a
three-way comparison. The other two candidates (`pg_textsearch` from
Tiger Data, `vchord_bm25` from VectorChord) are deferred with
documented return conditions.

## 1. Decision rationale

**Why `pg_search` first.** Concrete reasons, in priority order:

1. **PG version coverage matches MCPg's existing footprint** — 14, 15,
   16, 17, 18 supported with pre-built binaries. `pg_textsearch` is
   PG 17/18-only today; `vchord_bm25` is pre-1.0 and ships primarily
   as Docker images.
2. **Stable surface, deep release cadence.** 233 releases, latest
   v0.24.0 (June 2026), ~8.9k stars on the parent paradedb monorepo.
   The v2 API (`pdb.*` schema) is the documented stable contract.
3. **Documented hybrid with pgvector.** Their "Hybrid Search Missing
   Manual" blog gives an explicit pattern:
   `0.7 * paradedb.score_bm25(id) + 0.3 * paradedb.score_vector(id)`.
   MCPg's RAG efficiency suite (`analyze_vector_search_efficiency`)
   composes naturally with this.
4. **Compositional richness.** `pdb.score`, `pdb.snippet`,
   `pdb.snippets`, `pdb.agg`, `pdb.more_like_this`, `pdb.regex`,
   `pdb.parse` — the surface is rich enough to wrap as distinct
   MCPg tools, not just one omnibus `bm25_search`.
5. **Existing MCP integration upstream.** ParadeDB ships their own
   MCP integration; MCPg's wrapper is incremental rather than novel.

**Why `pg_textsearch` is deferred.** Strong second candidate — zero
Rust at install, native PG full-text-config compatibility (29+
languages including zhparser, Jieba via existing dictionaries),
delta-encoding for ~41% smaller indexes than GIN. But PG 17/18-only
today, and **no phrase queries** (no positions stored). Returns to
the front of the queue when PG 14–16 support lands or when MCPg's
TimescaleDB integration motivates the Tiger Data lineage match.

**Why `vchord_bm25` is deferred.** Pre-1.0 (0.3.0 latest, Dec 2025).
Strong CJK tokenizer story (Jieba, Lindera, BERT, custom-model
trainer with trigger), but: split-extension install surface
(`pg_tokenizer` + `vchord_bm25`), no documented hybrid pattern with
pgvector, ~370 stars. Returns when CJK / multilingual becomes a
top-line MCPg goal.

## 2. Unknowns to resolve before phase 1 starts

A focused investigation (one agent run, same shape as the
pg_turboquant pre-implementation read) needs to confirm:

1. **`pdb.*` function return types verbatim** from
   `pg_search/sql/` source files. The current research run pulled
   operator names and argument signatures from the v2 API blog post
   — not from `CREATE FUNCTION` declarations. Examples needing
   pinning:
   - `pdb.score(key_field) → ?` (float4? float8?)
   - `pdb.snippet(field) → ?`
   - `pdb.snippets(field, start_tag text, end_tag text, max_num_chars int) → ?`
   - `pdb.agg(json_spec)` — exact input/output JSONB shape.
   - `pdb.more_like_this(doc_id)` — argument type, return shape.
   - The non-standard operators (`|||`, `&&&`, `###`, `===`,
     `##`, `##>`) — operator-class registrations.
2. **The `bm25` index access method's `WITH (...)` options** —
   schema-config JSON, tokenizer selection, field-config. Verbatim
   `CREATE INDEX … USING bm25 (…) WITH (…)` examples from the
   integration tests are the source of truth.
3. **Pre-built binary distribution coverage.** Which PG distros
   are covered out-of-box (Tigerdata/Timescale images, Neon, RDS,
   self-managed). Bare-source installs need Rust + pgrx.
4. **AGPL-3.0 redistribution implications** for MCPg's
   downstream consumers. The `pg_search` extension is AGPL; MCPg's
   wrapper does not statically link against it (PG extensions run
   in-process but the license boundary is at the PG dynamic-load
   layer). This needs an explicit decision from the project owner
   before wide adoption.

These unknowns map 1-to-1 to a TQ-style "post-investigation"
agent run. Same discipline: read the upstream SQL definitions
verbatim, don't infer shapes from prose.

## 3. Guardrails (apply to every phase)

Same patterns proven in the pg_turboquant integration (see
`pg_turboquant-integration.md` for the canonical exposition):

1. **Module:** a single new `src/mcpg/pg_search.py`. Cohesive
   cluster — keeps `tools.py` conflicts to adjacent-block only.
2. **Presence check** on every public function via
   `extension_installed(driver, "pg_search")`. Reads return `[]` /
   `None` when absent; writes / DDL raise `PgSearchError`.
3. **Identifier safety:** schema / table / column / index names go
   through the established `_validate_identifier` + `_pg_quote_ident`
   helpers. The wrappers reuse the helpers from `mcpg.turboquant`
   or `mcpg.vector_tuning` (lifted to a shared util when the
   third copy is needed — not before).
4. **No-speculation discipline.** Same as the turboquant phases.
   Argument types and return shapes come from `pg_search/sql/`
   verbatim; anything not documented in source is deferred to a
   future phase with a documented return condition.
5. **Extension allowlist:** add `pg_search` to
   `ENABLEABLE_EXTENSIONS` in `mcpg.extensions` so the existing
   `enable_extension` tool can install it.
6. **Gating:**
   - Read tools (search, snippets, aggregations, more_like_this)
     → no gate.
   - Write tools (none anticipated — `pg_search` is read-heavy).
   - DDL tools (`CREATE INDEX … USING bm25`, `REINDEX`,
     `setup_pdb_schema`) → unrestricted + `MCPG_ALLOW_DDL`.

## 4. Phasing

Five phases proposed, mirroring the turboquant cadence.

### Phase BM-1 — observability + extension presence (1 PR)

Smallest first slice. Goal: any MCPg deployment can confirm
whether `pg_search` is installed, list every BM25 index in the
database, read each index's catalog metadata.

- `list_pg_search_indexes(driver) -> list[PgSearchIndexInfo]`
- `get_pg_search_index_metadata(driver, schema, index)` —
  surface `pg_class.reloptions` (the `WITH (…)` config) parsed
  into a typed dict, analogous to TQ-1's `index_options`.
- Extension presence check via `extension_installed`.
- `pg_search` added to `ENABLEABLE_EXTENSIONS`.
- Branch: `claude/bm1-observability`.

### Phase BM-2 — search execution (1 PR)

Wraps the `@@@` operator and the core `pdb.score` / `pdb.snippet`
projection helpers as MCPg tools.

- `pg_search_run(driver, schema, table, column, query, *, limit, return_snippets=False)`
  → `list[PgSearchHit]` with id, score, optional snippet.
- `pg_search_more_like_this(driver, schema, table, column, document_id, *, limit)`
  → `list[PgSearchHit]`.
- `pg_search_parse_query(driver, query_string, *, lenient=False)`
  — surfaces the parsed query structure for debugging.

Validation: every identifier (schema/table/column) through
`_validate_identifier`; `limit` bounded `1..10_000`. `query` and
`document_id` go in as bound params — never spliced into SQL.

- Branch: `claude/bm2-search-execution`.

### Phase BM-3 — hybrid-search composition (1 PR)

Composes BM25 + pgvector into one MCPg tool, mirroring the
ParadeDB "Hybrid Search Missing Manual" pattern.

- `hybrid_bm25_vector_search(driver, schema, table, *, query_text,
  query_vector, bm25_column, vector_column, vector_metric, k,
  bm25_weight=0.7, vector_weight=0.3)`
  → `list[HybridHit]` with combined score, plus the per-source
  scores for transparency.

Composes with the RAG efficiency suite — once shipped,
`analyze_vector_search_efficiency` can be re-used to tune the
vector arm of a hybrid query.

- Branch: `claude/bm3-hybrid-search`.

### Phase BM-4 — DDL (1 PR)

- `create_pg_search_index(driver, schema, table, columns,
  index_name, *, text_config="english", k1=1.2, b=0.75,
  tokenizer="unicode_words")` — builds
  `CREATE INDEX … USING bm25 (...) WITH (...)`.
- `reindex_pg_search_index(driver, schema, index, *, concurrently=True)`
  with the same pre-flight as `reindex_turboquant_index`
  (catalog lookup confirming the index uses the `bm25` AM).

Allowlist-validated `text_config`, `tokenizer`, `k1`/`b` bounds.
Gated under unrestricted + `MCPG_ALLOW_DDL`.

- Branch: `claude/bm4-ddl`.

### Phase BM-5 — advisor + audit category (1 PR)

`recommend_pg_search_maintenance` + `audit_pg_search_indexes`
(analogous to TQ-2's `recommend_turboquant_maintenance` +
`audit_turboquant_indexes`). Rules sourced from documented
signals only; threshold list TBD after Phase BM-1 reveals what
metadata `pg_search` exposes.

- Branch: `claude/bm5-audit`.

## 5. Sequencing

1. **Investigation run** (read `pg_search/sql/` and resolve the four
   unknowns in §2) — single focused agent invocation, no code.
2. **BM-1** — observability. No dependencies.
3. **BM-2** — search execution. Depends on BM-1's
   `PgSearchIndexInfo` dataclass.
4. **BM-3** — hybrid search. Depends on BM-2 (uses
   `pg_search_run` internally) and pgvector (already integrated).
5. **BM-4** — DDL. Independent of BM-2/BM-3.
6. **BM-5** — advisor + audit category. Depends on BM-1.

BM-2/BM-4 and BM-3/BM-4 could land in parallel.

## 6. Out of scope (named so they don't drift in)

- **Custom tokenizer registration.** `pg_search` supports custom
  Tantivy tokenizers; wrapping their registration touches the
  Rust build chain and is much heavier than the SQL surface.
  Revisit only if a use case surfaces.
- **`pg_search` background-merge tuning.** Surfaced via GUCs;
  out-of-scope until performance reports motivate it.
- **AGPL-3.0 license analysis.** Recommendation noted in §2 —
  the project owner needs to decide explicitly before BM-1
  starts. This doc doesn't attempt that analysis.
- **`pg_textsearch` and `vchord_bm25` wrappers.** Deferred per
  §1. Documented return conditions: PG 14–16 support for
  `pg_textsearch`; 1.0 release + hybrid-pattern docs for
  `vchord_bm25`.

## 7. Why this is differentiated

- **First MCP server with first-class BM25 + pgvector hybrid
  tooling.** Most projects bolt BM25 on with a separate search
  service (Elasticsearch, Tantivy via Lucene); keeping it inside
  PostgreSQL means hybrid queries compose with every other MCPg
  tool — audit trails, row-level security, multi-tenancy.
- **One coherent advisor surface across HNSW / IVFFlat /
  turboquant / pg_search.** Once BM-5 lands, `audit_database`
  reports on every retrieval-relevant index type in the cluster.
- **Composes with the RAG efficiency suite.** The hybrid-search
  arm (BM-3) is the natural input to a future RAG-F that
  evaluates hybrid quality — recall@k, NDCG, reranker lift — all
  inside PostgreSQL.
