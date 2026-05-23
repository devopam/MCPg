# MCPg — Progress Tracker

> **Resume here.** A new session should read `PLAN.md` then this file, then
> start the task under **Next action**. Update this file and commit before
> ending any session.

## Current state

- **Phase:** 18 — Schema diff (Phases 0–17 complete; **Batch A done**)
- **Last updated:** 2026-05-23
- **Branch:** `claude/postgresql-mcp-planning-8KssU`

## Next action

> Phase 18 complete (`compare_schemas`). **Batch A finished** — catalog
> completeness, visualisation, and structural diff are all in.
> Awaiting direction on the next batch:
>
> - **Batch B** — Advisors / lint (Phase 20) + audit trail (Phase 21);
> - **Batch C** — extension power-tools (`pg_cron`/`pg_partman`/pgvector);
> - **Batch G (USP)** — `generate_prisma_schema` then sibling ORM
>   bridges (Drizzle, SQLAlchemy, sqlc).
>
> Batches D, E, F still require their ADRs first (subprocess policy,
> NOTIFY model, migration shadowing).

## Phase 0 — Spike & foundation  ✅ COMPLETE

- [x] 0.1 Evaluate `crystaldba/postgres-mcp` (code, tests, license, activity) → ADR-0001 (hard-fork)
- [x] 0.2 Confirm/record stack → ADR-0002 (Python 3.12 + psycopg3 + mcp SDK)
- [x] 0.3 Vendor `sql/` subpackage (MIT, `NOTICE` + `_vendor/README.md`); scaffold `uv` project
- [x] 0.4 Configure `ruff`, `mypy --strict`, `pytest`, `pytest-cov`, coverage gate (in `pyproject.toml`)
- [x] 0.5 GitHub Actions CI (`.github/workflows/ci.yml`: ruff + mypy + pytest)
- [x] 0.6 `CONTRIBUTING.md`, pre-commit hooks (local hooks), issue/PR templates
- [x] 0.7 First green CI run — run #1 on commit `a20a757`, conclusion: success

### Phase 0 notes

- Vendored kernel lives in `src/mcpg/_vendor/sql/`; 75 upstream tests ported and
  passing (`tests/vendor/`). `test_db_conn_pool` and `test_readonly_enforcement`
  were NOT ported — they couple to upstream `server.py`; re-derive under TDD in
  Phase 1/3.
- `uv sync` + `uv run pytest tests/vendor` + `ruff` + `mypy src/mcpg` all green
  locally.
- CI runs ruff + mypy + pytest. The **coverage gate** (`fail_under = 90`) and
  the **PG 14–17 service-container matrix** are intentionally deferred: they are
  wired in during Phase 1 (authored code exists) and Phase 2 (integration tests
  exist) respectively, to avoid dead/failing config now.

## Phase 1 — Core server skeleton  ✅ COMPLETE

- [x] 1.1 Typed env-driven config/settings loader (`mcpg/config.py`, TDD, 100% cov)
- [x] 1.2 Connection-pool lifecycle wrapper (`mcpg/database.py`, TDD, 100% cov)
- [x] 1.3 MCP server bootstrap (`mcpg/server.py`, TDD, 100% cov); no global state
- [x] 1.4 `get_server_info` tool — first end-to-end vertical slice (`mcpg/tools.py`, TDD)
- [x] 1.5 `mcpg` CLI entry point (`mcpg/__main__.py`, TDD)
- [x] 1.6 Coverage gate (`--cov`, `fail_under = 90`) wired into CI

## Phase 2 — Schema introspection & safe reads  ✅ COMPLETE

- [x] 2.1 Integration-test harness (`tests/integration/`) + PG 14–17 CI service matrix
- [x] 2.2 Introspection tools: `list_schemas`, `list_tables`, `describe_table`,
      `list_indexes`, `list_extensions` (`mcpg/introspection.py`, TDD)
- [x] 2.3 `run_select` — read-only-enforced query execution via vendored `SafeSqlDriver` (`mcpg/query.py`, TDD)
- [x] 2.4 `explain_query` tool (`mcpg/query.py`, TDD)
- [x] 2.5 Result shaping — `max_rows` cap + `truncated` flag on `QueryResult` (TDD)

