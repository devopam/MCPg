# BM25 sparse-search integration — planning doc

> **Status: ✅ Shipped — archived planning doc.** The `pg_search`
> (ParadeDB BM25) integration this document planned has landed in full
> (BM-1…BM-5): `pg_search_run`, `pg_search_more_like_this`,
> `pg_search_parse_query`, `hybrid_bm25_vector_search`,
> `create_pg_search_index`, `reindex_pg_search_index`, plus the
> observability/advisor tools — see the
> [tool index](../tools.md#tool-index-252-tools). This doc is kept for
> its design rationale and the deferred-alternatives (`pg_textsearch`,
> `vchord_bm25`) return conditions; it is no longer a live roadmap.
> Current gaps live in [feature-shortlist.md](../feature-shortlist.md).

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
2. **Stable surface, deep release cadence** (as of 2026-06). Many
   releases on the parent ParadeDB monorepo with a healthy star
   count and active commit history. The v2 API (`pdb.*` schema) is
   the documented stable contract. Numbers shift; the durable
   signal is the active release cadence on a stable schema.
3. **Documented hybrid with pgvector.** ParadeDB's "Hybrid Search
   Missing Manual" documents the v2 pattern — `pdb.score(key)` from
   the BM25 side, weighted-summed against a pgvector distance
   expression. The exact arithmetic (linear blend, RRF, or a
   tunable weight knob) is the remaining open question after the
   §2 investigation — see §2.5; gates BM-3 only, not BM-1/BM-2.
   MCPg's RAG efficiency suite
   (`analyze_vector_search_efficiency`) composes naturally with
   either shape.
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

## 2. Pre-implementation investigation — results

The focused investigation agent (one run, same shape as the
pg_turboquant pre-implementation read) has completed. Items 1-4
below are **resolved** with verbatim upstream-source citations;
item 5 is deferred to BM-3 (does not block BM-1/BM-2/BM-4).

### 2.1 `pdb.*` function signatures — resolved

Verbatim from `pg_search/src/bootstrap/` Rust declarations
(pgrx generates the `CREATE FUNCTION` SQL from these):

- `pdb.score(anyelement) → float4` — scores the matching row.
- `pdb.parse(query_string text, lenient bool, conjunction_mode bool) → pdb.query`
  — third arg `conjunction_mode` was not surfaced in the v2 blog.
- `pdb.regex(pattern text) → pdb.query`.
- `pdb.more_like_this(anyelement, fields jsonb DEFAULT NULL, min_doc_frequency int4 DEFAULT NULL, max_doc_frequency int4 DEFAULT NULL, min_term_frequency int4 DEFAULT NULL, max_query_terms int4 DEFAULT NULL, min_word_length int4 DEFAULT NULL, max_word_length int4 DEFAULT NULL, boost_factor float4 DEFAULT NULL, stop_words text[] DEFAULT NULL) → pdb.query`
  — nine optional args (one `fields` jsonb + eight tuning knobs),
  all defaulted. ~~BM-2's wrapper exposes the document-identifier
  arg and `limit`; the eight tuning knobs are out-of-scope for the
  first wrapper pass and deferred to a future phase.~~ **Landed in
  full via #91 (post-BM-2 follow-up):** every kwarg is now exposed
  as an optional Python kwarg on `pg_search_more_like_this`. Omitted
  kwargs are not mentioned in the rendered SQL, so upstream's
  defaults apply unchanged for callers who don't tune.
- `pdb.agg(aggregation_spec jsonb, solve_mvcc bool DEFAULT true) → jsonb`
  — input is the Tantivy aggregation JSON, output is the result
  JSON.
- **Bonus query-builder helpers** discovered during the read
  (not in the original §2 list but useful for richer wrappers
  later): `pdb.all()`, `pdb.boolean(...)`, `pdb.boost(query, factor)`,
  `pdb.const_score(query, score)`, `pdb.term_set(...)`. Listed in
  §6 "out of scope (until needed)" — usable surface, but BM-2's
  `pg_search_parse_query` covers the common path.

**Snippet source — resolved (post-BM-0 follow-up investigation).**
The first sweep missed `pdb.snippet` / `pdb.snippets` because they
do not live in `pg_search/src/bootstrap/`. A targeted follow-up
agent located them in
`pg_search/src/postgres/customscan/basescan/projections/snippet.rs`
(paradedb/paradedb@`8bb9a64`). Verbatim signatures:

- `pdb.snippet(field anyelement, start_tag text DEFAULT '<b>',
  end_tag text DEFAULT '</b>', max_num_chars int4 DEFAULT 150,
  "limit" int4 DEFAULT NULL, "offset" int4 DEFAULT NULL) → text`
- `pdb.snippets(field anyelement, start_tag text DEFAULT '<b>',
  end_tag text DEFAULT '</b>', max_num_chars int4 DEFAULT 150,
  "limit" int4 DEFAULT NULL, "offset" int4 DEFAULT NULL,
  sort_by text DEFAULT 'score') → text[]`

Both are `#[pg_extern(stable, parallel_safe)]` Rust functions; the
pgrx-generated SQL declarations register them under schema `pdb`.
Two independent functions, not wrappers of each other —
`pdb.snippets` adds `sort_by` and returns the multi-snippet array.

**Caveat.** These are pgrx stubs marked
`#[allow(unused_variables)]`; the actual highlight generation
happens during custom-scan projection rewriting (see
`pg_search/src/postgres/customscan/projections.rs` →
`tantivy::snippet::SnippetGenerator`). Calling `pdb.snippet(...)`
outside a `pg_search`-driven SELECT executes the stub, not real
highlight code. BM-2's `pg_search_run` wires the snippet
projection together with the `@@@` predicate so this is
transparent for callers; bare-call wrappers (if ever needed) must
document the constraint.

`pdb.snippet_positions(field anyelement, "limit" int4, "offset"
int4) → int[][]` also lives in the same file via an explicit
`sql = r#"..."#` override. Deferred — the `int[][]` return shape
needs extra marshaling and there's no current MCPg consumer for
character positions.

### 2.2 `bm25` index `WITH (...)` options — resolved

Verbatim from `pg_search/src/api/index.rs` `IndexOptions` struct.
Thirteen documented options:

1. `key_field` (required) — primary-key column.
2. `text_fields` (jsonb) — per-text-field tokenizer/analyzer config.
3. `numeric_fields` (jsonb).
4. `boolean_fields` (jsonb).
5. `json_fields` (jsonb).
6. `range_fields` (jsonb).
7. `datetime_fields` (jsonb).
8. `layer_sizes` (text) — segment-merge tier sizes.
9. `background_layer_sizes` (text) — async-merge tier sizes.
10. `target_segment_count` (int).
11. `mutable_segment_rows` (int).
12. `sort_by` (text) — pre-sorted segment hint.
13. `search_tokenizer` (jsonb) — index-wide default tokenizer.

BM-4's `create_pg_search_index` exposes the small subset MCPg
operators are likely to want (per-column text config, k1/b
analogue via `text_fields`); the rest are reachable via a generic
`WITH (…)` passthrough or deferred to a tuning-helper tool.

### 2.3 Pre-built binary distribution coverage — resolved

ParadeDB ships pre-built `pg_search` binaries for:

- **Debian/Ubuntu** (.deb), **RHEL/CentOS/Rocky/Alma** (.rpm),
  **Arch** (.pkg), **macOS** (homebrew tap).
- **Docker** — `paradedb/paradedb` image.
- **Neon** (AWS-region only, via Neon's extension marketplace).

**Not** available out-of-the-box on **AWS RDS**, **Google Cloud
SQL**, **Azure Database for PostgreSQL**, or **Tiger Data /
Timescale Cloud**. Self-managed PG and Docker are the broad
deployment story; managed-PG operators need to either run
ParadeDB's image or compile from source (Rust + pgrx).

MCPg's wrappers are unaffected — `enable_extension` falls through
to a clear "not available on this server" error when the binary
isn't installed.

### 2.4 AGPL-3.0 redistribution implications — resolved

**Decision (project owner, 2026-06):** ship the wrappers; document
the operator-side AGPL obligations clearly in `README.md`.

Rationale and scope:

- MCPg's source remains MIT. Wrappers are arm's-length: SQL-level
  calls from a Python process to a PG-loaded extension, no static
  or dynamic linking into MCPg itself.
- MCPg-the-project is therefore not a derivative work of
  `pg_search`. Operators who deploy `MCPg + pg_search` over a
  network are subject to AGPL-3.0's network clause (typically:
  offer source of `pg_search` and any modifications to users).
- README §License now carries an explicit "Wrapped extensions —
  licenses you should know about" matrix with the AGPL-3.0
  callout for `pg_search`. Operators with redistribution models
  incompatible with AGPL pick a different BM25 implementation
  (this doc's §1 lists alternatives).

### 2.5 v2 hybrid-search arithmetic — resolved

A focused follow-up agent (2026-06-10) pinned the canonical v2
arithmetic from two living upstream sources:

- **Blog post** (2025-10-22): "Hybrid Search in PostgreSQL: The
  Missing Manual" — the canonical narrative since the dedicated
  docs page was removed.
- **`tests/tests/documentation.rs::hybrid_search`** — the
  source-of-truth test that the removed docs page used to embed.

Both agree on **Reciprocal Rank Fusion (RRF)** with the formula
`sum(1.0 / (k + rank))` per source, summed via UNION ALL + GROUP
BY. The literal `k = 60` constant matches both sources. Equal
weights are the default; the blog's weighted variant uses literal
float multipliers (`bm25_weight * 1.0/(k+rank)` + `vector_weight *
1.0/(k+rank)`). There is **no** `paradedb.score_hybrid` /
`paradedb.rank_hybrid` helper function in v2 (GitHub code search
returned zero hits on `main`) — operators write the CTE inline.

**MCPg's BM-3 wrapper ships RRF** as the documented default. The
UNION ALL + GROUP BY SUM form is simpler than the test's FULL OUTER
JOIN + COALESCE form; the arithmetic is identical. The wrapper
exposes `k`, `bm25_weight`, `vector_weight`, `per_leg_limit`,
`distance_op` (allowlist `<=>` / `<->` / `<#>`), and `final_limit`
as kwargs. Linear blend is **not** offered because there is no
upstream v2 source for min/max normalized linear blend — shipping
it would be speculation.

**Caveat — docs page removed.** The hybrid guide on
`docs.paradedb.com` returns HTTP 404 as of 2026-06-10 (the
`.prettierignore` still lists `docs/documentation/guides/hybrid.mdx`
but the file is gone). The blog post is the current canonical
reference; future ParadeDB versions may ship a more formal
hybrid-search API (the docs-page removal suggests the public
surface is in flux). MCPg's wrapper docstring cites both the
blog URL with retrieval date and the test path so future
maintainers can re-verify when upstream stabilizes.

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
- Branch (proposed): `claude/bm1-observability`.

### Phase BM-2 — search execution (1 PR)

Wraps the `@@@` operator and the core `pdb.score` / `pdb.snippet`
projection helpers as MCPg tools.

`pg_search` indexes can cover **one or many** columns (or the
entire table). The wrappers reflect that — `columns` is a
`list[str] | None`, where `None` means "search the whole index".
The single-column case is just `columns=["body"]`.

- `pg_search_run(driver, schema, table, query, *, columns=None, limit, return_snippets=False)`
  → `list[PgSearchHit]` with id, score, optional snippet.
- `pg_search_more_like_this(driver, schema, table, document_id, key_field, *, limit, fields=None, min_doc_frequency=None, max_doc_frequency=None, min_term_frequency=None, max_query_terms=None, min_word_length=None, max_word_length=None, boost_factor=None, stop_words=None)`
  → `list[PgSearchHit]`. Tuning kwargs landed via #91; when
  omitted, upstream's defaults apply.
- `pg_search_parse_query(driver, query_string, *, lenient=False)`
  — surfaces the parsed query structure for debugging.

Validation: every identifier (schema/table/each entry in `columns`)
through `_validate_identifier`; `limit` bounded `1..10_000`.
`query` and `document_id` go in as bound params — never spliced
into SQL.

- Branch (proposed): `claude/bm2-search-execution`.

### Phase BM-3 — hybrid-search composition (1 PR)

Composes BM25 + pgvector into one MCPg tool, mirroring the
ParadeDB "Hybrid Search Missing Manual" pattern.

- `hybrid_bm25_vector_search(driver, schema, table, *, query_text,
  query_vector, vector_column, vector_metric, k,
  bm25_columns=None, bm25_weight=0.7, vector_weight=0.3)`
  → `list[HybridHit]` with combined score, plus the per-source
  scores for transparency.

`bm25_columns=None` searches the entire BM25 index (ParadeDB's
default behavior — the index can cover multiple columns or the
whole table). Pass an explicit list to restrict to a subset.

Composes with the RAG efficiency suite — once shipped,
`analyze_vector_search_efficiency` can be re-used to tune the
vector arm of a hybrid query.

- Branch (proposed): `claude/bm3-hybrid-search`.

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

- Branch (proposed): `claude/bm4-ddl`.

### Phase BM-5 — advisor + audit category (1 PR)

`recommend_pg_search_maintenance` + `audit_pg_search_indexes`
(analogous to TQ-2's `recommend_turboquant_maintenance` +
`audit_turboquant_indexes`). Rules sourced from documented
signals only; threshold list TBD after Phase BM-1 reveals what
metadata `pg_search` exposes.

- Branch (proposed): `claude/bm5-audit`.

## 5. Sequencing

1. **Investigation run** — done (§2.1–§2.4 resolved; §2.5 deferred
   to BM-3). Results landed in this doc as the BM-0 checkpoint PR.
2. **BM-1** — observability. No dependencies. Next slice.
3. **BM-2** — search execution. Depends on BM-1's
   `PgSearchIndexInfo` dataclass. Confirms `pdb.snippet` source
   location before exposing `return_snippets=True`; otherwise ships
   without snippet support.
4. **BM-3** — hybrid search. Depends on BM-2 (uses
   `pg_search_run` internally), pgvector (already integrated), and
   the §2.5 arithmetic decision.
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
- **Bonus `pdb.*` query-builder helpers.** `pdb.all`,
  `pdb.boolean`, `pdb.boost`, `pdb.const_score`, `pdb.term_set`
  (discovered during §2.1). Usable surface, but BM-2's
  `pg_search_parse_query` covers the common path. Wrap when a
  concrete use case appears.
- **`pdb.more_like_this` tuning args.** ~~Eight defaulted knobs~~
  **Landed in #91 (post-BM-2 follow-up).** All nine documented
  upstream args (`fields` jsonb, `min_doc_frequency`,
  `max_doc_frequency`, `min_term_frequency`, `max_query_terms`,
  `min_word_length`, `max_word_length`, `boost_factor`,
  `stop_words`) are now optional kwargs on
  `pg_search_more_like_this`. Omitted kwargs are not mentioned in
  the SQL so upstream's defaults apply unchanged.
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
