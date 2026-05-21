# Changelog

All notable changes to MCPg are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `list_available_extensions` tool — lists every extension available to the
  database with its installed-vs-available status.
- `enable_extension` tool — enables an allowlisted PostgreSQL extension;
  requires unrestricted mode and `MCPG_ALLOW_DDL`.
- `fuzzy_search` tool — ranks a text column by `pg_trgm` trigram similarity
  to a search term.
- `full_text_search` tool — ranks documents with PostgreSQL's built-in
  `tsvector`/`tsquery` full-text search.
- `vector_search` tool — finds the rows nearest to a query vector by
  `pgvector` distance (`l2`, `cosine`, or `inner_product`).
- `geo_search` tool — finds the rows nearest to a lon/lat point by PostGIS
  distance.

### Changed

- `list_indexes` now reports each index's access method (`btree`, `gin`,
  `gist`, `brin`, `hash`, `spgist`).
- `recommend_indexes` now suggests per-column index types from column data
  types — GIN for `jsonb`/array columns, trigram GIN for text columns.
- `describe_table` now reads the catalog directly and reports the
  `pgvector` dimension for `vector(N)` columns.
- Documentation reorganised into living guides: `docs/installation.md`,
  `docs/user-guide.md`, and `docs/architecture.md` (replacing `docs/usage.md`).

## [0.1.0] - 2026-05-21

First release: a production-grade PostgreSQL MCP server with 14 tools across
introspection, querying, writes, and tuning — read-only by default, every
statement validated, every tool call audited.

### Added

- Project plan, phased roadmap, and session-resume protocol (`PLAN.md`,
  `docs/PROGRESS.md`).
- ADR-0001 (build approach: hard-fork) and ADR-0002 (technology stack).
- Vendored the self-contained `sql/` SQL-safety kernel from
  `crystaldba/postgres-mcp` @ `07eb329` (MIT) into `src/mcpg/_vendor/sql/`,
  with the upstream unit tests that port cleanly.
- Project scaffold: `pyproject.toml`, packaging, `ruff`/`mypy`/`pytest`/
  coverage configuration, `NOTICE`.
- GitHub Actions CI (`.github/workflows/ci.yml`): lint, format, type-check,
  and test jobs.
- `CONTRIBUTING.md`, local `pre-commit` hooks, and GitHub issue/PR templates.
- Env-driven configuration (`mcpg.config`): `Settings`, `AccessMode`,
  `Transport`, and `load_settings`. Read-only is the default access mode and
  the settings repr redacts database credentials.
- Database connection lifecycle (`mcpg.database`): `Database` wraps the pool
  with connect/close, async-context-manager support, and a typed
  `DatabaseError`.
- MCP server bootstrap (`mcpg.server`): `create_server` builds a configured
  `FastMCP` whose lifespan owns the settings and database (no global state);
  `run` serves over the stdio, streamable-HTTP, or SSE transport.
- First MCP tool, `get_server_info` (`mcpg.tools`): reports the server
  version, access mode, transport, and database connection status.
- Console entry point: `mcpg` (and `python -m mcpg`) loads configuration
  and runs the server.
- CI now enforces the test-coverage gate (90% of authored code).
- Integration-test harness (`tests/integration/`) running against a live
  PostgreSQL; CI exercises the suite against PostgreSQL 14, 15, 16, and 17.
- Schema-introspection tools (`mcpg.introspection`): `list_schemas`,
  `list_tables`, `describe_table`, `list_indexes`, and `list_extensions`,
  using parameterised read-only catalog queries.
- Safe query execution (`mcpg.query`): the `run_select` tool validates
  agent-supplied SQL against an allowlist and runs it read-only, returning a
  typed result; unsafe statements are rejected.
- The `explain_query` tool returns a query's `EXPLAIN (FORMAT JSON)`
  execution plan without running the query.
- `run_select` caps results at a configurable `max_rows` (default 1000) and
  reports whether the result was `truncated`.
- Access-mode policy engine (`mcpg.policy`): tool registration is gated by
  capability, so the available tools depend on the configured access mode.
- Adversarial SQL-safety regression suite covering statement stacking,
  comment and transaction-control escapes, DDL/DML, `COPY`, and `DO` blocks.
- Audit logging (`mcpg.audit`): every tool invocation is logged to the
  `mcpg.audit` logger with its outcome and arguments, with secrets masked.
- Security documentation (`docs/security.md`): threat model, trust
  boundaries, mitigations, and operator responsibilities.
- Write execution (`mcpg.write`): the `run_write` tool executes a single
  validated INSERT/UPDATE/DELETE statement, available only in unrestricted
  access mode; statement stacking is rejected.
- The `run_ddl` tool executes a single validated DDL statement; it requires
  unrestricted access mode and the `MCPG_ALLOW_DDL` opt-in.
- Database health checks (`mcpg.health`): the `check_database_health` tool
  reports connection utilisation, buffer cache hit ratio, tables needing
  vacuum, and invalid indexes.
- Workload analysis (`mcpg.workload`): the `analyze_workload` tool reports
  the slowest queries via `pg_stat_statements`, degrading gracefully when
  the extension is not installed.
- Index recommendations (`mcpg.indexing`): the `recommend_indexes` tool
  flags large tables read mostly by sequential scan.
- Query plan analysis (`mcpg.query`): the `analyze_query_plan` tool
  summarises a query's execution plan — total cost, estimated rows, node
  types, and sequentially-scanned tables.
- Configurable connection-pool sizing via `MCPG_POOL_MIN_SIZE` and
  `MCPG_POOL_MAX_SIZE` (defaults 1 and 5).
- Multi-tenancy / Row-Level Security guidance in `docs/security.md`.
- Scaling documentation (`docs/scaling.md`) and a benchmark harness
  (`benchmarks/bench.py`).
- Usage guide (`docs/usage.md`), tool reference (`docs/tools.md`), and a
  `uv`-based `Dockerfile`.
