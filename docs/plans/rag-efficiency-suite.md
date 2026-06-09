# RAG efficiency suite — design plan

Two cohesive, MCPg-native features that together produce an
end-to-end view of RAG quality:

1. **`analyze_vector_search_efficiency`** — cross-backend (pgvector
   HNSW / IVFFlat, turboquant) retrieval-quality report. Zero
   instrumentation cost: works against any existing vector table.
2. **`analyze_reranker_efficiency`** — cross-encoder rerank-stage
   analytics over a small caller-populated event table.

Both live in a new module `src/mcpg/rag_efficiency.py` so they share
their statistical helpers (Spearman, Kendall τ, recall, NDCG,
histogram) and the surfacing into `audit_database`. Neither is in
upstream `pgvector` or `pg_turboquant`; both are MCPg-original
synthesis.

---

## Design 1 — `analyze_vector_search_efficiency`

### The question this answers

"Is my ANN index actually pulling its weight, and which knob should I
turn?" A single function, one report shape, three backends —
turboquant, HNSW, IVFFlat — so an agent doesn't need to know which
AM it's looking at to get an actionable recommendation.

### Public API

```python
@dataclass(frozen=True, slots=True)
class RerankLiftPoint:
    candidate_multiplier: int    # 1, 2, 4, 10
    knob_name: str               # "ef_search" / "probes" / "rerank_limit"
    knob_value: int              # the per-backend value that produced the multiplier
    recall_at_k: float
    p50_latency_ms: float        # measured during the run
    p95_latency_ms: float

@dataclass(frozen=True, slots=True)
class VectorEfficiencyFinding:
    code: str                    # see "Rule codes" below
    severity: str                # GOOD / WARNING / CRITICAL
    evidence: str
    suggested_action: str        # ready-to-run SQL where applicable

@dataclass(frozen=True, slots=True)
class VectorEfficiencyReport:
    schema: str
    table: str
    column: str
    index_name: str
    backend: str                 # "hnsw" | "ivfflat" | "turboquant"
    metric: str                  # "l2" | "cosine" | "inner_product"
    sample_size: int
    k: int
    row_count_estimate: int
    dimension: int

    # Core metrics
    recall_at_k_baseline: float          # at backend-default knob value
    rerank_lift_curve: list[RerankLiftPoint]
    score_rank_correlation: float        # Spearman ρ vs. exact ranks
    score_rank_correlation_kendall: float  # Kendall τ — robust on ties

    # Cost-of-relevance
    pages_scanned_per_query_p50: float | None
    pages_scanned_per_query_p95: float | None
    pages_pruned_ratio_p50: float | None       # turboquant only (from tq_last_scan_stats)
    bytes_per_indexed_row: float | None        # index_size / row_count

    # Findings the agent should act on
    findings: list[VectorEfficiencyFinding]

async def analyze_vector_search_efficiency(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    *,
    index_name: str | None = None,       # auto-detect if None
    k: int = 10,
    sample_size: int = 30,                # capped at _MAX_SAMPLE_SIZE = 100
    candidate_multipliers: tuple[int, ...] = (1, 2, 4, 10),
    metric: str = "cosine",
    query_source: str = "in_table_sample",  # see below
) -> VectorEfficiencyReport: ...
```

### How it works (the part that isn't obvious)

**1. Backend detection.** Look up `pg_index` → `pg_am.amname` for the
named (or auto-picked) index. One of `{hnsw, ivfflat, turboquant}`;
anything else raises `VectorEfficiencyError`. The detected backend
selects the **knob axis** — what we actually sweep:

| Backend | Knob | How we set it per-query |
|---|---|---|
| HNSW | `ef_search` | `SET LOCAL hnsw.ef_search = …` |
| IVFFlat | `probes` | `SET LOCAL ivfflat.probes = …` |
| turboquant | `rerank_limit` | call `tq_rerank_candidates(…, rerank_limit=…)` |

The `candidate_multiplier` is the agent-facing abstraction; we map
it to a sensible per-backend value (e.g. for HNSW, multiplier `m` →
`ef_search = max(k * m, k + 10)`; for turboquant, `rerank_limit = k * m`).

