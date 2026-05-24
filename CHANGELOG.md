# Changelog

All notable changes to MCPg are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `run_advisors` tool — runs a set of codified, catalog-driven lint
  rules against a schema and returns a typed report of findings. First
  cut covers: `missing_primary_key`, `unindexed_foreign_key` (leading-
  column heuristic), `duplicate_indexes` (same column-keys + access
  method), and `nullable_timestamp_without_tz`. Each finding carries a
  rule id, severity (`warning`/`info`), a qualified object name, and a
  human-readable message. Advisory only — no writes.
- `generate_prisma_schema` tool — read a PostgreSQL schema and emit a
  valid Prisma `.prisma` schema string, mirroring `prisma db pull` but
  driven by MCPg. Covers tables, columns, primary/foreign keys
  (including composite), unique constraints, secondary indexes, and
  enums; standard defaults (`nextval(...)` → `autoincrement()`, `now()`
  → `now()`, `gen_random_uuid()` → `uuid()`, literals) and array types
  are mapped; unmappable types (vectors, custom domains) fall back to
  `Unsupported("...")` exactly like `prisma db pull`. Views, foreign
  tables, partitions, triggers, functions, policies, and composite
  types are out of scope for v1. **First USP-tier tool — no other PG
  MCP server bridges to an ORM schema DSL.**
- `tune_vector_index` tool — recommends an `ivfflat` or `hnsw`
  configuration for a pgvector column. Reads the live row count
  (`pg_class.reltuples`) and column dimension, applies the standard
  pgvector heuristics (lists ≈ rows/1000 or sqrt for ivfflat; m
  scales with size, ef_construction with size for hnsw), and returns
  the parameters plus a ready-to-run `CREATE INDEX` statement.
- `vector_recall_at_k` tool — measures recall@k of an existing
  pgvector index by comparing its top-k results against a brute-force
  ground truth for the same query vectors. Uses pgvector's distance
  functions (`l2_distance` / `cosine_distance` / `inner_product`) as
  the non-indexed baseline; the operator form (`<->`, `<=>`, `<#>`)
  triggers the ANN index.
- `list_cron_jobs` tool — read pg_cron's `cron.job` catalog. Returns an
  empty list when pg_cron is not installed (graceful degradation).
- `schedule_cron_job` and `unschedule_cron_job` tools (write-gated) —
  thin wrappers over `cron.schedule()` / `cron.unschedule()`. Raise
  `CronError` when pg_cron is not installed.
- `partman_create_parent`, `partman_run_maintenance`,
  `partman_drop_partition` tools (write-gated) — pg_partman
  partition-set creation, periodic maintenance (forward partitions +
  retention drops), and explicit retention-based drops (time- or
  id-controlled). `partition_type` is allowlisted to
  range/list/native. Raise `PartmanError` when pg_partman is not
  installed.
- `pg_cron` and `pg_partman` added to `ENABLEABLE_EXTENSIONS` — agents
  can request enabling them (still gated on unrestricted mode +
  `MCPG_ALLOW_DDL`; pg_cron also requires server-side
  `shared_preload_libraries`).

## [0.3.0] - 2026-05-23

Twelve new MCP tools, closing Batch A of the post-0.2.0 roadmap
(`PLAN.md` §11): catalog completeness (Phase 16), schema visualisation
(Phase 17), and structural schema diff (Phase 18). Brings the total
MCP tool surface from 33 to 45 and lays the structural foundation for
Phase 27 shadow migrations.

### Added

- `list_foreign_keys` tool — every foreign key in a schema, resolved to
  its from-columns, referenced schema, referenced table, and
  to-columns. The two column arrays are aligned by ordinal position.
- `generate_schema_diagram` tool — renders a Mermaid ER diagram for a
  schema (entities with PK/FK column markers, edges parent → child).
  Views and foreign tables are excluded; partitions are excluded by
  default and can be included with ``include_partitions=true``.