## Phase 3 — Security hardening & access control  ✅ COMPLETE

- [x] 3.1 Access-mode policy engine — gate tool registration by `Settings.access_mode` (`mcpg/policy.py`, TDD)
- [x] 3.2 SQL-safety regression suite — adversarial tests for the SQL-injection CVE class (`tests/unit/test_sql_safety.py`)
- [x] 3.3 Audit logging of tool invocations (`mcpg/audit.py`, TDD)
- [x] 3.4 Threat model + security documentation (`docs/security.md`)

## Phase 4 — Write & DDL tools  ✅ COMPLETE

- [x] 4.1 `run_write` — gated DML (INSERT/UPDATE/DELETE), unrestricted mode only (`mcpg/write.py`, TDD)
- [x] 4.2 `run_ddl` — gated DDL, unrestricted mode + `MCPG_ALLOW_DDL` opt-in (`mcpg/write.py`, TDD)
- [x] 4.3 Phase 4 verification — write tool calls audited end-to-end

### Phase 4 decisions

- DDL requires a second opt-in beyond unrestricted mode (`MCPG_ALLOW_DDL`),
  per user direction — DDL has the highest blast radius.
- No dry-run/preview: writes execute directly (user direction — avoid the
  runtime cost of a rolled-back preview transaction).
- Per-write auditing is already provided by `AuditedFastMCP` (every tool call
  is audited); Task 4.3 verifies it for write tools rather than adding code.

## Phase 5 — Ops, health & tuning  ✅ COMPLETE

> Authored fresh under TDD — the upstream `database_health/`, `index/`,
> `top_queries/` modules were not vendored (ADR-0001 narrowed scope to `sql/`).

- [x] 5.1 `check_database_health` — connections, cache hit ratio, vacuum/dead
      tuples, invalid indexes (`mcpg/health.py`, TDD)
- [x] 5.2 `analyze_workload` — slow queries via `pg_stat_statements` (`mcpg/workload.py`, TDD)
- [x] 5.3 `recommend_indexes` — missing-index heuristics (`mcpg/indexing.py`, TDD)
- [x] 5.4 `analyze_query_plan` — structured `EXPLAIN` plan analysis (`mcpg/query.py`, TDD)

## Phase 6 — Scalability & multi-tenancy

- [x] 6.1 Configurable connection-pool sizing (`MCPG_POOL_MIN_SIZE`/`MAX_SIZE`,
      vendored `DbConnPool` patched per ADR-0003)
- [x] 6.2 Multi-tenancy & RLS awareness — document-only for v0.1.0
      (`docs/security.md`); per-request-role mechanism deferred post-1.0
- [x] 6.3 Scaling characteristics (`docs/scaling.md`) + benchmark harness (`benchmarks/bench.py`)
- [ ] 6.4 (optional, deferred post-1.0) server-side cursors; read-replica routing

## Phase 7 — Docs, packaging & release  ✅ COMPLETE (pending release sign-off)

- [x] 7.1 Usage docs + tool reference (`docs/tools.md`); the usage guide was
      later split into `docs/installation.md` + `docs/user-guide.md`
- [x] 7.2 Packaging — `Dockerfile`, `.dockerignore`, install instructions
- [x] 7.3 v0.1.0 release prep — version bumped to 0.1.0, CHANGELOG finalised.
      Tagging/publishing awaits explicit user sign-off.

> **v0.1.0 merged to `main` via PR #1.** Post-1.0 work continues below.

## Phase 8 — Index intelligence & extension management  ✅ COMPLETE

- [x] 8.1 `list_indexes` reports the index access method (btree/gin/gist/...)
- [x] 8.2 `list_available_extensions` tool — installed vs available
- [x] 8.3 `enable_extension` tool — gated DDL, known-extension allowlist
- [x] 8.4 Index-type-aware `recommend_indexes` — GIN for `jsonb`/arrays,
      trigram GIN for text columns

## Phase 9 — Text search & fuzzy matching  ✅ COMPLETE

- [x] 9.1 Trigram fuzzy/similarity search tool over `pg_trgm` (`mcpg/textsearch.py`, TDD)
- [x] 9.2 Full-text search tool over `tsvector`/`tsquery` (`mcpg/textsearch.py`, TDD)