**2. Ground truth.** For each of `sample_size` query vectors, the
exact baseline is computed by **disabling the index** (the standard
pgvector trick: use the distance *function* instead of the operator,
or `SET LOCAL enable_indexscan = off`). Cap is the same
`_MAX_SAMPLE_SIZE = 100` already in `vector_tuning.py` — reused
constant, not duplicated.

**3. Query source.**
   - `"in_table_sample"` (default): pick `sample_size` random rows
     from the table itself, use their vectors as queries, and exclude
     each query row from its own result set. Cheap, no extra storage,
     decent coverage. Same trick `vector_recall_at_k` uses.
   - `"holdout_table"`: caller supplies `schema.table.column`
     pointing at a separate query set (production query embeddings
     captured offline). Optional second arg.

**4. Latency capture.** Wrap each per-sample query in `EXPLAIN
(ANALYZE, BUFFERS, FORMAT JSON)`, pull `Execution Time` and
`Shared Hit Blocks + Shared Read Blocks`. Report p50/p95 across
the sample. For turboquant, also call `tq_last_scan_stats()`
between iterations to extract `pages_pruned` / `pages_scanned`.

**5. Spearman ρ and Kendall τ.** Computed in pure Python (no
SciPy dep) over the score sequences. Both because Spearman is
sensitive to ties (which quantized scores produce in spades) and
Kendall τ degrades more gracefully — when they disagree by more
than 0.15 it's diagnostic on its own.

**6. Findings — the rule table.** Same shape as the turboquant
advisor codes. Initial set:

| Code | Trigger | Severity | Suggested action |
|---|---|---|---|
| `baseline_recall_low` | `recall_at_k_baseline < 0.80` | CRITICAL | "raise default knob"; emits the `SET` statement |
| `rerank_lift_flat` | recall at 10× multiplier within 0.02 of baseline | WARNING | "knob is over-provisioned; lower default to save latency" — emits the lower value |
| `rerank_lift_steep` | recall at 1× < 0.70 but at 4× ≥ 0.95 | WARNING | "default knob is too tight; raise to N" |
| `ranking_degraded` | `recall ≥ 0.90` but `spearman < 0.5` | WARNING | "scores are noisy at the top — for `bits`/`m`/`lists` increase" |
| `latency_per_recall_high` | p95 / recall > backend-specific threshold | WARNING | "consider a smaller `bits` / different metric / IVF instead of HNSW for this row count" |
| `bytes_per_row_high` | index size / row count > backend-specific threshold | WARNING | "halfvec or higher quantization" |
| `pruning_ineffective` | turboquant; `pages_pruned_ratio_p50 < 0.10` | WARNING | "lists too low — consider IVF mode or raise `lists`" |

The thresholds live in module-level constants so they're easy to
tune in one place — no scatter.

### MCP tool

`analyze_vector_search_efficiency(schema, table, column, *,
index_name=None, k=10, sample_size=30, candidate_multipliers=(1,2,4,10),
metric="cosine", query_source="in_table_sample", query_table_schema=None,
query_table=None, query_column=None)`

**Gate:** read-only. No `MCPG_ALLOW_*` required. But the docstring
states explicitly: this is a **diagnostic**, not a monitoring hook —
each call burns `sample_size × (1 exact + len(candidate_multipliers)
approx)` queries, where each exact query touches the whole table.
Run it ad-hoc, not on a cron.

### Audit-database integration

A new optional category `audit_vector_indexes(driver) -> CategoryResult
| None`:
- Walks `pg_indexes` for HNSW / IVFFlat / turboquant AMs.
- For each, runs a **tiny** sweep (`sample_size=10`, multipliers
  `(1, 4)` only) — enough to flag `baseline_recall_low` and
  `rerank_lift_steep`, not enough to be expensive on a routine audit.
- Returns `None` when there are no ANN indexes in the schema — the
  category is omitted, matching how other optional categories already
  behave.

---

## Design 2 — `analyze_reranker_efficiency`

### The question this answers

