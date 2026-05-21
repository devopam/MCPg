# MCPg — Progress Tracker

> **Resume here.** A new session should read `PLAN.md` then this file, then
> start the task under **Next action**. Update this file and commit before
> ending any session.

## Current state

- **Phase:** 7 complete — v0.1.0 release-ready (awaiting sign-off)
- **Last updated:** 2026-05-21
- **Branch:** `claude/postgresql-mcp-planning-8KssU`

## Next action

> Release prep is done (version 0.1.0, CHANGELOG finalised). **Awaiting user
> sign-off** to tag `v0.1.0` and publish (PyPI / GitHub release) — these are
> side-effecting and must not be done unprompted. After v0.1.0: Phase 8
> (index intelligence & extension management) — see `PLAN.md` §7a.

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

- [x] 7.1 Usage guide (`docs/usage.md`) + tool reference (`docs/tools.md`)
- [x] 7.2 Packaging — `Dockerfile`, `.dockerignore`, install instructions
- [x] 7.3 v0.1.0 release prep — version bumped to 0.1.0, CHANGELOG finalised.
      Tagging/publishing awaits explicit user sign-off.

## Phase 8 — Index intelligence & extension management (not started)

- Report index access methods (B-tree/GIN/GiST/BRIN/Hash/SP-GiST) in `list_indexes`.
- `list_available_extensions`; `enable_extension` tool (gated DDL, allowlist).
- **Revisit `recommend_indexes` (Task 5.3)** — make it index-type aware:
  trigram GIN for `LIKE`/fuzzy, GIN for `jsonb`/arrays, BRIN for append-only,
  and (with Phase 10) HNSW/IVFFlat for `vector` columns.

## Phase 9 — Text search & fuzzy matching, incl. `pg_trgm` (not started)
## Phase 10 — Vector search (`pgvector`) (not started)
## Phase 11 — Geospatial (PostGIS), optional (not started)

> Phases 8–11 cover PostgreSQL extension and advanced-feature support; see
> `PLAN.md` §7a for the capability inventory and per-extension priorities.

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
