# MCPg — Progress Tracker

> **Resume here.** A new session should read `PLAN.md` then this file, then
> start the task under **Next action**. Update this file and commit before
> ending any session.

## Current state

- **Phase:** 0 — Spike & foundation
- **Last updated:** 2026-05-20
- **Branch:** `claude/postgresql-mcp-planning-8KssU`

## Next action

> Phase 0, Task 0.1 — hands-on evaluation of `crystaldba/postgres-mcp` and
> write ADR-0001 (fork vs hard-fork vs greenfield).

## Phase 0 — Spike & foundation

- [ ] 0.1 Evaluate `crystaldba/postgres-mcp` (code, tests, license, activity) → ADR-0001
- [ ] 0.2 Confirm/record stack → ADR-0002
- [ ] 0.3 Scaffold `uv` project (`pyproject.toml`, src layout, package skeleton)
- [ ] 0.4 Configure `ruff`, `mypy --strict`, `pytest`, `pytest-cov`, coverage gate
- [ ] 0.5 GitHub Actions CI (lint + types + tests, PG 14–17 matrix)
- [ ] 0.6 `CONTRIBUTING.md`, pre-commit hooks, issue/PR templates
- [ ] 0.7 First green CI run on a placeholder test

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
| —   | Approach: decide fork vs greenfield after Phase 0 spike | accepted | 2026-05-20 |
| —   | Stack: Python 3.12 + psycopg3 + mcp SDK (provisional, see ADR-0002) | provisional | 2026-05-20 |

## Open questions

- Remote HTTP transport auth model (Phase 1/3).
- Whether tuning tools need opt-in beyond `unrestricted` (Phase 5).
- Observability scope (Phase 6).

## Session log

- 2026-05-20 — Researched ecosystem, created `PLAN.md` + this tracker.
  Official MCP Postgres server confirmed deprecated/archived; `crystaldba/postgres-mcp`
  identified as strongest base. Plan committed; Phase 0 ready to start.