"Is my cross-encoder earning its latency budget, or is it theatre?"
Cross-encoders run outside PostgreSQL — there's no way for MCPg to
capture this autonomously. So the design is **caller logs events
into a small table; MCPg owns the schema and the analytics**.

This is the same shape as `mcpg_audit.events`: tiny footprint, high
analytical leverage. The instrumentation cost is real and worth
naming up front — the docstring says so, and the tooling makes the
instrumentation step trivial.

### Schema (owned by MCPg)

```sql
CREATE SCHEMA IF NOT EXISTS mcpg_rag;

CREATE TABLE mcpg_rag.rerank_events (
    event_id            BIGSERIAL PRIMARY KEY,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    query_hash          BYTEA       NOT NULL,   -- SHA-256 of normalized query text
    retrieval_index     TEXT        NOT NULL,   -- "schema.index_name"
    retrieval_backend   TEXT        NOT NULL,   -- "hnsw" / "ivfflat" / "turboquant"
    candidate_id        BIGINT      NOT NULL,   -- caller's row id in their table
    bi_encoder_score    DOUBLE PRECISION,       -- distance from the vector index
    bi_encoder_rank     SMALLINT    NOT NULL,   -- 1-based position before rerank
    cross_encoder_score DOUBLE PRECISION NOT NULL,
    cross_encoder_rank  SMALLINT    NOT NULL,   -- 1-based position after rerank
    reranker_model      TEXT        NOT NULL,   -- "voyage-rerank-2", "cohere-rerank-3", …
    used_in_context     BOOLEAN     NOT NULL DEFAULT FALSE,
    ground_truth_relevance SMALLINT,            -- 0/1 or 0..4 graded; nullable
    extra               JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ON mcpg_rag.rerank_events (occurred_at);
CREATE INDEX ON mcpg_rag.rerank_events (query_hash);
CREATE INDEX ON mcpg_rag.rerank_events (reranker_model, occurred_at);
```

**Design notes:**
- `query_hash` (not `query_text`) is the join key for grouping
  per-query candidates. The raw text is **never** required by any
  analytic — keeps PII out of MCPg's table by default. Callers who
  want it can store it in `extra`.
- `ground_truth_relevance` nullable on purpose. Online traffic
  produces unlabeled events; offline eval sets produce labeled ones.
  Same table handles both — the analytics gate on `IS NOT NULL`
  where labels are needed.
- `extra JSONB` for caller-specific fields (latency, A/B variant,
  user_id, etc.) without schema churn.
- `retrieval_index` / `retrieval_backend` make this table joinable
  with Design 1's reports — same vector index identifies both halves
  of the pipeline.

### Setup tool

`setup_rag_telemetry()` — creates the schema, table, and indexes if
absent; idempotent.
- **Gate:** unrestricted + `MCPG_ALLOW_DDL` (mirrors every other
  schema-creating tool).
- Returns a `{schema_created, table_created, indexes_created}`
  shape so the caller knows what changed.

### Event-logging tool (optional convenience)

`log_rerank_event(query_hash, retrieval_index, retrieval_backend,
candidate_id, bi_encoder_score, bi_encoder_rank, cross_encoder_score,
cross_encoder_rank, reranker_model, used_in_context=False,
ground_truth_relevance=None, extra=None)` — one row per
(query, candidate) pair.
- **Gate:** unrestricted mode (write).
- Most callers will write events directly via their own DB client
  for throughput; this tool is for the cases where the agent itself
  is curating the eval set.

### Analytics tools

All read-only. All accept `window` (default `INTERVAL '7 days'`),
`model` (optional `reranker_model` filter), and `retrieval_index`
(optional). All gracefully return `samples_observed = 0` when the
table is empty — no errors, just empty findings.

**1. `analyze_reranker_lift(window, model=None, retrieval_index=None)`**

For each `query_hash` in window, compute:
- Spearman ρ between `bi_encoder_rank` and `cross_encoder_rank`.
- Kendall τ (same pair).

Aggregate across queries → mean, p25, p75. Output:

