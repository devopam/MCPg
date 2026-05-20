# MCPg — Progress Tracker

> **Resume here.** A new session should read `PLAN.md` then this file, then
> start the task under **Next action**. Update this file and commit before
> ending any session.

## Current state

- **Phase:** 2 — Schema introspection & safe reads
- **Last updated:** 2026-05-20
- **Branch:** `claude/postgresql-mcp-planning-8KssU`

## Next action

> Phase 2, Task 2.5 — TDD result shaping for `run_select`: a configurable row
> cap with a `truncated` flag (pagination if warranted).

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

## Phase 2 — Schema introspection & safe reads

- [x] 2.1 Integration-test harness (`tests/integration/`) + PG 14–17 CI service matrix
- [x] 2.2 Introspection tools: `list_schemas`, `list_tables`, `describe_table`,
      `list_indexes`, `list_extensions` (`mcpg/introspection.py`, TDD)
- [x] 2.3 `run_select` — read-only-enforced query execution via vendored `SafeSqlDriver` (`mcpg/query.py`, TDD)
- [x] 2.4 `explain_query` tool (`mcpg/query.py`, TDD)
- [ ] 2.5 Result shaping — typed result, row caps, pagination (TDD)
## Phase 3 — Security hardening & access control (not started)
## Phase 4 — Write & DDL tools (not started)
## Phase 5 — Ops, health & tuning (not started)
## Phase 6 — Scalability & multi-tenancy (not started)
## Phase 7 — Docs, packaging & release (not started)

## Decisions log

| ID  | Decision | Status | Date |
|-----|----------|--------|------|
| —   | Scope: broad (ops + data access, gated by access mode) | accepted | 2026-05-20 |
| ADR-0001 | Approach: hard-fork `crystaldba/postgres-mcp` (MIT); TDD-hybrid (strict TDD for new code, characterization tests for inherited kernel) | accepted | 2026-05-20 |
| ADR-0002 | Stack: Python 3.12 + psycopg3 + `mcp` SDK + pglast; `mypy --strict` + coverage gate for new code | accepted | 2026-05-20 |

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