## Phase 10 — Vector search (`pgvector`)  ✅ COMPLETE

- [x] 10.1 `vector` column awareness — `describe_table` reports vector dimension (TDD)
- [x] 10.2 k-NN vector similarity search tool (`<->`/`<=>`/`<#>`) (`mcpg/textsearch.py`, TDD)
- [x] 10.3 HNSW/IVFFlat index awareness — `list_indexes` reports the access
      method; confirmed by an integration test (`method == "hnsw"`).

## Phase 11 — Geospatial (PostGIS)  ✅ COMPLETE

- [x] 11.1 `geo_search` tool — k-NN by PostGIS distance to a lon/lat point;
      CI builds a pgvector + PostGIS image so it is integration-tested.
- Geometry column types and GiST spatial indexes were already surfaced by
  `describe_table` and `list_indexes`.

> Phases 8–11 cover PostgreSQL extension and advanced-feature support; see
> `PLAN.md` §7a for the capability inventory and per-extension priorities.

## Phase 12 — Deeper schema introspection

- [x] 12.1 `list_constraints` — PK, FK, unique, check, exclusion (`mcpg/introspection.py`, TDD)
- [x] 12.2 `list_views` (+ view definitions) (`mcpg/introspection.py`, TDD)
- [x] 12.3 `list_functions` — functions and procedures (`mcpg/introspection.py`, TDD)
- [x] 12.4 `list_triggers` (`mcpg/introspection.py`, TDD)
- [x] 12.5 `list_sequences` (`mcpg/introspection.py`, TDD)

## Phase 13 — Partitioning

- [x] 13.1 `list_partitions` — strategy, bounds, parent↔partition links (`mcpg/introspection.py`, TDD)
- [x] 13.2 Flag partitioned tables / partitions in `list_tables` (`mcpg/introspection.py`, TDD)
- [x] 13.3 Partition-aware `list_indexes` and `recommend_indexes` (TDD)

## Phase 14 — Access-control introspection

- [x] 14.1 `list_policies` — Row-Level-Security policies on a table (`mcpg/introspection.py`, TDD)
- [x] 14.2 `list_roles` (`mcpg/introspection.py`, TDD)
- [x] 14.3 `list_grants` — table/object privileges (`mcpg/introspection.py`, TDD)

## Phase 15 — Live ops & maintenance

- [x] 15.1 `list_active_queries` + lock / blocking inspection (`mcpg/liveops.py`, TDD)
- [x] 15.2 Replication-lag and bloat health checks (`mcpg/health.py`, TDD)
- [x] 15.3 Gated `run_maintenance` (VACUUM/ANALYZE) (`mcpg/maintenance.py`, TDD)
- [x] 15.4 Gated `cancel_query` / `terminate_backend` (`mcpg/liveops.py`, TDD)

> Phases 12–15 cover deeper introspection and live ops; see `PLAN.md` §7b for
> the capability gap analysis behind them.

## Decisions log

| ID  | Decision | Status | Date |
|-----|----------|--------|------|
| —   | Scope: broad (ops + data access, gated by access mode) | accepted | 2026-05-20 |
| ADR-0001 | Approach: hard-fork `crystaldba/postgres-mcp` (MIT); TDD-hybrid (strict TDD for new code, characterization tests for inherited kernel) | accepted | 2026-05-20 |
| ADR-0002 | Stack: Python 3.12 + psycopg3 + `mcp` SDK + pglast; `mypy --strict` + coverage gate for new code | accepted | 2026-05-20 |
| —   | Phase 4: DDL gated behind a second opt-in (`MCPG_ALLOW_DDL`); no dry-run (direct execution) | accepted | 2026-05-20 |
| —   | Extension support (Phases 8–11) lands **after** the v0.1.0 release (Phase 7) | accepted | 2026-05-20 |
| —   | Index intelligence (Phase 8) is the first extension area implemented | accepted | 2026-05-20 |
| ADR-0003 | Configurable pool sizing via a minimal behaviour-preserving patch to the vendored `DbConnPool` | accepted | 2026-05-20 |
| —   | Multi-tenancy/RLS: document-only for v0.1.0 (one instance per tenant); per-request `SET ROLE` deferred post-1.0 | accepted | 2026-05-20 |