```python
@dataclass(frozen=True, slots=True)
class RerankerLiftReport:
    window: str
    model: str | None
    retrieval_index: str | None
    query_count: int
    mean_spearman: float
    mean_kendall: float
    p25_spearman: float
    p75_spearman: float
    interpretation: str   # "reranker actively reorders" / "reranker mostly confirms"
```

**Findings produced:**
- `reranker_idle` — `mean_kendall > 0.85` → "reranker rarely
  changes ordering; consider skipping it at this K and saving the
  latency. Likely candidates: shorter inputs, simple lookups."

**2. `analyze_topk_stability(window, k, model=None,
retrieval_index=None)`**

For each query, compute Jaccard overlap between top-K-by-bi-rank and
top-K-by-cross-rank. Aggregate.

**Findings:**
- `topk_stable` — mean Jaccard > 0.9 at K → "92% of top-K
  unchanged after rerank; reranker barely earns its place at K=N".

**3. `analyze_rerank_score_distribution(window, model=None)`**

Histogram of `cross_encoder_score` with 20 buckets. Also computes the
share of scores in the top decile — clustering at the top is a known
failure mode for some commercial rerankers.

**Findings:**
- `score_clustering` — top-decile share > 0.5 → "more than half of
  rerank scores cluster in the top 10% of the range; this reranker
  isn't discriminating. Calibrate or switch model."

**4. `analyze_rerank_ndcg(window, k, model=None, retrieval_index=None)`**

Gated on `ground_truth_relevance IS NOT NULL`. Computes NDCG@K under
two orderings (bi-rank and cross-rank), reports both plus the lift.
Also reports the count of labeled queries — small samples get a
`low_sample` finding.

**Findings:**
- `rerank_hurts_ndcg` — `ndcg_after < ndcg_before - 0.02` → CRITICAL.
  "Reranker is actively making it worse on labeled data. Investigate
  model/version/prompt."
- `rerank_lifts_ndcg` — `ndcg_after > ndcg_before + 0.05` → GOOD
  evidence the reranker is doing real work.

**5. `recommend_rerank_strategy(window, retrieval_index=None)`**

The roll-up advisor. Reads the four analytics above (one window, one
optional retrieval index), produces a single ranked recommendation:
- "Reranker is theatre at K=5 — `topk_stable` + `reranker_idle`
  both fire. Save ~Xms p95 by skipping it for queries shorter than N
  tokens." (X computed from `extra.latency_ms` if present)
- "Switch reranker: NDCG lift < 0.02 and score_clustering present."
- "Reranker is critical: NDCG lift > 0.08; keep it."

### Audit-database integration

A new optional category `audit_rag_pipeline(driver) -> CategoryResult
| None`:
- Returns `None` when `mcpg_rag.rerank_events` doesn't exist (the
  common case — keeps the category invisible until the user opts in).
- Otherwise runs `recommend_rerank_strategy` over the last 7 days
  and turns each finding into a `MetricResult` for the scorecard.

---

## Shared module: `src/mcpg/rag_efficiency.py`

One file, two designs. Layout:

```
rag_efficiency.py
├── _stats.py-equivalent helpers (Spearman, Kendall τ, NDCG, histogram,
│   Jaccard) — module-private, pure Python, tested directly.
├── Vector-search-efficiency:
│   ├── public dataclasses (RerankLiftPoint, VectorEfficiencyFinding,
│   │   VectorEfficiencyReport)
│   ├── _detect_backend, _knob_for_backend, _set_knob_local
│   ├── _exact_baseline, _approx_with_knob, _explain_costs
│   ├── analyze_vector_search_efficiency
│   └── audit_vector_indexes (scorecard adapter)
└── Reranker-efficiency:
    ├── public dataclasses (RerankerLiftReport, ScoreDistributionReport,
    │   NDCGReport, RerankRecommendation)
    ├── _SETUP_DDL (the CREATE TABLE / CREATE INDEX statements)
    ├── setup_rag_telemetry, log_rerank_event
    ├── analyze_reranker_lift / _topk_stability /
    │   _score_distribution / _ndcg
    ├── recommend_rerank_strategy
    └── audit_rag_pipeline (scorecard adapter)
```

