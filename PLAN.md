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
- Configurable pool sizing (done, ADR-0003); server-side cursors for big
  reads, backpressure.
- Multi-tenancy: for v0.1.0, **document-only** RLS guidance — one MCPg
  instance per tenant with a tenant-specific role (`docs/security.md`). An
  optional per-request `SET ROLE` / session-variable mechanism is deferred
  post-1.0 (pooled-connection session-state management). Read-replica routing
  also post-1.0.
- Load/soak test harness.
- Deliverable: documented scaling characteristics + benchmarks.

### Phase 7 — Docs, packaging & release
- Support pages: README, usage guide, tool reference (`docs/tools/`),
  security page, troubleshooting, FAQ; site scaffold optional.
- PyPI publish, Docker image, install instructions (`uvx`, Docker, source).
- `v0.1.0` release with `CHANGELOG.md`; if forking, upstream contribution.

### Phases 8–11 — Extension & feature support (post-1.0)

A phased build-out of PostgreSQL extension and advanced-feature awareness.
See §7a for the full capability inventory and rationale.

- **Phase 8 — Index intelligence & extension management.** Report index
  access methods (B-tree/GIN/GiST/BRIN/Hash/SP-GiST) in introspection;
  `list_available_extensions`; `enable_extension` (gated DDL, known-extension
  allowlist); make `recommend_indexes` index-type aware (GIN for
  `jsonb`/arrays, trigram GIN for `LIKE`, BRIN for append-only).
- **Phase 9 — Text search & fuzzy matching.** `pg_trgm` similarity/fuzzy
  search tool; built-in full-text search (`tsvector`/`tsquery`) helper;
  `unaccent` / `fuzzystrmatch` awareness.
- **Phase 10 — Vector search (pgvector).** `vector` column awareness in
  introspection; k-NN similarity-search tool (`<->`, `<=>`, `<#>`);
  HNSW / IVFFlat index awareness in `list_indexes` and `recommend_indexes`.
- **Phase 11 — Geospatial (PostGIS) [optional].** `geometry`/`geography`
  awareness, spatial-index reporting, bounding-box / distance query helpers.

Deliverable per phase: new tools + introspection upgrades, fully TDD'd, with
graceful degradation when an extension is absent (as `analyze_workload`
already does for `pg_stat_statements`).

## 7a. Extension & feature capability inventory

What "support" means here: MCPg should (a) **detect** which extensions are
installed vs available, (b) make introspection **extension-aware** (index
types, special column types), (c) offer **gated management** to enable known
extensions, and (d) expose **feature tools** that use them. Every feature
degrades gracefully when its extension is absent.

### Index access methods (built-in — awareness only)

| Method   | Best for                                   | Phase |
|----------|--------------------------------------------|-------|
| B-tree   | equality / range on scalars (default)      | done  |
| GIN      | `jsonb`, arrays, full-text, trigram        | 8     |
| GiST     | ranges, geometry, full-text, nearest-neighbour | 8 |
| BRIN     | very large naturally-ordered tables        | 8     |
| Hash     | equality only                              | 8     |
| SP-GiST  | non-balanced / partitioned data            | 8     |

### Extensions by category

| Extension            | Capability                                  | Priority | Phase |
|----------------------|---------------------------------------------|----------|-------|
| `pg_stat_statements` | query workload stats                        | —        | done (5.2) |
| `hypopg`             | hypothetical indexes for tuning             | high     | 5.4   |
| `pg_trgm`            | trigram similarity, fuzzy `LIKE`            | high     | 9     |
| `pgvector`           | `vector` type, similarity search, HNSW/IVF  | high     | 10    |
| built-in FTS         | `tsvector`/`tsquery` full-text search       | high     | 9     |
| `unaccent`           | accent-insensitive search                   | medium   | 9     |
| `fuzzystrmatch`      | soundex, levenshtein                        | medium   | 9     |
| `citext`             | case-insensitive text type                  | medium   | 8 (awareness) |
| `hstore`             | key-value type                              | low      | 8 (awareness) |
| `pgcrypto`/`uuid-ossp` | crypto / UUID generation                  | low      | 8 (awareness) |
| `ltree`              | hierarchical data                           | low      | later |
| PostGIS              | geospatial types & indexes                  | medium   | 11    |
| `pgstattuple`        | table/index bloat estimation                | medium   | 8 (health) |
| `pg_partman`/`postgres_fdw` | partitioning / federation             | low      | later |

> **Operator note:** some extensions (`pg_stat_statements`, parts of PostGIS,
> `hypopg`) need `shared_preload_libraries` or superuser to install. MCPg
> detects and uses them but cannot always enable them itself.

> **Priority ordering** (per user direction emphasising pgvector, GIN,
> trigram): Phase 8 (index intelligence) → 9 (text/trigram) → 10 (pgvector)
> → 11 (PostGIS). Re-orderable; revisit before starting Phase 8.

## 7b. Capability gap analysis — Phases 12–15 (post-extension)

After the extension phases, MCPg still lacks introspection and operations for
several core PostgreSQL areas. Partition DDL already runs via `run_ddl`, but
nothing is partition-*aware*; similarly there is no view of constraints,
non-table objects, RLS policies, roles, or live activity.