## Open questions

- Remote HTTP transport auth model (Phase 1/3).
- Whether tuning tools need opt-in beyond `unrestricted` (Phase 5).
- Observability scope (Phase 6).

## Session log

- 2026-05-20 — Researched ecosystem, created `PLAN.md` + this tracker.
  Official MCP Postgres server confirmed deprecated/archived; `crystaldba/postgres-mcp`
  identified as strongest base. Plan committed; Phase 0 ready to start.
- 2026-05-20 — Task 0.1/0.2: hands-on eval of `crystaldba/postgres-mcp`
  (commit `07eb329`, MIT, ~7.3k src / ~6.4k test LOC, real-Postgres tests).
  Decided hard-fork with TDD-hybrid strategy. Wrote ADR-0001 + ADR-0002.
- 2026-05-20 — Task 0.3/0.4: narrowed vendoring scope to the self-contained
  `sql/` subpackage only (import-graph verified). Vendored 6 files + 75 tests,
  scaffolded the `uv` project (`pyproject.toml`, tooling config, `NOTICE`,
  `CHANGELOG.md`). All tests/lint/types green locally.
- 2026-05-20 — Task 0.5: added GitHub Actions CI (`ci.yml`) running ruff,
  ruff-format, mypy, and pytest. Coverage gate + PG matrix deferred (see notes).
- 2026-05-20 — Task 0.6/0.7: added `CONTRIBUTING.md`, local pre-commit hooks,
  issue/PR templates. Set `force-exclude` so ruff skips vendored code under
  pre-commit. CI run #1 green. **Phase 0 complete.**
- 2026-05-20 — Task 1.1: TDD'd the env-driven config loader (`mcpg/config.py`):
  `Settings`, `AccessMode`, `Transport`, `load_settings`, `ConfigError`.
  16 tests, 100% coverage of authored code; repr redacts credentials.
- 2026-05-20 — Task 1.2: TDD'd the `Database` lifecycle wrapper
  (`mcpg/database.py`) around the vendored `DbConnPool` — connect/close, async
  context manager, typed `DatabaseError`. Switched pytest to `asyncio_mode =
  auto`. 99 tests, 100% coverage.
- 2026-05-20 — Task 1.3: TDD'd the server bootstrap (`mcpg/server.py`):
  `create_server`, `make_lifespan`, `AppContext`, `run`. No global state —
  shared state lives in the lifespan. `run` dispatches all three transports,
  so Task 1.5 is reduced to the CLI entry point. 104 tests, 100% coverage.
  Installed the `pre-commit` git hook locally.
- 2026-05-20 — Task 1.4: TDD'd the first tool (`mcpg/tools.py`): `ServerInfo`,
  `build_server_info`, `register_tools` + the `get_server_info` tool. Moved
  `AppContext` to `mcpg/context.py` to break a server/tools import cycle.
  Verified the tool end-to-end via an in-memory MCP client. 108 tests, 100% cov.
- 2026-05-20 — Task 1.5/1.6: TDD'd the `mcpg` CLI entry point
  (`mcpg/__main__.py`) and restored the `[project.scripts]` entry; wired the
  coverage gate into CI (`pytest --cov`). 110 tests, 100% coverage.
  **Phase 1 complete.**
- 2026-05-20 — Task 2.1: integration-test harness (`tests/integration/`):
  `database_url` / `connected_database` fixtures gated on
  `MCPG_TEST_DATABASE_URL`, auto-`integration` marker, 3 real-DB tests for the
  `Database` lifecycle. CI `test` job is now a PG 14–17 service-container
  matrix. Verified locally against PostgreSQL 16. 113 tests, 100% coverage.
- 2026-05-20 — Task 2.2: TDD'd schema introspection (`mcpg/introspection.py`):
  `list_schemas`, `list_tables`, `describe_table`, `list_indexes`,
  `list_extensions` — typed results, parameterised catalog queries — plus
  their MCP tools. Added `FakeDriver`/`FakeDatabase` doubles and real-DB
  integration tests. 126 tests, 100% coverage.
