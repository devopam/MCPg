# PostgreSQL 19 readiness

Tracking document for MCPg's PG 19 readiness work. Spawned by issue #120.

## Current status

| Phase | Status | Notes |
|---|---|---|
| 1. CI matrix + compatibility surface | **in progress** | PG 19 beta added as experimental matrix entry. pgvector built from source (custom Dockerfile). PostGIS deferred until apt package available. Phase-1 PR: this doc + CI changes. |
| 2. Feature audit | not started | Decision matrix below to be filled in as PG 19 progresses through Beta → RC → GA. |
| 3. Incremental landing | not started | Each row marked "expose via new tool" / "extend existing tool" below becomes a separate small PR. |

## Phase 1 — what landed

- `.github/workflows/ci.yml`: PG 19 added to the test matrix with `continue-on-error` (non-blocking until GA).
- `.github/ci-postgres-pg19.Dockerfile`: standalone Dockerfile that builds pgvector v0.8.0 from source on top of `postgres:19beta1`. Drop and route PG 19 back to the standard Dockerfile once pgvector ships a `pg19` image tag.
- `pyproject.toml` / `README.md`: PG version range updated.
- This document.

PostGIS is intentionally omitted from the PG 19 image — no `postgresql-19-postgis-3` apt package is published yet. Tests that require PostGIS will fail under PG 19; failures are non-blocking via the `continue-on-error` matrix entry.

## Phase 2 — feature audit

Walking each highlighted PG 19 Beta 1 feature against MCPg's existing tool surface. Each row carries:

- **Spec link** — upstream commit / release notes
- **Existing surface affected** — MCPg modules and tools touched
- **Decision** — expose via new tool / extend existing tool / defer
- **Test plan** — what we'd assert against PG 19 specifically

| PG 19 feature | MCPg surface | Decision | Status |
|---|---|---|---|
| Asynchronous I/O subsystem (`io_method` GUC, new `pg_stat_io` columns) | `mcpg.io_stats.read_pg_stat_io`; new advisor `recommend_io_method` | **extend + add tool** | TODO |
| OAuth-based authentication (`pg_hba.conf` extension) | None today; auth lives outside MCPg | **defer** (doc only) | TODO |
| MERGE … RETURNING improvements | `mcpg.write.run_write` allow-list / safety driver | **verify** (no new tool, characterization test) | TODO |
| GIN OR-of-AND filtering optimisations | Planner-only; no MCPg surface | **defer** (perf bench only) | TODO |
| Skip scans for B-tree indexes | `mcpg.advisors.recommend_indexes` heuristic | **extend** (factor in skip-scan eligibility) | TODO |
| Partition expression-based bounds | `mcpg.introspection.list_partitions`, `mcpg.partman.*`, `mcpg.schema_diff.compare_schemas` | **verify** (characterization tests) | TODO |
| CHECK constraints NOT VALID + validation | DDL surface | **add tool** `validate_check_constraint` | TODO |
| Logical replication: per-table progress | Replication tooling | **add tool** `read_logical_replication_progress` | TODO |
| New `pg_stat_*` columns (vacuum I/O timing, etc) | `mcpg.health.audit_database`, `mcpg.health.check_database_health`, `mcpg.advisors.run_advisors` | **extend** | TODO |
| `pg_get_acl()` SQL function | `mcpg.introspection.list_grants` | **extend** (faster, more accurate) | TODO |
| Hash function for `interval` type | `mcpg.partman.partman_create_parent` | **verify** (hash partitioning over interval) | TODO |
| VACUUM: opportunistic freeze + faster pages | `mcpg.maintenance.run_maintenance` | **extend** (update tool description) | TODO |
| Sequence: per-sequence access methods | DDL surface | **defer** (no user demand) | TODO |

## Phase 2 — prioritised list (MCPg view)

Each candidate is scored along four axes; the headline column **PR weight** is what an implementer needs to read first.

**Scoring axes**

