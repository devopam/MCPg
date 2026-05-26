# Changelog

All notable changes to MCPg are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Four more catalog → DSL exporters under the same Batch G umbrella.
  Tool surface 74 → 78. All read-only, no new capability or env-var
  gates. Coverage matches the existing exporters (Prisma / Drizzle /
  SQLAlchemy 2.0 / sqlc): base tables, columns, primary keys, single-
  column intra-schema foreign keys, enums. Cross-schema FKs and
  composite FKs are documented v1 gaps.
  - `generate_diesel_schema` — emits a Diesel ORM (Rust) `schema.rs`
    with one `table!` macro per table, `Nullable<T>` wrappers for
    nullable columns, `joinable!` lines for intra-schema FKs, and an
    `allow_tables_to_appear_in_same_query!` macro so multi-table
    joins type-check. Enum types are emitted as Text-backed wrapper
    enums in a `pg_enum` module so the output works without
    `diesel_derive_enum`.
  - `generate_jooq_config` — emits a `jooq-codegen` configuration
    XML pointing at the database. Unlike the other exporters, jOOQ
    generates Java code itself from the live database at build
    time; the artefact here is the XML the user feeds to
    `mvn jooq-codegen:generate`. Includes an explicit `<includes>`
    regex naming every base table, an `<excludes>` covering MCPg's
    bookkeeping schemas, and a `<forcedType>` for every json / jsonb
    column so they map to `org.jooq.JSON` / `org.jooq.JSONB`.
  - `generate_ent_schemas` — emits Ent (Go) Schema struct files,
    one `.go` per table. Each struct lists `field.X(...)` calls,
    `edge.To(...)` lines for single-column FKs, and
    `field.Enum().Values()` for enum-typed columns. Returns a
    `{filename: source}` dict.
  - `generate_ecto_schemas` — emits Ecto (Elixir) schema modules,
    one `.ex` per table named after the singularised table
    (matching the Phoenix `lib/my_app/<singular>.ex` convention).
    Each module uses `use Ecto.Schema`, declares `@primary_key`,
    `field` for each column, `belongs_to` for single-column FKs,
    and `timestamps()` when both `inserted_at` + `updated_at`
    exist. The Elixir top-level module is configurable via the
    `app_module` arg (default `MyApp`).

## [0.4.0] - 2026-05-26

Twenty-nine new MCP tools, closing **Batches D / E / F / G** of the
post-0.3.0 roadmap (`PLAN.md` §11). Brings the total MCP tool surface
from **45 to 74** and ships the long-planned cross-cutting features:
the data-movement family, the LISTEN/NOTIFY bridge, the agent-driven
migration shadow workflow, and three new ORM-DSL exporters
(Drizzle / SQLAlchemy 2.0 / sqlc) alongside the existing Prisma one.

### Headline features

- **Batch D — data movement (5 tools).** `dump_database` /
  `restore_database` round-trip a database through `pg_dump` /
  `psql` / `pg_restore` via the ADR-0004 subprocess gate.
  `copy_table_between_databases` pipes one database's table into
  another in one shell pipeline. `import_csv` /
  `import_json` bulk-load via in-process `COPY ... FROM STDIN`
  and parametrised `executemany` — no subprocess gate needed.
- **Batch E — LISTEN/NOTIFY bridge (4 tools), ADR-0005.**
  `subscribe_channel` / `poll_notifications` /
  `unsubscribe_channel` / `list_notification_subscriptions`
  let an agent react to PostgreSQL events through a polled,
  per-subscription bounded queue. New `Capability.LISTEN` +
  `MCPG_ALLOW_LISTEN` opt-in.
- **Batch F — staged-migration workflow (4 tools), ADR-0006.**
  `prepare_migration` clones a target schema's structure into a
  shadow schema via introspection, applies a candidate SQL there,
  and runs `compare_schemas` so the agent reviews the structural
  delta. `complete_migration` lands it on the target.
  `cancel_migration` / `list_pending_migrations` round out the
  workflow. Same-database shadow (no full-DB clone). New
  `Capability.MIGRATE` reuses the existing `MCPG_ALLOW_DDL` opt-in.