- `compare_schemas` tool — structural diff between two schemas. Reports
  tables / columns / indexes / constraints / foreign keys as added,
  removed, or changed; column changes include the list of differing
  ColumnInfo fields. Object identity is by name; renames surface as a
  paired add + remove. Foundation for the Phase-27 shadow-migration
  workflow.
- `list_constraints` tool — a table's primary-key, foreign-key, unique,
  check, and exclusion constraints.
- `list_views` tool — the views and materialized views in a schema, with
  their definitions.
- `list_functions` tool — the functions and procedures in a schema, with
  kind, arguments, return type, and language.
- `list_triggers` tool — the user-defined triggers on a table.
- `list_sequences` tool — the sequences in a schema, with each sequence's
  data type, range, increment, cycle flag, and last value.
- `list_partitions` tool — how a table is partitioned (range, list, or
  hash) and its partitions, each with its bound expression.
- `list_policies` tool — the Row-Level-Security policies on a table, with
  each policy's command, permissive flag, roles, and predicates, plus
  whether row security is enabled on the table.
- `list_roles` tool — the database roles and their attributes (superuser,
  create-role/db, login, replication, bypass-RLS, connection limit, and
  role membership).
- `list_grants` tool — the privileges granted on a table, with each
  grant's grantee, privilege, grantable flag, and grantor.
- `list_active_queries` tool — the queries currently running on the
  server, each with its wait event, duration, and blocking PIDs.
- `check_database_health` gains two checks — replication lag (how far
  connected standbys trail) and table bloat (tables far larger than their
  estimated minimum size).
- `run_maintenance` tool — runs `VACUUM` or `ANALYZE` against one table;
  requires unrestricted mode. Runs on an autocommit connection, since
  `VACUUM` cannot run inside a transaction.
- `cancel_query` and `terminate_backend` tools — signal a backend PID to
  cancel its current query or close its connection; require unrestricted
  mode.
- `list_enums`, `list_domains`, `list_composite_types` tools — the
  user-defined types in a schema. Composite types report each attribute
  with its rendered type; the catalog's implicit table row-types are
  excluded.
- `list_foreign_data_wrappers`, `list_foreign_servers`,
  `list_foreign_tables`, `list_user_mappings` tools — the FDW catalog,
  with each entry's options array parsed into a typed dict.
- `list_publications` and `list_subscriptions` tools — read-only view of
  logical-replication publications (with the tables and operations they
  cover) and subscriptions; reading subscriptions requires superuser, by
  PostgreSQL design.
- `postgres_fdw` added to `ENABLEABLE_EXTENSIONS` — agents can now
  enable the wrapper they can already introspect (gated on unrestricted
  mode + `MCPG_ALLOW_DDL`).

### Changed

- `list_tables` now flags each table with `partitioned` (a partitioned
  parent) and `is_partition` (itself a partition).
- `list_indexes` now flags each index with `partitioned` (a
  partitioned-index template).
- `recommend_indexes` now rolls a flagged partition up to its partitioned
  parent — summing scan and row counts and setting a `partitioned` flag —
  since an index created on the parent propagates to every partition.
- The "every introspection tool is callable" check moved from the unit
  suite (fakes-only) to the integration suite — it now runs against the
  real catalog across the PG 14–17 CI matrix, closing a trust gap the
  unit-level fake driver couldn't reach.

## [0.2.0] - 2026-05-21

Extension support: index-method intelligence, extension management, and
similarity-search tools (trigram, full-text, pgvector, PostGIS) — six new
tools, each degrading gracefully when its extension is absent.

### Added

- `list_available_extensions` tool — lists every extension available to the
  database with its installed-vs-available status.
- `enable_extension` tool — enables an allowlisted PostgreSQL extension;
  requires unrestricted mode and `MCPG_ALLOW_DDL`.
- `fuzzy_search` tool — ranks a text column by `pg_trgm` trigram similarity
  to a search term, with a `word` mode (fragment matching, the default) and
  a `full` mode (whole-string comparison).
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