- **Agent value (1-5)** — how often will an LLM-driven workflow hit this surface? 5 = ubiquitous (every DBA session); 1 = niche.
- **Impl. cost** — `S` (one module + tests, ≤ ~500 lines), `M` (advisor + multi-tool, ~1000 lines), `L` (cross-module refactor).
- **Differentiation (1-3)** — does exposing this give MCPg a unique value vs `psql` + `pg_stat_*` views? 3 = yes (advisor or autowarm-style automation); 1 = thin wrapper.
- **PG 19 dependence** — `hard` (only works on PG 19; needs feature-detection shim), `soft` (PG 19 makes existing surface better, no shim needed), `compat` (just shape-compat work to keep PG 14-18 green).

**PR weight = `agent_value * differentiation / impl_cost_weight`** (S = 1, M = 2, L = 3), rounded for ordering.

| # | Feature | Surface change | Agent value | Cost | Diff. | Dep. | PR weight | Notes |
|---|---|---|---:|:---:|---:|---|---:|---|
| 1 | **Async I/O advisor (`recommend_io_method`)** | New advisor + extend `read_pg_stat_io` with new columns | 5 | M | 3 | hard | 7.5 | The headline PG 19 differentiator. Operators don't know whether `io_uring` / `worker` / `sync` is right; advisor reads workload from `pg_stat_io` + `pg_stat_database` and recommends. |
| 2 | **Skip-scan-aware `recommend_indexes`** | Extend existing advisor — add a heuristic that prefers indexes where skip-scan would help | 5 | S | 3 | soft | 15.0 | Highest leverage: improves the most-used advisor without a new tool surface. Cheap because the heuristic plugs into existing scoring. |
| 3 | **CHECK constraint validation tool (`validate_check_constraint`)** | New DDL tool wrapping `ALTER TABLE ... VALIDATE CONSTRAINT` | 4 | S | 3 | hard | 12.0 | DBAs ship `NOT VALID` constraints all the time; the validate step is often forgotten. A dedicated tool closes the loop. |
| 4 | **`pg_get_acl()` migration in `list_grants`** | Replace the catalogue-walking query with `pg_get_acl(class, oid)` | 4 | S | 2 | soft | 8.0 | Faster, more accurate, fewer corner cases. Drop-in compat — wraps with a version-detect fallback for PG ≤ 18. |
| 5 | **Per-relation logical replication progress (`read_logical_replication_progress`)** | New read tool reading from new `pg_stat_subscription_stats` columns | 4 | S | 2 | hard | 8.0 | Replication observability today reads cluster-wide LSN lag; per-relation breakdown is genuinely new. |
| 6 | **VACUUM `pg_stat` enrichments (`run_advisors`, `check_database_health`)** | Extend existing advisors with new I/O-timing columns | 3 | S | 2 | soft | 6.0 | Quietly improves several advisors. No new tools. |
| 7 | **Async I/O metrics in observability (`read_pg_stat_io`)** | Pull new columns through to the existing read | 3 | S | 1 | soft | 3.0 | Strictly needed for (1) to work; can land standalone or as part of (1). |
| 8 | **Partition expression characterisation tests** | No tool change; assert `list_partitions` / `compare_schemas` handle expression-bound partitions | 3 | S | 1 | compat | 3.0 | Defensive — surfaces drift before a user hits it. |
| 9 | **MERGE … RETURNING characterisation tests** | No tool change; verify `run_write` accepts the new syntax | 3 | S | 1 | compat | 3.0 | Same shape as (8). |
| 10 | **Hash-on-`interval` partitioning verification** | No tool change; verify `partman_create_parent` accepts interval-hash partition key | 2 | S | 1 | compat | 2.0 | Niche; fits in the same "PG 19 characterisation" PR as (8) + (9). |
| 11 | **OAuth `pg_hba.conf` documentation** | Docs only — `docs/security.md` callout | 2 | S | 1 | hard | 2.0 | No tool surface; document the operator pattern. |
| 12 | **GIN OR-of-AND perf bench** | Optional `benchmarks/` script | 1 | S | 1 | soft | 1.0 | Nice to have for the release blog post; not a tool. |
| 13 | **Per-sequence access methods** | DDL surface | 1 | M | 1 | hard | 0.5 | Defer until concrete user demand. |

