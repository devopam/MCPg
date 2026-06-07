# pg_turboquant integration — phased plan

**Extension:** [mayflower/pg_turboquant](https://github.com/mayflower/pg_turboquant/)
(MIT, PG 16–17, depends on `pgvector` 0.8.1)

**What it is:** a custom ANN index access method (`USING turboquant`)
over pgvector `vector` / `halfvec` columns, with SIMD scoring,
quantization, optional IVF, and an on-demand delta-tier compaction
step. No new tables, views, GUCs, background workers, or cron jobs.

**SQL surface to wrap** (from the upstream README/sources):

| Callable | Kind | Purpose |
|---|---|---|
| `tq_index_metadata(index regclass)` | read | algorithm version, quantizer family, sketch kind, fast-path eligibility, capability flags, delta state |
| `tq_index_heap_stats(index regclass)` | read | exact heap row counts for the index |
| `tq_last_scan_stats()` | read | backend-local JSON: score mode, SIMD kernel, scan orchestration, page-pruning counters from the most recent TurboQuant scan |
| `tq_recommended_query_knobs(...)` | read | tuning recommendations for a query |
| `tq_approx_candidates(...)` | read | approximate retrieval, no rerank |
| `tq_rerank_candidates(...)` | read | approximate + SQL-side exact rerank |
| `tq_maintain_index(index regclass)` | **write** | delta-tier merge / compaction |
| `CREATE INDEX … USING turboquant (…)` | **DDL** | with options `bits`, `lists`, `transform`, `normalized` |
| `REINDEX` (after format upgrade) | **DDL** | v1 → v2 boundary |

This plan integrates the extension into MCPg the same way `pg_cron`,
`pg_partman`, and `pgvector` already are: a presence-checked module, a
read/write/DDL split, tight allowlists on every SQL identifier and
option value, and an audit-database category that surfaces the
extension's observability outputs.

---

## Guardrail rules (apply to every phase)

These are non-negotiable. They mirror the patterns already established
in `cron.py`, `partman.py`, and `vector_tuning.py`:

1. **Module:** a single new `src/mcpg/turboquant.py` (cohesive cluster
   — keeps `tools.py` conflicts to adjacent-block only, per
   `parallel-roadmap.md`).
2. **Presence check** on every public function via
   `extension_installed(driver, "pg_turboquant")`. Reads return `[]` /
   `None` when absent; writes / DDL raise `TurboQuantError`.
3. **Identifier safety:** schema / table / column / index names go
   through `_quoted()` against the same `[A-Za-z_][A-Za-z0-9_]*` regex
   the rest of MCPg uses. No agent-supplied string ever lands
   unquoted in SQL.
4. **Index options allowlist** (`bits`, `lists`, `transform`,
   `normalized`) — each one validated by type and range before any
   `CREATE INDEX` text is built:
   - `bits` ∈ `{1, 2, 4, 8}` (the values upstream documents)
   - `lists` ≥ `0` (0 = flat, >0 = IVF)
   - `transform` ∈ allowlist `{"none", "hadamard"}`
   - `normalized` ∈ `{True, False}` (bool, not freeform)
   - Reject anything else with a precise `TurboQuantError`.
5. **Gating, matching MCPg's existing axes:**
   - Read advisors / metadata / last-scan-stats → no gate (read-only).
   - `tq_maintain_index` → **unrestricted mode** (writes catalog state
     through the AM; same gate `schedule_cron_job` uses).
   - `CREATE INDEX USING turboquant` / `REINDEX` → **unrestricted +
     `MCPG_ALLOW_DDL`** (matches every other DDL-shaped tool, e.g.
     `partman_create_parent`).
6. **Extension allowlist:** add `pg_turboquant` to
   `ENABLEABLE_EXTENSIONS` in `extensions.py` so `enable_extension`
   can install it (still gated by the existing rules).
7. **Audit:** every new tool registers under a new
   `_register_turboquant` family in `tools.py`, gets a `@server.tool`
   with the standard description shape (capability + gating + extension
   prerequisite), and is exercised in audit-trail tests like the rest.
8. **Tests:** unit tests with `FakeRoutingDriver` (extension present /
   absent, identifier rejection, option-range rejection, happy path
   asserts the exact SQL shape). MCP layer test that the tools are
   registered. No live-server integration tests in the first wave —
   the matrix doesn't ship `pg_turboquant`.

---

## Phase 1 — read-only advisors (single PR)

**Goal:** land the module skeleton + the safe, read-only surface so
the rest can compose on top of it.

**New file:** `src/mcpg/turboquant.py`

**Public API:**

```python
class TurboQuantError(Exception): ...

@dataclass(frozen=True, slots=True)
class TurboQuantIndexInfo:
    schema: str
    index: str
    table: str
    column: str
    algorithm_version: str
    quantizer_family: str
    residual_sketch_kind: str
    fast_path_eligible: bool
    capability_flags: list[str]
    delta_state: str
    maintenance_recommended: bool

@dataclass(frozen=True, slots=True)
class TurboQuantHeapStats:
    schema: str
    index: str
    row_count: int

@dataclass(frozen=True, slots=True)
class TurboQuantLastScanStats:
    raw: dict[str, Any]                # the JSON pg_turboquant returns
    score_mode: str | None
    simd_kernel: str | None
    pages_pruned: int | None
    pages_scanned: int | None

@dataclass(frozen=True, slots=True)
class TurboQuantQueryKnobs:
    index: str
    recommendations: dict[str, Any]    # the extension's recommendation payload
    rationale: str

async def list_turboquant_indexes(driver) -> list[TurboQuantIndexInfo]:
    """Walk pg_index/pg_am for AM='turboquant', call tq_index_metadata
    on each, return one row per index. Empty list if extension absent."""

async def get_turboquant_index_metadata(driver, schema, index) -> TurboQuantIndexInfo
async def get_turboquant_heap_stats(driver, schema, index) -> TurboQuantHeapStats
async def get_turboquant_last_scan_stats(driver) -> TurboQuantLastScanStats | None
async def recommend_turboquant_query_knobs(
    driver, schema, table, vector_column, *, metric, limit, rerank_limit
) -> TurboQuantQueryKnobs
```

**Tools registered (read-only, no gate):**

- `list_turboquant_indexes`
- `get_turboquant_index_metadata(schema, index)`
- `get_turboquant_heap_stats(schema, index)`
- `get_turboquant_last_scan_stats()`
- `recommend_turboquant_query_knobs(schema, table, vector_column, metric, limit, rerank_limit)`

**Tests:** ~12 unit tests covering presence/absence, identifier
rejection, regclass binding, the JSON-unwrap of `tq_last_scan_stats`,
and the MCP registration smoke check.

**Docs:** add a `9.x` row to `feature-shortlist.md` flipped to ✅; one
line in `docs/tour.md` (per the parallel-roadmap rule: line only, not
the tool count); CHANGELOG bullet under `### Added`.

---

## Phase 2 — advisor surface + `audit_database` integration (single PR)

**Goal:** turn the metadata into MCPg-native recommendations and feed
them into the existing `audit_database` scorecard.

**New advisor in `turboquant.py`:**

```python
@dataclass(frozen=True, slots=True)
class TurboQuantAdvisorFinding:
    schema: str
    index: str
    severity: str          # GOOD / WARNING / CRITICAL
    code: str              # e.g. "maintenance_due", "format_v1_reindex_needed",
                           #      "fast_path_ineligible", "delta_tier_large"
    evidence: str
    suggested_action: str  # ready-to-run SQL (`SELECT tq_maintain_index(...)`
                           # or `REINDEX INDEX CONCURRENTLY ...`)

async def recommend_turboquant_maintenance(driver) -> list[TurboQuantAdvisorFinding]:
    """For every turboquant index: read metadata + heap stats, apply
    rules below, emit a finding when anything is sub-optimal."""
```

**Rule codes (initial set, easy to extend later):**

| Code | Trigger | Severity | Suggested action |
|---|---|---|---|
| `format_v1_reindex_needed` | `algorithm_version` starts with `v1` | CRITICAL | `REINDEX INDEX CONCURRENTLY <idx>` |
| `maintenance_due` | metadata flag says delta tier should merge | WARNING | `SELECT tq_maintain_index('<idx>')` |
| `fast_path_ineligible` | `fast_path_eligible = false` | WARNING | text — usually a knob mismatch; link to the README's tuning table |
| `delta_tier_large` | heap-stats delta rows > N% of base | WARNING | `SELECT tq_maintain_index('<idx>')` |

**Tool registered:**

- `recommend_turboquant_maintenance` (read-only, no gate)

**`audit_database` integration** (`audit.py`):

- New `audit_turboquant_indexes(driver) -> CategoryResult | None`.
- Skip cleanly (`return None`) when the extension is absent — the
  scorecard simply omits the category, the way other optional
  categories already do.
- Wire it into `audit_database` next to `audit_cleanliness_bloat`:
  append to `categories` only when non-None; everything downstream
  (overall score, top issues, recommendations) already handles
  variable-length category lists.
- Each finding becomes a `MetricResult` with `status` set from the
  severity, `evidence`, and `suggestion` lifted directly from the
  advisor. `audit_database`'s existing loop will promote
  CRITICAL/WARNING entries into `top_issues` and `recommendations`
  without further changes.

**Tests:** the advisor's rule table is the bulk of the suite —
parametrised: one fixture per rule code, both the GOOD path and each
sub-optimal path. Plus an `audit_database`-level test asserting that
turboquant findings flow into `top_issues` with the right severity.

**Docs:** add a `4.x` (audit) row to `feature-shortlist.md`,
CHANGELOG bullet, one line in `docs/tour.md`.

---

## Phase 3 — write tool: `tq_maintain_index` (single PR, small)

**Goal:** let agents act on the Phase-2 recommendations.

**New function in `turboquant.py`:**

```python
@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    schema: str
    index: str
    started_at: str
    completed_at: str

async def maintain_turboquant_index(driver, schema, index) -> MaintenanceResult:
    """Call tq_maintain_index('<schema>.<index>')."""
```

**Gating:** unrestricted mode (matches `schedule_cron_job`,
`unschedule_cron_job`). **Not** DDL-gated — it modifies index payload
not catalog shape.

**Validation:** identifier-quote both `schema` and `index`; reject
anything that doesn't match `_IDENTIFIER`. Confirm the named index is
actually a turboquant index (catalog lookup on `pg_index`/`pg_am`)
before running, so the call can't be turned into a way to "probe"
arbitrary indexes for error messages.

**Tool registered (in a new `_register_turboquant_writes`):**

- `maintain_turboquant_index(schema, index)`

**Tests:** happy path + identifier rejection + extension-absent +
"index exists but isn't a turboquant index" rejection.

---

## Phase 4 — DDL tools: `create_turboquant_index`, `reindex_turboquant_index` (single PR)

**Goal:** complete the CRUD loop. This is the highest-blast-radius
piece, so it lands last and behind the strictest gate.

**New functions:**

```python
async def create_turboquant_index(
    driver,
    schema: str,
    table: str,
    column: str,
    *,
    name: str | None = None,            # optional explicit index name
    metric: str,                         # "cosine" | "inner_product" | "l2"
    bits: int = 8,
    lists: int = 0,
    transform: str = "none",
    normalized: bool = False,
    concurrently: bool = True,
) -> CreateIndexResult:
    """Build and execute `CREATE INDEX [CONCURRENTLY] ... USING turboquant
    (col tq_<metric>_ops) WITH (bits=..., lists=..., transform='...',
    normalized=...)`. Every value comes from an allowlist, so the only
    string interpolation is the validated values themselves."""

async def reindex_turboquant_index(driver, schema, index, *, concurrently=True) -> ReindexResult:
    """REINDEX INDEX [CONCURRENTLY] <schema>.<index>. Pre-flight: confirm
    the index is a turboquant index — same reasoning as Phase 3."""
```

**Operator-class mapping** (single source of truth, like
`_BACKUP_FORMAT_FLAGS` in `cron.py`):

```python
_TQ_OPS_FOR_METRIC = {
    "cosine": "tq_cosine_ops",
    "inner_product": "tq_inner_product_ops",
    "l2": "tq_l2_ops",
}
```

**Gating:** unrestricted + `MCPG_ALLOW_DDL` (matches
`partman_create_parent`).

**Tools registered (in `_register_turboquant_ddl`):**

- `create_turboquant_index(...)`
- `reindex_turboquant_index(schema, index, concurrently=True)`

**Tests:** the full validation matrix —
- happy paths per metric and per option combination,
- bits/lists/transform out-of-range,
- identifier rejection on schema/table/column/name,
- `CONCURRENTLY` flag flows through verbatim,
- catalog pre-flight on `reindex_turboquant_index`,
- DDL gate rejects calls when `MCPG_ALLOW_DDL=false`.

**Docs:** flip an item in `feature-shortlist.md` to ✅, CHANGELOG
bullet, `docs/tour.md` line.

---

## Phase 5 — query execution + per-query advisor (single PR)

**Status:** promoted from "out of scope" to in-scope after a coverage
sweep. Two reasons:

1. **Adoption.** Without these, callers can list and inspect a
   turboquant index but can't actually issue a turboquant-aware
   query through MCPg. The pgvector operators (`<->`, `<=>`, `<#>`)
   may not exercise the turboquant access method's SIMD fast path
   the same way the dedicated functions do.
2. **RAG efficiency dependency.** Phase A of the RAG efficiency suite
   (`analyze_vector_search_efficiency`) sweeps a candidate-multiplier
   axis. The turboquant arm of that sweep is `rerank_limit`, which
   is set via `tq_rerank_candidates(...)`. Without TQ-5 the RAG
   suite's turboquant arm degenerates to "call vector_search and
   hope the planner picks the index."

**Goal:** thin pass-through wrappers around the three user-query
functions, with defensive JSON parsing on returns (matching the
`tq_last_scan_stats` pattern in TQ-1).

**New functions in `turboquant.py`:**

```python
@dataclass(frozen=True, slots=True)
class TurboQuantCandidate:
    candidate_id: Any         # caller's id type — bigint, uuid, text
    score: float

@dataclass(frozen=True, slots=True)
class TurboQuantSearchResult:
    schema: str
    table: str
    candidates: list[TurboQuantCandidate]
    last_scan_stats: TurboQuantLastScanStats | None  # composes the TQ-1 helper

async def turboquant_approx_candidates(
    driver, schema, table, id_column, vector_column,
    query_vector: list[float], *, metric, limit
) -> TurboQuantSearchResult:
    """Wrap `tq_approx_candidates(...)`. No rerank step."""

async def turboquant_rerank_candidates(
    driver, schema, table, id_column, vector_column,
    query_vector: list[float], *, metric, limit, rerank_limit
) -> TurboQuantSearchResult:
    """Wrap `tq_rerank_candidates(...)`. Approx retrieval + exact rerank."""

async def recommend_turboquant_query_knobs(
    driver, schema, table, vector_column,
    query_vector: list[float], *, metric, limit, rerank_limit
) -> dict[str, Any]:
    """Wrap `tq_recommended_query_knobs(...)`. Defensive return — the
    upstream signature isn't documented at field level, so the result
    is exposed as the raw JSON the function returns plus a thin
    `recommendations` typed view of the keys we recognise."""
```

**Validation:**
- `_validate_identifier` on schema / table / id_column / vector_column.
- `metric` allowlist matching TQ-4's `_TQ_OPS_FOR_METRIC` (single
  source of truth — defined in TQ-4 or in a shared module so both
  phases reuse it).
