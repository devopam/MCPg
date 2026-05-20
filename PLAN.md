# MCPg — PostgreSQL MCP Server: Master Plan

> Status: **living document**. This is the single source of truth for the
> project plan. Session-to-session progress is tracked in
> [`docs/PROGRESS.md`](docs/PROGRESS.md). Architecture decisions are recorded
> in [`docs/adr/`](docs/adr/).

## 1. Vision

Build **MCPg**, a production-grade [Model Context Protocol](https://modelcontextprotocol.io)
server for PostgreSQL that lets AI agents safely **inspect, query, operate, and
tune** a Postgres database. It targets a *broad* scope: both an application data
access layer (safe CRUD/query) and a database operations toolkit (health checks,
index tuning, EXPLAIN analysis), with which tools are exposed gated by an
**access mode**.

### Why this project

- The official `@modelcontextprotocol/server-postgres` was **deprecated &
  archived (July 2025)** after a SQL-injection CVE. There is no maintained
  reference implementation.
- The strongest active project is **`crystaldba/postgres-mcp`** ("Postgres MCP
  Pro", MIT, Python). It is a credible base and Phase 0 evaluates whether to
  fork it or build greenfield.

## 2. Guiding principles

1. **Security first.** Read-only by default. No string-interpolated SQL. Every
   statement parsed/validated before execution. Treat the database connection
   string and query results as sensitive.
2. **Test-Driven Development.** Red → Green → Refactor. No production code
   without a failing test first. Coverage gate enforced in CI.
3. **Resumable.** Work is decomposed into small, independently shippable tasks.
   `docs/PROGRESS.md` always reflects exact current state so any session can
   resume cold (see §8).
4. **Best practices everywhere.** Consistent taxonomy, typed code, documented
   tools, ADRs for decisions, semantic versioning, conventional commits.
5. **Incremental & honest.** Each phase ends with something runnable and
   demoed. No half-finished phases.

## 3. Technology selection (recommended)

| Concern            | Choice                                  | Rationale |
|--------------------|-----------------------------------------|-----------|
| Language           | Python 3.12+                            | Best MCP+Postgres ecosystem; keeps fork option open |
| MCP framework      | Official `mcp` Python SDK               | First-party, supports stdio + streamable HTTP |
| Postgres driver    | `psycopg` 3 (async)                     | Modern, async, server-side cursors, pipeline mode |
| Connection pooling | `psycopg_pool`                          | Async pool, health checks |
| SQL parsing        | `pglast` (libpg_query)                  | Real Postgres grammar; classify read vs write, block escapes |
| Test runner        | `pytest`, `pytest-asyncio`              | TDD workhorse |
| Integration tests  | `testcontainers[postgres]`              | Real Postgres per test session, no mocks for DB behavior |
| Packaging / env    | `uv`                                    | Fast, reproducible, lockfile |
| Lint / format      | `ruff`                                  | Fast, single tool |
| Types              | `mypy --strict`                         | Catch interface drift |
| Coverage           | `pytest-cov` + CI gate                  | Enforce TDD discipline |
| CI                 | GitHub Actions                          | Matrix over PG 14–17 |
| Distribution       | PyPI + Docker image (`distroless`)      | `uvx mcpg` and container deploy |

> Decision is provisional until ADR-0001 is accepted at end of Phase 0.

## 4. Architecture overview

```
            ┌─────────────────────────────────────────────┐
            │              MCP Client (agent)              │
            └───────────────┬─────────────────────────────┘
                  stdio / streamable HTTP
            ┌───────────────▼─────────────────────────────┐
            │                 MCPg server                  │
            │  ┌────────────┐  ┌──────────────────────┐    │
            │  │ Transport  │  │ Tool registry         │    │
            │  └────────────┘  │  (gated by AccessMode)│    │
            │  ┌────────────┐  └──────────────────────┘    │
            │  │ AccessMode │  ┌──────────────────────┐    │
            │  │ policy     │  │ SQL guard (pglast)    │    │
            │  └────────────┘  └──────────────────────┘    │
            │  ┌────────────┐  ┌──────────────────────┐    │
            │  │ Conn pool  │  │ Audit log             │    │
            │  └────────────┘  └──────────────────────┘    │
            └───────────────┬─────────────────────────────┘
                            │ psycopg3 async
            ┌───────────────▼─────────────────────────────┐
            │              PostgreSQL 14–17                 │
            └───────────────────────────────────────────────┘
```

### Access modes (security posture)

| Mode          | Tools exposed                                    | Transaction |
|---------------|--------------------------------------------------|-------------|
| `read-only`   | introspection + SELECT + ops/tuning (read)       | `READ ONLY`, enforced |
| `restricted`  | read-only set + statement timeout + row limits   | `READ ONLY` + guards |
| `unrestricted`| all of the above + write/DDL/maintenance tools   | read-write  |

## 5. Tool taxonomy

Tools are namespaced and named `verb_noun`. Stable contract; documented in
`docs/tools/`. Planned set (final list refined per phase):

- **Introspection** — `list_schemas`, `list_tables`, `describe_table`,
  `list_indexes`, `list_extensions`, `get_server_info`
- **Query** — `run_select` (parsed, read-only), `explain_query`
- **Write** (unrestricted) — `run_write`, `run_ddl` (each parsed + audited)
- **Ops & health** — `check_database_health`, `analyze_connections`,
  `analyze_vacuum`, `analyze_replication`
- **Tuning** — `analyze_workload`, `recommend_indexes`, `analyze_query_plan`

Error taxonomy: every tool returns structured errors with a stable `code`
(`E_ACCESS_DENIED`, `E_SQL_REJECTED`, `E_TIMEOUT`, `E_CONNECTION`, ...).

## 6. Data modeling & conventions

- **Config**: env-var driven (`MCPG_DATABASE_URL`, `MCPG_ACCESS_MODE`, ...),
  validated via a typed settings model; secrets never logged.
- **Result shape**: tools return typed JSON — `columns`, `rows`, `row_count`,
  `truncated`, plus `notices`. Large results paginated/capped.
- **Naming**: `snake_case` tools/params; `ADR-NNNN` decisions; conventional
  commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- **Versioning**: SemVer; `CHANGELOG.md` (Keep a Changelog format).

## 7. Phased roadmap

Each phase is a milestone; each task is TDD (failing test first). Tasks are
sized to fit comfortably within a single session. Checklists live in
`docs/PROGRESS.md`.

### Phase 0 — Spike & foundation
- Hands-on evaluation of `crystaldba/postgres-mcp`: code quality, test coverage,
  license, extensibility, maintenance health.
- Write **ADR-0001** (fork vs hard-fork vs greenfield) and **ADR-0002** (stack).
- Scaffold repo: `uv` project, `pyproject.toml`, `ruff`/`mypy` config,
  `pytest` layout, GitHub Actions CI, pre-commit, `CONTRIBUTING.md`.
- Deliverable: green CI on an empty test, decision recorded.

### Phase 1 — Core server skeleton
- MCP server bootstrap; stdio transport; streamable HTTP transport.
- Typed config/settings loader; connection pool lifecycle.
- `get_server_info` tool as the first end-to-end TDD vertical slice.
- Deliverable: server connects to Postgres, one tool callable from an MCP client.

### Phase 2 — Schema introspection & safe reads
- Introspection tools; `run_select` with `pglast` read-only enforcement;
  `explain_query`; result shaping, row caps, pagination.
- Deliverable: agent can fully explore + query a DB read-only.

### Phase 3 — Security hardening & access control
- Access-mode policy engine gating the tool registry.
- SQL guard: block multi-statement, `COMMIT`/`ROLLBACK` escapes, DDL in
  read-only; statement timeout; row-limit enforcement.
- Audit log; secrets-handling review; SQL-injection regression suite (the CVE
  class that killed the official server).
- Deliverable: documented, tested security posture; threat model in `docs/`.

### Phase 4 — Write & DDL tools
- `run_write`, `run_ddl` gated to `unrestricted`; explicit transaction control;
  dry-run/preview; per-write audit entries.
- Deliverable: safe, audited write path.

### Phase 5 — Ops, health & tuning
- Health checks (indexes, cache, connections, vacuum, replication, sequences).
- `analyze_workload` via `pg_stat_statements`; `recommend_indexes`;
  `analyze_query_plan` with `hypopg` hypothetical indexes.
- Deliverable: production tuning toolkit.

### Phase 6 — Scalability & multi-tenancy
- Pool sizing/tuning, server-side cursors for big reads, backpressure.
- Multi-tenant scoping; Row-Level-Security awareness; read-replica routing.
- Load/soak test harness.
- Deliverable: documented scaling characteristics + benchmarks.

### Phase 7 — Docs, packaging & release
- Support pages: README, usage guide, tool reference (`docs/tools/`),
  security page, troubleshooting, FAQ; site scaffold optional.
- PyPI publish, Docker image, install instructions (`uvx`, Docker, source).
- `v0.1.0` release with `CHANGELOG.md`; if forking, upstream contribution.

## 8. Resume protocol (work across session limits)

To resume at any time, a new session must:
1. Read `PLAN.md` (this file) and `docs/PROGRESS.md`.
2. `docs/PROGRESS.md` contains: current phase, per-task checklist with status,
   "Next action" pointer, open questions, and a decisions log.
3. Pick up the first unchecked task under "Next action".
4. On finishing meaningful work: update `docs/PROGRESS.md`, commit, push.

**Every session ends by committing an updated `docs/PROGRESS.md`** so state is
never lost when limits are hit.

## 9. Definition of done (per task)

- [ ] Failing test written first, then code to pass it.
- [ ] `ruff`, `mypy --strict`, full test suite green; coverage gate met.
- [ ] Public behavior documented (tool doc / docstring).
- [ ] `docs/PROGRESS.md` updated; conventional-commit pushed.

## 10. Open questions

- Hosting target for streamable HTTP (auth model for remote use)?
- Should tuning tools require a separate opt-in beyond `unrestricted`?
- Telemetry/observability scope (OpenTelemetry?) — revisit in Phase 6.

These are tracked and resolved via ADRs as phases reach them.