### Phase 12 — Deeper schema introspection
- `list_constraints` — primary keys, foreign keys, unique, check, exclusion.
- `list_views` (+ definitions), `list_functions`, `list_triggers`,
  `list_sequences`.
- Deliverable: an agent can see a table's full structure, not just columns.

### Phase 13 — Partitioning
- `list_partitions` — partition strategy (range/list/hash), bounds,
  parent↔partition links; flag partitioned tables in `list_tables`.
- Make `list_indexes` / `recommend_indexes` partition-aware (parent vs
  per-partition indexes; aggregate partition scan stats).
- Deliverable: partitioned schemas are correctly understood, including the
  index interaction.

### Phase 14 — Access-control introspection
- `list_policies` — Row-Level-Security policies on a table (supports the
  multi-tenant / partition-per-tenant story).
- `list_roles`, `list_grants` — roles and table/object privileges.
- Deliverable: "who can access what", and RLS visibility.

### Phase 15 — Live ops & maintenance
- `list_active_queries`, lock / blocking inspection (`pg_stat_activity`,
  `pg_locks`).
- Replication-lag and table/index bloat health checks (extends
  `check_database_health`).
- Gated maintenance: `run_maintenance` (`VACUUM`/`ANALYZE`),
  `cancel_query` / `terminate_backend`.
- Deliverable: diagnose and act on a running database.

Each phase is TDD'd with unit + real-PostgreSQL integration tests, like
Phases 0–11. Ordering 12 → 15; re-orderable.

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

## 11. Post-Phase-15 roadmap (Phases 16–27)

After Phases 12–15 closed (365 tests, 100% coverage), a second round of
capability selection picked eleven themes spanning catalog completeness,
advisors, extension wrappers, data movement, replication, events, and
migrations. The work is grouped into six deliverable batches; each batch
opens its own feature branch and pull request.

### Batch A — Catalog completeness & visualisation
- **Phase 16** — Introspection gaps: `list_enums`, `list_domains`,
  `list_composite_types`, `list_foreign_data_wrappers`,
  `list_foreign_servers`, `list_foreign_tables`, `list_user_mappings`,
  `list_publications`, `list_subscriptions`.
- **Phase 17** — Schema visualisation: `generate_schema_diagram`
  returning a Mermaid ER diagram (tables, columns, PK/FK, partitions).
- **Phase 18** — Schema diff: `compare_schemas(left, right)` returning a
  structured diff. Foundation for Batch F.
- **Phase 19** — Storage & cost telemetry: `analyze_storage`;
  `wal_volume` health check.

### Batch B — Advisors & trust
- **Phase 20** — Advisors / lint layer: `run_advisors` with codified
  rules (missing PKs, unindexed FKs, RLS gaps, duplicate indexes, etc).
- **Phase 21** — Audit trail with semantic diff: `run_write`/`run_ddl`
  emit a structured diff alongside their result; optional persistence
  to an `mcpg_audit` schema (off by default).

### Batch C — Extension power-tools
- **Phase 22** — `pg_cron` + `pg_partman` wrappers: list / schedule /
  unschedule / create-parent / run-maintenance / drop-partition.
- **Phase 23** — pgvector tuning: `tune_vector_index`,
  `vector_recall_at_k`.

### Batch D — Data movement (LARGE — gated on ADR-0004)
- **Phase 24** — Export/import: `export_query`, `export_table`,
  `import_csv`/`import_json`, `dump_database`/`restore_database`,
  `copy_table_between_databases`. Subprocess execution is a new attack
  surface; ADR-0004 must define the allowlist / opt-in / redaction
  policy before any code is written.

### Batch E — Replication & event streams (LARGE — gated on ADR-0005)
- **Phase 25** — Logical replication management: replication slots and
  publication/subscription create+drop wrappers (write-gated).
- **Phase 26** — `LISTEN`/`NOTIFY` bridge. ADR-0005 picks between a
  polling model (recommended) and MCP notifications.

### Batch G — ORM bridges (USP)
- **Phase 28** — `generate_prisma_schema`: read the PG catalog and emit a
  valid `.prisma` schema (mirrors `prisma db pull`). High-leverage
  differentiator for TS/JS agentic workflows; sibling tools for Drizzle,
  SQLAlchemy, and sqlc can follow under the same "schema → ORM DSL"
  umbrella. Scope is deliberately narrow: catalog → DSL only, no
  `.prisma` → DDL parsing and no `prisma migrate` subprocess driving.

### Batch F — Migrations with shadow workflow (LARGEST — gated on ADR-0006)
- **Phase 27** — `prepare_migration`/`complete_migration` driven by the
  Phase-18 schema diff. ADR-0006 picks between same-DB shadow schema
  (Option 1, recommended) and side-channel `CREATE DATABASE ... TEMPLATE`
  (Option 2, heavyweight).

Cadence: per-task TDD; 90% coverage gate (current 100%); ruff / mypy /
PG 14–17 CI matrix must be green before each commit. Batches D, E, F
require their ADR to be merged and signed off before implementation
begins.
