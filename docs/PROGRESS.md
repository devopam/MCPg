# MCPg — Progress Tracker

> **Resume here.** A new session should read `PLAN.md` then this file, then
> start the task under **Next action**. Update this file and commit before
> ending any session.

## Current state

- **Phase:** 0 — Spike & foundation
- **Last updated:** 2026-05-20
- **Branch:** `claude/postgresql-mcp-planning-8KssU`

## Next action

> Phase 0, Task 0.5 — add GitHub Actions CI (ruff + mypy + pytest, with a
> Postgres version matrix) and confirm a first green run.

## Phase 0 — Spike & foundation

- [x] 0.1 Evaluate `crystaldba/postgres-mcp` (code, tests, license, activity) → ADR-0001 (hard-fork)
- [x] 0.2 Confirm/record stack → ADR-0002 (Python 3.12 + psycopg3 + mcp SDK)
- [x] 0.3 Vendor `sql/` subpackage (MIT, `NOTICE` + `_vendor/README.md`); scaffold `uv` project
- [x] 0.4 Configure `ruff`, `mypy --strict`, `pytest`, `pytest-cov`, coverage gate (in `pyproject.toml`)
- [ ] 0.5 GitHub Actions CI (lint + types + tests, PG 14–17 matrix)
- [ ] 0.6 `CONTRIBUTING.md`, pre-commit hooks, issue/PR templates
- [ ] 0.7 First green CI run on a placeholder test

### Phase 0 notes

- Vendored kernel lives in `src/mcpg/_vendor/sql/`; 75 upstream tests ported and
  passing (`tests/vendor/`). `test_db_conn_pool` and `test_readonly_enforcement`
  were NOT ported — they couple to upstream `server.py`; re-derive under TDD in
  Phase 1/3.
- `uv sync` + `uv run pytest tests/vendor` + `ruff` + `mypy src/mcpg` all green
  locally.

## Phase 1 — Core server skeleton (not started)
## Phase 2 — Schema introspection & safe reads (not started)
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