- 2026-05-20 — Task 2.3: TDD'd `run_select` (`mcpg/query.py`) — runs
  agent-supplied SQL through the vendored `SafeSqlDriver` (AST allowlist +
  forced read-only), returns a typed `QueryResult`, wraps rejections/failures
  in `QueryError`. Registered the `run_select` tool. 136 tests, 100% coverage.
- 2026-05-20 — Task 2.4: TDD'd `explain_query` (`mcpg/query.py`) — wraps the
  query in `EXPLAIN (FORMAT JSON)`, validated by the same allowlist, returns a
  typed `ExplainResult`. Registered the `explain_query` tool. 142 tests, 100% cov.
- 2026-05-20 — Task 2.5: TDD'd result shaping for `run_select` — a `max_rows`
  cap (default 1000) with a `truncated` flag on `QueryResult`, exposed as a
  tool parameter. Cursor-style pagination is left to caller SQL
  `LIMIT`/`OFFSET`. 146 tests, 100% coverage. **Phase 2 complete.**
- 2026-05-20 — Task 3.1: TDD'd the access-mode policy engine (`mcpg/policy.py`):
  `Capability` enum + per-mode permission table. `register_tools` now takes the
  access mode and gates registration by capability (all current tools are
  reads; write gating bites in Phase 4). 157 tests, 100% coverage.
- 2026-05-20 — Task 3.2: added an adversarial SQL-safety regression suite
  (`tests/unit/test_sql_safety.py`) — 21 hostile queries (statement stacking,
  comment escapes, transaction-control escapes, DDL/DML, COPY, DO blocks) all
  rejected before reaching the driver; 5 legitimate reads still accepted.
  183 tests, 100% coverage.
- 2026-05-20 — Task 3.3: TDD'd audit logging (`mcpg/audit.py`): `AuditEvent`,
  `redact_arguments` (masks secret-named args, obfuscates embedded passwords),
  `record`. `AuditedFastMCP` overrides `call_tool` so every tool invocation —
  success or error — is logged to the `mcpg.audit` logger. 192 tests, 100% cov.
- 2026-05-20 — Task 3.4: wrote the threat model and security documentation
  (`docs/security.md`) — trust boundaries, threats T1–T5 with mitigations,
  operator responsibilities, scope. Linked docs from the README.
  **Phase 3 complete.**
- 2026-05-20 — Task 4.1: TDD'd `run_write` (`mcpg/write.py`) — parses with
  `pglast`, requires exactly one INSERT/UPDATE/DELETE (blocks statement
  stacking), executes read-write. `register_tools` now takes `Settings` and
  gates the `run_write` tool to unrestricted mode. 209 tests, 100% coverage.
- 2026-05-20 — Task 4.2: TDD'd `run_ddl` (`mcpg/write.py`) — single-statement
  DDL allowlist. Added the `MCPG_ALLOW_DDL` config flag (`Capability.DDL`);
  the `run_ddl` tool is registered only in unrestricted mode with the opt-in
  enabled. 224 tests, 100% coverage.
- 2026-05-20 — Task 4.3: verified write tool calls are audited end-to-end
  (`tests/unit/test_audit.py`). 225 tests, 100% coverage. **Phase 4 complete.**
- 2026-05-20 — Task 5.1: TDD'd database health checks (`mcpg/health.py`):
  `check_connections`, `check_cache_hit_ratio`, `check_dead_tuples`,
  `check_invalid_indexes`, aggregated by `check_database_health` and exposed
  as a tool. Added the `FakeRoutingDriver` test double. 234 tests, 100% cov.
- 2026-05-20 — Task 5.2: TDD'd `analyze_workload` (`mcpg/workload.py`) —
  slowest queries via `pg_stat_statements`, degrading gracefully to
  `available=False` when the extension is absent. 239 tests, 100% coverage.
- 2026-05-20 — Task 5.3: TDD'd `recommend_indexes` (`mcpg/indexing.py`) — a
  table-level heuristic flagging large tables read mostly by sequential scan
  (column-level recommendations deferred to Phase 8). 244 tests, 100% coverage.