Statistical helpers are shared because both designs use ρ / τ /
histogram. Zero external deps — pure Python, fits inside the existing
"no SciPy at runtime" stance of the codebase.

---

## Guardrails (apply to every phase)

Same patterns as the turboquant plan, repeated here for the new
helpers:

1. **No SciPy / NumPy at runtime.** Spearman / Kendall / NDCG /
   Jaccard implemented from scratch; tested directly with known
   fixtures (mononic series, reversed series, all-ties series).
2. **Identifier safety** on every schema/table/column/index name
   reaching SQL text. Reuse `_IDENTIFIER` + `_quoted` from
   `vector_tuning.py` (lift into a shared util if it migrates).
3. **Sample-size cap** reused from `vector_tuning._MAX_SAMPLE_SIZE
   = 100`. Single source of truth.
4. **`SET LOCAL`, not `SET`** for any per-query knob change, so
   the connection's GUC state is restored at transaction end.
5. **Catalog pre-flight on `index_name`** in Design 1 — confirm the
   index is actually one of the three AMs we support before doing
   anything. Same reasoning as the turboquant maintenance preflight.
6. **PII boundary:** Design 2's schema **does not** store query
   text. The `query_hash` column is the join key. Callers may add
   text in `extra` if they accept the responsibility.
7. **Audit-trail categories are opt-in by absence:** both
   `audit_vector_indexes` and `audit_rag_pipeline` return `None`
   when their prerequisites aren't met. No new failures introduced
   into an existing audit run.

---

## Phasing

Two designs, four phases. Vector first because it needs zero
instrumentation; rerank second because it needs a schema and adoption.

### Phase A — `analyze_vector_search_efficiency` core ✅ shipped

- Module `src/mcpg/rag_efficiency.py` with the stat helpers
  (Spearman, Kendall tau-b with ties, recall@k, average-rank
  helper, percentile interpolation).
- Tool `analyze_vector_search_efficiency` registered under
  `_register_rag_efficiency`.
- Five rule codes shipped (per the table at the top of this doc).
- Turboquant arm composes with `mcpg.turboquant`'s
  `turboquant_rerank_candidates` + `get_turboquant_last_scan_stats`
  helpers (which is why TQ-5 was un-deferred first).
- `inner_product` metric deferred to a follow-up — the pgvector
  operator form (`<#>`, negated) and function form
  (`inner_product()`, raw) order opposite directions, requiring
  careful handling beyond Phase A's no-speculation discipline.
- Branch: `claude/rag1-vector-efficiency`.

### Phase B — `audit_database` integration ✅ shipped

- `audit_vector_indexes` category wired into `audit_database` via
  the same lazy-import pattern as `audit_turboquant_indexes`.
- Sample budget: 10 samples + (1, 4) multipliers = 30 queries per
  index. Bounded even on databases with several ANN indexes.
- Composite-PK and PK-less tables skipped silently (audit
  reports what it can, surfaces the rest as GOOD baseline
  metrics). Per-index failures isolated — one raise doesn't
  sink the audit.
- Branch: `claude/rag2-vector-audit`.

### Phase C — Design 2 schema + logging ✅ shipped

- `setup_rag_telemetry` (DDL-gated) + `log_rerank_event`
  (write-gated) + the `mcpg_rag.rerank_events` schema with three
  indexes (`occurred_at`, `query_hash`, composite
  `(reranker_model, occurred_at)`).
- Idempotency: catalog probes before each `CREATE … IF NOT EXISTS`
  let the setup result report first-run vs no-op.
- DDL runs through `Database.run_unmanaged` so each statement
  commits independently (`CREATE SCHEMA` can't be re-issued
  inside a failed transaction).
- Bool-as-int subclass trap caught explicitly on the rank
  validators — same pattern as TQ-4's `concurrently`.
- Branch: `claude/rag3-rerank-schema`.

### Phase D — Design 2 analytics + advisor + audit hook ✅ shipped