- **Batch G — catalog → DSL exporters (3 new tools).**
  `generate_drizzle_schema` (Drizzle ORM TypeScript),
  `generate_sqlalchemy_models` (SQLAlchemy 2.0 declarative Python),
  `generate_sqlc_schema` (replayable plain DDL for sqlc). All
  read-only — drop into any agentic project as a starting point.

### Fixed

- PR #17 code-review findings (10 fixes across the Batches D / E / F / G
  surfaces):
  1. `restore_database` for custom/tar formats now passes
     `--dbname=postgresql:///` so pg_restore actually connects (it
     previously fell into "convert to SQL script" mode without `-d`).
  2. `ListenManager` recovers from a dead listener connection — the
     reader-loop clears `_conn` and sets `_needs_resubscribe`, the next
     subscribe opens a fresh conn and re-issues LISTEN for every active
     channel (previously the manager silently stopped delivering after
     any PG restart).
  3. Migration DDL replay only rewrites schema references on
     `foreign_key` constraints, not on every constraint type — a CHECK
     constraint whose literal happens to contain the target schema
     name (e.g. `CHECK (path LIKE 'public.%')`) is no longer corrupted.
  4. `mcpg.sqlc` enum labels are now apostrophe-escaped (PG-standard
     `''` doubling) so labels like `O'Brien` don't break the DDL.
  5. `mcpg.sqlalchemy_export` enum generator falls back to the
     functional `enum.Enum("Name", {...})` form when any label isn't a
     valid Python identifier (`in-progress`, `1st`, `class`, ...),
     keeping the generated file importable.
  6. `mcpg.drizzle` default rendering now translates PG escape rules to
     JS escape rules in the right order: `''` → `'`, backslash → `\\`,
     `"` → `\"`. Previously `'it''s'` became `"it''s"` and `'a\nb'`
     silently injected a newline.
  7. Shadow schema names are capped to fit PostgreSQL's 63-byte
     NAMEDATALEN limit, preventing silent truncation that would leak
     shadow schemas the workflow couldn't clean up.
  8. The migration shadow-workflow now refuses candidate SQL containing
     statements PG won't run inside a transaction block (CREATE INDEX
     CONCURRENTLY, VACUUM, ALTER SYSTEM, ...) with a clear error
     pointing the user at `run_ddl` instead.
  9. `mcpg.shell._write_stdin` always closes the child's stdin in a
     `finally` block — a non-`BrokenPipeError` from `write`/`drain`
     no longer leaks the pipe and wedges the child.
  10. `ListenManager.close()` bounds the `conn.close()` await at 2s so
      a libpq close hanging on a half-open socket can't wedge server
      shutdown.

- PR #17 code-review findings (10 fixes across the Batches D / E / F / G
  surfaces):
  1. `restore_database` for custom/tar formats now passes
     `--dbname=postgresql:///` so pg_restore actually connects (it
     previously fell into "convert to SQL script" mode without `-d`).
  2. `ListenManager` recovers from a dead listener connection — the
     reader-loop clears `_conn` and sets `_needs_resubscribe`, the next
     subscribe opens a fresh conn and re-issues LISTEN for every active
     channel (previously the manager silently stopped delivering after
     any PG restart).
  3. Migration DDL replay only rewrites schema references on
     `foreign_key` constraints, not on every constraint type — a CHECK
     constraint whose literal happens to contain the target schema
     name (e.g. `CHECK (path LIKE 'public.%')`) is no longer corrupted.
  4. `mcpg.sqlc` enum labels are now apostrophe-escaped (PG-standard
     `''` doubling) so labels like `O'Brien` don't break the DDL.
  5. `mcpg.sqlalchemy_export` enum generator falls back to the
     functional `enum.Enum("Name", {...})` form when any label isn't a
     valid Python identifier (`in-progress`, `1st`, `class`, ...),
     keeping the generated file importable.
  6. `mcpg.drizzle` default rendering now translates PG escape rules to
     JS escape rules in the right order: `''` → `'`, backslash → `\\`,
     `"` → `\"`. Previously `'it''s'` became `"it''s"` and `'a\nb'`
     silently injected a newline.
  7. Shadow schema names are capped to fit PostgreSQL's 63-byte
     NAMEDATALEN limit, preventing silent truncation that would leak
     shadow schemas the workflow couldn't clean up.
  8. The migration shadow-workflow now refuses candidate SQL containing
     statements PG won't run inside a transaction block (CREATE INDEX
     CONCURRENTLY, VACUUM, ALTER SYSTEM, ...) with a clear error
     pointing the user at `run_ddl` instead.
  9. `mcpg.shell._write_stdin` always closes the child's stdin in a
     `finally` block — a non-`BrokenPipeError` from `write`/`drain`
     no longer leaks the pipe and wedges the child.
  10. `ListenManager.close()` bounds the `conn.close()` await at 2s so
      a libpq close hanging on a half-open socket can't wedge server
      shutdown.