- 2026-05-20 — Task 5.4: TDD'd `analyze_query_plan` (`mcpg/query.py`) — walks
  the `EXPLAIN (FORMAT JSON)` tree into a structured summary (total cost,
  estimated rows, node types, sequential scans). 249 tests, 100% coverage.
  **Phase 5 complete.**
- 2026-05-20 — Task 6.1: configurable connection-pool sizing. ADR-0003 chose a
  minimal vendored `DbConnPool` patch (`min_size`/`max_size` params); added
  `MCPG_POOL_MIN_SIZE`/`MCPG_POOL_MAX_SIZE` settings flowing into `Database`.
  256 tests, 100% coverage.
- 2026-05-20 — Task 6.2: multi-tenancy & RLS. Decided document-only for
  v0.1.0 — `docs/security.md` gains a "Multi-tenancy and Row-Level Security"
  section (one instance per tenant with a tenant-specific role). The
  per-request `SET ROLE` mechanism is deferred post-1.0.
- 2026-05-20 — Task 6.3: added `benchmarks/bench.py` (concurrent `run_select`
  throughput/latency harness) and `docs/scaling.md` (execution model, pool
  sizing, measured baseline ~2200 req/s p50 ~7ms, bottleneck guidance).
  Task 6.4 deferred post-1.0; **Phase 6 effectively complete for v0.1.0.**
- 2026-05-20 — Task 7.1: wrote `docs/usage.md` (install, configuration env-var
  table, access modes, running, MCP client config, troubleshooting) and
  `docs/tools.md` (reference for all 14 tools). Linked from the README.
- 2026-05-20 — Task 7.2: added a `uv`-based `Dockerfile` (non-root,
  streamable-HTTP default) and `.dockerignore`; documented Docker usage and a
  README quick start. Not built locally (no Docker in this environment).
- 2026-05-21 — Task 7.3: bumped the version to 0.1.0 (`pyproject.toml`,
  `mcpg.__version__`, `uv.lock`), finalised the CHANGELOG with a `[0.1.0]`
  section. 256 tests green. **Phase 7 complete — v0.1.0 release-ready,
  awaiting user sign-off to tag/publish.**
- 2026-05-20 — Planning: added PostgreSQL extension support to the roadmap
  (`PLAN.md` §7a + Phases 8–11): index-method intelligence (GIN/GiST/BRIN/...),
  `pg_trgm` / full-text search, `pgvector`, PostGIS. Per-extension priority
  table recorded; ordering revisited before Phase 8 starts.
- 2026-05-21 — v0.1.0 merged to `main` via PR #1 (CI green, PG 14–17).
  Branch synced to merged `main`; post-1.0 extension work continues here.
- 2026-05-21 — Task 8.1: `list_indexes` now reports each index's access
  method (btree/gin/gist/brin/hash/spgist) via a `pg_am` catalog join;
  `IndexInfo` gains a `method` field. 256 tests, 100% coverage.
- 2026-05-21 — Task 8.2: added `list_available_extensions` (`pg_available_extensions`)
  reporting every available extension with installed-vs-not status, exposed
  as an MCP tool. 258 tests, 100% coverage.
- 2026-05-21 — Task 8.3: added `mcpg/extensions.py` — `enable_extension`
  runs `CREATE EXTENSION IF NOT EXISTS` for names on a curated allowlist
  (the injection guard, since the name is an identifier). Exposed as a
  DDL-gated MCP tool. 265 tests, 100% coverage.
- 2026-05-21 — Task 8.4: `recommend_indexes` is now index-type aware — a
  single join of `pg_stat_user_tables` + `information_schema.columns` yields
  per-column `IndexSuggestion`s (GIN for `jsonb`/arrays, trigram GIN for
  text). 268 tests, 100% coverage. **Phase 8 complete.**
- 2026-05-21 — Task 9.1: added `mcpg/textsearch.py` — `fuzzy_search` ranks a
  text column by `pg_trgm` trigram similarity, degrading to `available=False`
  when the extension is absent. Identifiers are validated + quoted (the term
  is bound). Shared `extension_installed` helper moved to `mcpg/extensions.py`.
  277 tests, 100% coverage.