- `limit` and `rerank_limit` both validated as `1..10_000` to avoid
  pathological argument values.
- `query_vector` is a `list[float]`; MCPg already does this for
  `vector_search` — reuse the same coercion helper.

**Gating:** read-only. No `MCPG_ALLOW_*` required. Per-call cost is
one query function invocation plus an automatic follow-up call to
`tq_last_scan_stats()` to populate the report's diagnostics.

**Tools registered** (in `_register_turboquant_reads`):

- `turboquant_approx_candidates(...)`
- `turboquant_rerank_candidates(...)`
- `recommend_turboquant_query_knobs(...)`

**Tests:**
- Happy path per function: extension present + fake routing returns
  rows.
- Extension absent: `TurboQuantError` raised.
- Identifier rejection across schema / table / id_column / vector_column.
- Metric not in allowlist.
- `limit` / `rerank_limit` out of range.
- `tq_last_scan_stats()` composition: TQ-5 happy path includes the
  diagnostics from the helper added in TQ-1.

**Docs:** new "Search a pg_turboquant index" section in `tour.md`,
new feature-shortlist row, CHANGELOG bullet.

---

## Out of scope for this plan

The following could come later but are deliberately deferred:

- **`v2 → vN` migration helpers.** Upstream is currently at v2; we
  don't need a migration tool until they ship v3.