### Added

- ORM-bridge exporters — Batch G follow-ons (Phase 28b/c/d). Three
  new MCP tools sit alongside the existing `generate_prisma_schema`
  under the schema→DSL umbrella:
  - `generate_drizzle_schema` — emit a Drizzle ORM TypeScript schema
    (`drizzle-orm/pg-core`) covering tables, columns with PG-native
    types (incl. `serial`/`bigserial` from `nextval` defaults, length
    on varchar, `withTimezone` on timestamptz), single-column FKs as
    column-level `.references(() => ...)`, primary/unique/check
    constraints, indexes, defaults, and enums via `pgEnum`. The
    helper-import line is computed from what was actually emitted, so
    unused helpers don't clutter the output.
  - `generate_sqlalchemy_models` — emit a SQLAlchemy 2.0 declarative
    models file (`DeclarativeBase` + `Mapped[T]` + `mapped_column`)
    with PG types from both `sqlalchemy` core and
    `sqlalchemy.dialects.postgresql` (jsonb), single-column FKs via
    `ForeignKey("schema.table.col")`, composite uniques in
    `__table_args__`, enum types emitted as Python `enum.Enum`
    classes, and `server_default=text(...)` / `func.now()` for
    defaults. Composite FKs are a documented v1 gap.
  - `generate_sqlc_schema` — emit a sqlc-friendly `schema.sql` (plain
    DDL) ordered for clean replay: `CREATE SCHEMA` → `CREATE TYPE`
    enums → `CREATE TABLE` (columns only) → `ALTER TABLE ADD
    CONSTRAINT` (PK / unique / check / FK in that order) → `CREATE
    INDEX` for non-constraint indexes. In-process — no
    `MCPG_ALLOW_SHELL` needed.
  All three are read-only; gated by the standard READ capability.

- Staged-migration workflow — Batch F (Phase 27), per ADR-0006. New
  `mcpg.migrations` module implements Neon-style "branch the schema,
  test the migration, merge" with same-database shadow schemas (no
  `pg_dump` shell-out, no cross-batch dependency on Batch D). Four
  new MCP tools:
  - `prepare_migration(name, target_schema, candidate_sql,
    ttl_minutes=60)` clones the target schema's structure into a
    fresh `mcpg_shadow_<id>` schema via introspection-driven DDL
    replay (tables + columns, PK / UNIQUE / CHECK / FK constraints,
    indexes), applies `candidate_sql` against the shadow with
    `SET LOCAL search_path` so unqualified identifiers resolve there,
    runs `compare_schemas(target, shadow)`, and persists the staged
    row in `mcpg_migrations.staged`. Returns the migration id +
    shadow schema name + TTL + structural diff for review.
  - `complete_migration(id)` applies the candidate SQL to the
    target schema and drops the shadow. Refuses if status is not
    `prepared` or TTL has expired.
  - `cancel_migration(id)` drops the shadow and marks the row
    `cancelled`. Idempotent.
  - `list_pending_migrations()` lists prepared migrations newest
    first; sweeps any expired prepared rows before listing.
  Intra-schema FK references are rewritten to point at the shadow;
  cross-schema FKs are left pointing at the original and surface in
  the diff as removed (documented limitation per ADR-0006).