- 2026-05-21 — Task 9.2: added `full_text_search` (`mcpg/textsearch.py`) —
  ranks documents via built-in `tsvector`/`websearch_to_tsquery`/`ts_rank`
  (no extension needed); text-search config is identifier-validated.
  285 tests, 100% coverage. **Phase 9 complete.**
- 2026-05-21 — CI now runs the matrix on `pgvector/pgvector:pgNN` images so
  Phase 10 vector tests run for real. Task 10.1: `describe_table` rewritten to
  a `pg_attribute` catalog query; `ColumnInfo` gains `vector_dimension`,
  reported for `vector(N)` columns. 286 tests (1 pgvector test skips locally),
  100% coverage.
- 2026-05-21 — Docs: split `usage.md` into living `docs/installation.md`
  (Installation Guide) and `docs/user-guide.md` (User Guide), and added
  `docs/architecture.md` (Architecture Document). These are maintained
  alongside feature work (see `CONTRIBUTING.md`).
- 2026-05-21 — Task 10.2/10.3: added `vector_search` (pgvector k-NN, `mcpg/
  textsearch.py`); confirmed HNSW index awareness via `list_indexes`.
  291 tests (3 pgvector integration tests run in CI), 100% coverage.
  **Phase 10 complete.** Phases 0–10 done; 19 MCP tools.
- 2026-05-21 — Phase 11: CI now builds a pgvector + PostGIS image
  (`.github/ci-postgres.Dockerfile`); added `geo_search` (PostGIS k-NN by
  distance to a lon/lat point) to `mcpg/textsearch.py`. 296 tests (4
  extension integration tests run in CI), 100% coverage. **Phase 11 complete
  — all eleven planned phases delivered; 20 MCP tools.**
- 2026-05-21 — Live-test of the real server surfaced a `fuzzy_search` UX gap;
  added a `word`/`full` `mode` (default `word`). 306 tests.
- 2026-05-21 — Capability gap analysis (`PLAN.md` §7b): added Phases 12–15 to
  the roadmap — deeper schema introspection, partitioning, access-control
  introspection, and live ops & maintenance.
- 2026-05-21 — Task 12.1: added `list_constraints` — PK/FK/unique/check/
  exclusion constraints on a table, via `pg_constraint`. 309 tests, 100% cov.
- 2026-05-21 — PR #2 (v0.2.0 + Phase 12 start) merged to `main`; branch
  re-synced. Task 12.2: added `list_views` — views and materialized views in
  a schema with definitions, via `pg_class`. 311 tests, 100% coverage.
- 2026-05-21 — Task 12.3: added `list_functions` — functions and procedures
  in a schema (kind, arguments, return type, language), via `pg_proc`.
  313 tests, 100% coverage.
- 2026-05-21 — Task 12.4: added `list_triggers` — user-defined triggers on a
  table (function + definition), via `pg_trigger`. 315 tests, 100% coverage.
- 2026-05-22 — Task 12.5: added `list_sequences` — sequences in a schema
  (data type, range, increment, cycle, last value), via `pg_sequences`.
  Phase 12 complete. 318 tests, 100% coverage.
- 2026-05-22 — Task 13.1: added `list_partitions` — a table's partitioning
  strategy and partitions with bound expressions, via `pg_partitioned_table`
  and `pg_inherits`. 323 tests, 100% coverage.
- 2026-05-22 — Task 13.2: `list_tables` now reads `pg_class` and flags each
  table with `partitioned` and `is_partition`. 325 tests, 100% coverage.
- 2026-05-22 — Task 13.3: `list_indexes` flags partitioned-index templates;
  `recommend_indexes` rolls partition stats up to the partitioned parent.
  Phase 13 complete. 328 tests, 100% coverage.
- 2026-05-22 — Task 14.1: added `list_policies` — Row-Level-Security
  policies on a table (command, permissive, roles, predicates) plus the
  table's RLS-enabled flag, via `pg_policies`. 334 tests, 100% coverage.
- 2026-05-22 — Task 14.2: added `list_roles` — database roles and their
  attributes (superuser, create-role/db, login, replication, bypass-RLS,
  connection limit, membership), via `pg_roles` and `pg_auth_members`.
  340 tests, 100% coverage.