- Four analytics tools + `recommend_rerank_strategy` +
  `audit_rag_pipeline` category in `audit_database`.
- Five rule codes: `reranker_idle`, `topk_stable`,
  `score_clustering`, `rerank_hurts_ndcg`, `rerank_lifts_ndcg`.
- Pure-Python `_jaccard`, `_ndcg_at_k`, `_histogram` helpers added
  to `mcpg.rag_efficiency` alongside the existing Spearman /
  Kendall / percentile helpers from Phase A.
- Branch: `claude/rag4-rerank-analytics`.

### Phase E — adaptive thresholds ✅ shipped

**Motivation.** Phase A ships with hardcoded rule thresholds
(`baseline_recall_low` at 0.80, `pruning_ineffective` at 0.10, …)
picked from intuition rather than data. They're fine defaults but
say nothing about *this* deployment's normal range. An adaptive
framework replaces them with corpus-percentile thresholds learned
from accumulated observations of the same function — "you're in the
bottom 5% of recall@10 across HNSW indexes in this database" is more
actionable than "you're below 0.80".

**Shape.** Mirrors Design 2's caller-populated-table pattern:

```sql
CREATE TABLE mcpg_rag.efficiency_observations (
    observation_id BIGSERIAL PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_name TEXT, table_name TEXT, column_name TEXT, index_name TEXT,
    backend TEXT NOT NULL,
    metric TEXT NOT NULL,
    k INT NOT NULL,
    sample_size INT NOT NULL,
    recall_baseline DOUBLE PRECISION,
    rerank_lift_curve JSONB,
    spearman DOUBLE PRECISION,
    kendall DOUBLE PRECISION,
    pages_pruned_ratio_p50 DOUBLE PRECISION,
    duration_seconds DOUBLE PRECISION,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb
);
```

**Tools.** `setup_efficiency_observations()` (DDL),
`record_efficiency_observation(report)` (write),
`recommend_efficiency_thresholds(window, backend, metric, k)`
(read — computes corpus percentiles like recall-low = p10,
lift-flat = p10 of the "10x recall − baseline" deltas).

**Integration.** `_evaluate_rules` already takes a plain `dict` of
metrics; Phase E injects a `thresholds: dict[str, float]` argument,
falling back to the hardcoded defaults when the corpus has fewer
than N observations. Single insertion point, no downstream change.

**Branch:** `claude/rag5-adaptive-thresholds`. **Depends on:** Phase
A (the function whose outputs feed the observation table).

---

**Sequencing:** A → B and C → D are independent tracks. Could land
A → C → B → D. Phase D depends only on C; Phase B depends only on A.
Phase E depends only on A.
No phase touches the same files as another except `tools.py` (one
new registrar each, adjacent-block conflict only) and `CHANGELOG.md`
(top-of-section).

---

## Out of scope (named so they don't drift in)

- **Online monitoring / continuous evaluation.** Both designs are
  diagnostic. A "watch this rerank model for drift and alert"
  feature is a separate, larger thing — needs a worker, a state
  table, and notification surface.
- **Reranker A/B test harness.** The schema supports it (`extra`
  can carry the variant), but the orchestration belongs in the app,
  not in MCPg.
- **Bi-encoder training data export.** Tempting (every logged event
  is a labeled pair), but a different feature and easy to add later
  as a simple SQL view.

---

## Why this is differentiated

- **Cross-backend retrieval-quality report in one call** — no
  existing tool does HNSW + IVFFlat + turboquant under one schema.
  Most projects measure recall on one index in isolation.
- **Rerank analytics inside the database** — every commercial RAG
  observability tool stores this in their own SaaS. Keeping it in
  PostgreSQL means it composes with every other MCPg tool: join
  with `mcpg_audit.events`, filter by user, slice by tenant. The
  data stays where the rest of the app's data lives.
- **The two halves compose.** Design 1's findings tell you *the
  index is fine but rerank-limit is too tight*; Design 2's
  findings tell you *the reranker doesn't reorder anything*. Run
  both and you have an end-to-end RAG quality picture nobody else
  is producing from inside the data plane.