- New `Capability.MIGRATE` enum entry; the migration tools register
  under unrestricted mode + the existing `MCPG_ALLOW_DDL` opt-in
  (the underlying ops are DDL).
- New `mcpg_migrations` schema + `staged` table created idempotently
  on first migration call. State columns: `id`, `prepared_at`,
  `target_schema`, `shadow_schema`, `candidate_sql`, `status`
  (`prepared` / `completed` / `cancelled` / `expired`),
  `ttl_expires_at`, `completed_at`.

- LISTEN/NOTIFY bridge — Batch E first slice, per ADR-0005. New
  `mcpg.listen` module owns the server-lifetime subscription state.
  Four new MCP tools:
  - `subscribe_channel(channel)` opens a PostgreSQL `LISTEN` on the
    given channel (validated against the standard plain-identifier
    allowlist) and returns a subscription id. Notifications buffer
    in a per-subscription bounded queue.
  - `poll_notifications(subscription_id, timeout_ms, max_messages)`
    drains up to `max_messages` from the queue, waiting at most
    `timeout_ms` for the first one when the queue is empty. Each
    `{channel, payload, delivered_at, dropped_count}` notification
    surfaces drop count only on the first message after an overflow
    so the caller is informed exactly once.
  - `unsubscribe_channel(subscription_id)` removes a subscription;
    `UNLISTEN` fires when the last subscription on a channel is gone.
  - `list_notification_subscriptions()` reports the active
    `{subscription_id, channel}` pairs for visibility.
  A single dedicated PostgreSQL connection (separate from the request
  pool) holds every active LISTEN, opened lazily on first subscribe.
  A background `asyncio.Task` drains psycopg's notifies generator
  with a short polling timeout so subscribe/unsubscribe `execute()`
  calls can land between iterations (the psycopg connection lock
  would otherwise deadlock concurrent admin commands). Queue overflow
  drops the oldest message and surfaces `dropped_count` on the next
  poll.
- New `Capability.LISTEN` enum entry. Two new env vars:
  `MCPG_ALLOW_LISTEN` (bool, default `false`) toggling the
  subscription tool surface; `MCPG_LISTEN_QUEUE_MAX` (default 1000)
  capping per-subscription buffer size.
- `AppContext.listen_manager` exposes the manager to every tool;
  `create_server` accepts an optional `listen_manager` keyword arg so
  tests can inject a fake connection factory.

- `copy_table_between_databases` tool — copy a single table from one
  database to another by piping `pg_dump --format=custom --table=...`
  (source) into `pg_restore --format=custom --single-transaction
  --exit-on-error` (destination). Both legs run through the ADR-0004
  shell runner with separate libpq env dicts derived from the source
  and destination URLs; credentials never appear on argv. `include_schema`
  and `include_data` flags are required (no implicit default) so the
  caller can't accidentally copy the wrong half. If the captured
  pg_dump archive exceeds `MCPG_SHELL_MAX_OUTPUT_BYTES`, the tool
  raises before invoking pg_restore — a truncated custom-format archive
  would either fail obscurely or partially restore. A failed pg_dump
  short-circuits the same way, returning the dump stderr_tail with
  `restore_exit_code=-1` as a sentinel. Gated under unrestricted mode
  + `MCPG_ALLOW_SHELL`.

- `import_csv` tool — bulk-load CSV content into `schema.table` via
  `COPY ... FROM STDIN`. CSV text is sent verbatim; `header` toggles
  header-row skipping; optional `columns` restricts loading to named
  columns (each validated against the plain-identifier allowlist).
  Delimiter is restricted to a single non-newline, non-quote character
  so it cannot terminate the COPY options list early. Returns the
  server-reported row count. Gated under unrestricted mode (WRITE
  capability) — no subprocess, no `MCPG_ALLOW_SHELL` needed.