- 2026-05-22 — Task 14.3: added `list_grants` — privileges granted on a
  table (grantee, privilege, grantable, grantor), via
  `information_schema.table_privileges`. Phase 14 complete. 342 tests,
  100% coverage.
- 2026-05-22 — Task 15.1: added `list_active_queries` (new `mcpg/liveops`
  module) — in-flight queries with wait events, duration, and blocking
  PIDs, via `pg_stat_activity` and `pg_blocking_pids`. 346 tests, 100%
  coverage.
- 2026-05-22 — Task 15.2: `check_database_health` gains `replication_lag`
  (via `pg_stat_replication`) and `table_bloat` (catalog-only size
  estimate) checks. 349 tests, 100% coverage.
- 2026-05-22 — Task 15.3: added gated `run_maintenance` (new
  `mcpg/maintenance` module) — VACUUM/ANALYZE on one table via a new
  autocommit `Database.run_unmanaged` path. 357 tests, 100% coverage.
- 2026-05-22 — Task 15.4: added gated `cancel_query` and
  `terminate_backend` — signal a backend PID via `pg_cancel_backend` /
  `pg_terminate_backend`. Phases 12–15 complete. 365 tests, 100%
  coverage.
- 2026-05-23 — Post-Phase-15 roadmap (`PLAN.md` §11) captured: Phases
  16–27 grouped into six batches (catalog completeness, advisors,
  extension wrappers, data movement, replication/events, migrations).
- 2026-05-23 — Task 16.1: added `list_enums`, `list_domains`,
  `list_composite_types` — user-defined types via `pg_type`/`pg_enum`/
  `pg_constraint` and `pg_attribute`. Composite types filter on
  `relkind='c'` to exclude table row-types. 251 unit tests.
- 2026-05-23 — Task 16.2: added `list_foreign_data_wrappers`,
  `list_foreign_servers`, `list_foreign_tables`, `list_user_mappings` —
  postgres_fdw and other wrappers via `pg_foreign_data_wrapper`,
  `pg_foreign_server`, `pg_foreign_table`, `pg_user_mappings`. Text[]
  options parsed into typed dicts. 255 unit tests.
- 2026-05-23 — Task 16.3: added `list_publications` and
  `list_subscriptions` — read-only logical-replication catalog via
  `pg_publication`/`pg_publication_tables` and `pg_subscription`.
  Phase 16 complete (29 MCP tools total). 257 unit tests, 100% coverage.
- 2026-05-23 — PR #4 merged to `main` (Phase 16); branch re-synced.
- 2026-05-23 — Phase 17: added `list_foreign_keys` (structured FK
  introspection via `pg_constraint`/`pg_attribute` aligned by ordinal)
  and `generate_schema_diagram` (new `mcpg.diagrams` module) — a
  Mermaid ER renderer with PK/FK column markers, parent→child edges,
  cross-schema edge filtering, and an `include_partitions` knob.
  Phase 17 complete (31 MCP tools total). 396 tests, 100% coverage.
- 2026-05-23 — `PLAN.md` §11 gained Batch G (ORM bridges): Phase 28
  `generate_prisma_schema` flagged as a deliberate USP. Sibling tools
  for Drizzle, SQLAlchemy, sqlc may follow; scope deliberately narrow
  (catalog → DSL only, no DSL→DDL parsing, no `prisma migrate` driving).
- 2026-05-23 — PR #5 merged to `main` (Phase 17); branch re-synced.
- 2026-05-23 — Phase 18: added `compare_schemas` (new
  `mcpg.schema_diff` module) — structural diff between two schemas
  reporting tables / columns / indexes / constraints / foreign keys as
  added, removed, or changed. Identity is by name (renames = paired
  add + remove). Column changes carry a `fields_changed` list of
  differing `ColumnInfo` fields. Built on a small generic name-keyed
  helper `_diff_by_name[T, C]` shared across the four per-table object
  kinds. **Batch A complete** (catalog completeness → visualisation →
  diff). Phase 18 complete (32 MCP tools total). 409 tests, 100%
  coverage. `FakeParamRoutingDriver` test fake added so the diff can
  be exercised in unit tests with two distinct fake schemas.
