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