- `import_json` tool — bulk-load a JSON array of objects into
  `schema.table` via parametrised `INSERT ... executemany`. Columns
  are derived from the first row's keys (or supplied explicitly);
  nested `dict`/`list` values are JSON-serialised so they round-trip
  into `jsonb` columns; missing keys in later rows bind as `NULL`.
  Values are bound — never spliced into SQL — so they cannot inject
  statements. Gated under unrestricted mode (WRITE capability).
- `Database.copy_from_stdin` and `Database.execute_many` helpers —
  in-process plumbing for COPY FROM STDIN and `executemany`, used by
  the new import tools. The vendored `SqlDriver` exposes neither, so
  imports go through the `Database` wrapper for raw connection access.

- `restore_database` tool — restore a dump into the connected database
  via the ADR-0004 subprocess gate. `format='plain'` pipes SQL text
  through `psql --single-transaction --set=ON_ERROR_STOP=on` so a
  syntax error rolls back the whole restore; `format='custom'`/`'tar'`
  base64-decode the payload and pipe the binary archive into
  `pg_restore --single-transaction --exit-on-error`. Credentials reach
  the binary via libpq env vars; the dump bytes flow through stdin and
  are never interpolated into argv. Gated on unrestricted mode +
  `MCPG_ALLOW_SHELL`.

### Fixed

- `mcpg.shell.run_pg_binary` now writes the optional `stdin` payload
  concurrently with the stdout/stderr drain. The previous "write
  stdin after wait()" ordering would have deadlocked any subprocess
  that consumes stdin (`pg_restore`, `psql -f -`); no shipped tool
  used stdin yet, but the bug blocked `restore_database` from working.

- `dump_database` tool — wraps `pg_dump` to capture the connected
  database's schema (and optionally data) as a plain-SQL string or
  base64-encoded binary archive. Implements the ADR-0004 subprocess
  policy: argv-only invocation, allowlisted binaries, hard timeout,
  output cap with truncation flag, credentials passed via libpq env
  vars (never on the command line). Gated behind a new
  `Capability.SHELL` + `MCPG_ALLOW_SHELL` opt-in on top of
  unrestricted access mode.
- New `MCPG_ALLOW_SHELL` env var (bool, default `false`) toggling the
  whole subprocess-tool surface. Two companion knobs:
  `MCPG_SHELL_TIMEOUT_SEC` (default 60) and `MCPG_SHELL_MAX_OUTPUT_BYTES`
  (default 64 MiB).
- `Capability.SHELL` added to the policy table; required for any tool
  that invokes an external binary.
- `export_query` tool — run a read-only SQL query and serialise the
  rows to CSV or JSON. Reuses the safety checks of `run_select` and
  truncates at the supplied row limit with a `truncated` flag in the
  result so callers can paginate.
- `export_table` tool — serialise every row in a `schema.table` (up
  to the supplied limit) to CSV or JSON. Identifier names must match
  the plain SQL allowlist; anything that needs delimited-identifier
  quoting is rejected.
- `list_audit_events` tool — read recent rows from `mcpg_audit.events`
  (newest first). Returns an empty list when `MCPG_AUDIT_PERSIST` has
  never been turned on (no audit table yet). Optional tool-name filter.
- New `MCPG_AUDIT_PERSIST` env var (bool, default `false`). When on,
  every `run_write` / `run_ddl` call appends one row to
  `mcpg_audit.events` containing redacted arguments, status, error, and
  result. Persistence failures are swallowed so audit logging never
  masks the real write outcome.
- `run_ddl` gains optional `schema` / `table` hints. When both are
  supplied, the call snapshots the table's columns before and after the
  DDL and attaches the structured before/after lists to the result as a
  `SchemaDiffSnapshot`. The snapshot is also stored in the persisted
  audit row when `MCPG_AUDIT_PERSIST` is on.
- PostgreSQL 18 added to the CI test matrix (was 14–17; now 14–18). The
  integration suite runs against every supported version on every PR.
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