### Recommended landing order (Phase 3)

The bigger-than-it-looks rule: weight ordering is the rough guide, but related items batch into the same PR.

1. **PR A — "Skip-scan-aware `recommend_indexes`"** (weight 15.0). Highest leverage, lowest cost. Single advisor extension, full test coverage. Lands first because it doesn't depend on PG 19 — it just becomes more accurate when PG 19 is available.
2. **PR B — "PG 19 characterisation tests"** (rows 8, 9, 10). Single PR that pins behaviour for partition expressions, MERGE … RETURNING, and interval-hash partitioning. Defensive; lands second so all other Phase 3 PRs branch off a known-good PG 19 baseline.
3. **PR C — "`validate_check_constraint` tool"** (weight 12.0). New tool with full module / tool / bucket / test treatment per the `mcpg-add-tool` skill. The first net-new tool of Phase 3.
4. **PR D — "Async I/O advisor"** (weight 7.5; bundles rows 1 + 7). The headline PG 19 differentiator. New module, new advisor, extends `read_pg_stat_io`. Largest single PR of Phase 3.
5. **PR E — "`pg_get_acl()` upgrade in `list_grants`"** (weight 8.0). Drop-in upgrade with PG ≤ 18 fallback.
6. **PR F — "Per-relation logical replication progress"** (weight 8.0). Read-only tool over new columns.
7. **PR G — "VACUUM + autovacuum advisor enrichments"** (weight 6.0). Quiet pass over `run_advisors` + `check_database_health`.
8. **PR H — "OAuth pg_hba documentation"** (weight 2.0). Doc-only.

After PR D lands, MCPg has covered every PG 19 feature with meaningful operational impact. Items 12 + 13 stay in the queue with no commitment.

### Decision rule

A row is **GO** for the current cycle when:

- weight ≥ 5, **or**
- it batches with another GO row (same PR), **or**
- it's a characterisation test that defends an already-shipped surface against PG 19 shape changes.

Everything else stays in the audit table until evidence of demand.

## Phase 3 — incremental landing plan

| Milestone | Work | Gate |
|---|---|---|
| Now (Beta 1) | CI matrix + Phase 1 compat shims | This PR |
| Beta 2/3 | Feature-specific PRs based on Phase 2 audit | ~5-10 PRs |
| RC | Final pass; promote PG 19 to a required CI job (drop `continue-on-error`) | One PR |
| GA + 1 week | Release advertising PG 19 support in README + PyPI classifiers | Release PR |

## Why bother now

1. **The compatibility check is cheap.** Most failures will be shape changes in `pg_stat_*` views or new columns mid-`SELECT *` queries. Fixing them in advance avoids panic when a user upgrades to PG 19 in staging.
2. **Skip-scans alone are worth the early read.** `recommend_indexes` becomes meaningfully more accurate when it can suggest indexes that benefit from the new B-tree skip-scan optimisation.
3. **AIO is genuinely new operational surface.** Operators will want guidance on `io_method=io_uring` vs `worker` vs `sync`. Having `recommend_io_method` ready at GA is differentiation.
4. **Community signal.** Being explicitly "PG 19 day-one ready" on the PyPI page is a stronger positioning than waiting.

## Local testing

```bash
# Build the PG 19 image locally:
docker build -f .github/ci-postgres-pg19.Dockerfile -t mcpg-pg19 .

# Run the test suite against it:
docker run -d --name mcpg-pg19 -p 5432:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=mcpg_test mcpg-pg19
MCPG_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mcpg_test \
  uv run pytest -q --cov
```

Expected behaviour: PostGIS-dependent tests skip / fail (see `tests/integration/test_geo.py`); everything else should pass. Failures outside that category are the Phase 2 triage queue.

## Cross-references

- Beta 1 release notes: <https://www.postgresql.org/about/news/postgresql-19-beta-1-released-3313/>
- Tracking issue: #120
- Phase A static fact pack (Phase B observation harness baseline): `docs/reviews/tool-surface-fact-pack.md`