- **Multi-database / cross-cluster `tq_maintain_index` scheduler.**
  Once Phases 1–5 land, this is just `schedule_cron_job` composing
  Phase 3 — not a separate feature.

---

## Sequencing & branch names

Each phase lands on its own branch / PR so reviews stay narrow and
parallel work elsewhere isn't blocked:

1. `claude/tq1-read-advisors` — Phase 1 ✅ (PR #71)
2. `claude/tq5-query-execution` — Phase 5 (promoted ahead of 2/3/4)
3. `claude/tq2-audit-integration` — Phase 2
4. `claude/tq3-maintenance-write` — Phase 3
5. `claude/tq4-ddl-tools` — Phase 4

**Why TQ-5 ahead of TQ-2/3/4:** TQ-5 unblocks both adoption ("I can
actually query the index from MCPg") and the RAG efficiency suite's
turboquant arm. TQ-2/3/4 are advisor + admin shaped; valuable but
not blocking.

Phase 1 has no dependencies; Phase 5 depends only on TQ-1's
`TurboQuantLastScanStats`. Phase 2 depends on Phase 1's advisor
dataclasses; Phase 3 is independent of Phase 2 (could land
concurrently); Phase 4 is independent of Phase 3 (also could land
concurrently). Realistic order is 1 → (2 || 3) → 4.
