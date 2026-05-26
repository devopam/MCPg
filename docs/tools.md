# MCPg Tool Reference

The tools MCPg exposes over MCP, grouped by category. Availability depends on
the configured access mode (see the [User Guide](user-guide.md)).

## Capability gates at a glance

| Capability | Default access mode | Extra opt-in | What it unlocks |
|---|---|---|---|
| READ | every mode | — | catalog introspection, query, search, health, ORM exporters |
| WRITE | unrestricted | — | `run_write`, `run_maintenance`, `cancel_query`, `terminate_backend`, `import_csv`, `import_json`, `pg_cron` writes |
| DDL | unrestricted | `MCPG_ALLOW_DDL=true` | `run_ddl`, `enable_extension`, `pg_partman` write tools, **migration tools** (`MIGRATE` capability piggybacks on this gate) |
| SHELL | unrestricted | `MCPG_ALLOW_SHELL=true` | `dump_database`, `restore_database`, `copy_table_between_databases` |
| LISTEN | unrestricted | `MCPG_ALLOW_LISTEN=true` | `subscribe_channel`, `poll_notifications`, `unsubscribe_channel`, `list_notification_subscriptions` |

## Tool index (78 tools)

| Category | Tools |
|---|---|
| **Server** | `get_server_info` |
| **Catalog — schemas / tables / columns** | `list_schemas`, `list_tables`, `describe_table`, `list_indexes`, `list_constraints`, `list_views`, `list_functions`, `list_triggers`, `list_partitions`, `list_roles`, `list_grants`, `list_policies`, `list_sequences`, `list_enums`, `list_domains`, `list_composite_types`, `list_foreign_keys`, `list_foreign_data_wrappers`, `list_foreign_servers`, `list_foreign_tables`, `list_user_mappings`, `list_publications`, `list_subscriptions`, `list_extensions`, `list_available_extensions` |
| **Visualisation & structural diff** | `generate_schema_diagram`, `compare_schemas` |
| **Query intelligence** | `run_select`, `explain_query`, `analyze_query_plan` |
| **Health & tuning** | `check_database_health`, `analyze_workload`, `recommend_indexes`, `run_advisors` |
| **Search** | `fuzzy_search`, `full_text_search`, `vector_search`, `geo_search` |
| **Vector tuning advisors** | `recommend_vector_index`, `analyze_vector_search`, `analyze_vector_table` |
| **Live ops** | `list_active_queries` |
| **Audit trail** | `list_audit_events` |
| **Data movement — read** | `export_query`, `export_table` |
| **Data movement — write (gated)** | `import_csv`, `import_json` |
| **Data movement — subprocess (gated)** | `dump_database`, `restore_database`, `copy_table_between_databases` |
| **LISTEN/NOTIFY bridge (gated)** | `subscribe_channel`, `poll_notifications`, `unsubscribe_channel`, `list_notification_subscriptions` |
| **Staged migrations (gated)** | `prepare_migration`, `complete_migration`, `cancel_migration`, `list_pending_migrations` |
| **ORM-DSL exporters** | `generate_prisma_schema`, `generate_drizzle_schema`, `generate_sqlalchemy_models`, `generate_sqlc_schema`, `generate_diesel_schema`, `generate_jooq_config`, `generate_ent_schemas`, `generate_ecto_schemas` |
| **pg_cron write (gated)** | `pg_cron.schedule`, `pg_cron.unschedule`, `pg_cron.update` |
| **pg_partman write (gated)** | `partman.create_parent`, `partman.run_maintenance`, `partman.drop_partition_time` |
| **Write & DDL (gated)** | `run_write`, `run_ddl`, `run_maintenance`, `cancel_query`, `terminate_backend`, `enable_extension` |

## Server

### `get_server_info`
Returns the MCPg version, access mode, transport, and database connection
status. *Available in every mode.*

## Introspection (read)

### `list_schemas`
Lists database schemas. Parameter: `include_system` (bool, default `false`) —
include PostgreSQL's own schemas.

### `list_tables`
Lists the tables and views in a schema. Each entry carries a `partitioned`
flag (the table is a partitioned parent) and an `is_partition` flag (the
table is itself a partition). Parameter: `schema` (string).

### `describe_table`
Describes a table's columns in ordinal order — name, data type, nullability,
default, and (for `pgvector` `vector(N)` columns) the vector dimension.
Parameters: `schema`, `table` (strings).

### `list_indexes`
Lists the indexes on a table, each with its access method — a built-in one
(`btree`, `gin`, `gist`, `brin`, `hash`, `spgist`) or an extension's (e.g.
`hnsw`/`ivfflat` from `pgvector`) — and a `partitioned` flag (a
partitioned-index template propagated to each partition). Parameters:
`schema`, `table` (strings).

### `list_constraints`
Lists a table's constraints — each with its `type` (`primary_key`,
`foreign_key`, `unique`, `check`, `exclusion`, or `other`) and definition.
Parameters: `schema`, `table` (strings).

### `list_views`
Lists the views and materialized views in a schema — each with a
`materialized` flag and its definition. Parameter: `schema` (string).

### `list_functions`
Lists the functions and procedures in a schema — each with its `kind`
(`function`, `procedure`, `aggregate`, `window`, `other`), arguments, return
type, and language. Parameter: `schema` (string).

### `list_triggers`
Lists the user-defined triggers on a table — each with the function it calls
and its definition. Internal (constraint-enforcement) triggers are excluded.
Parameters: `schema`, `table` (strings).

### `list_partitions`
Describes how a table is partitioned and lists its partitions. Returns
`partitioned` (bool), `strategy` (`range`, `list`, `hash`, or `null`), and
`partitions` — each with its `name` and `bounds` expression. Parameters:
`schema`, `table` (strings).

### `list_roles`
Lists the database roles and their attributes — `superuser`, `create_role`,
`create_db`, `can_login`, `replication`, `bypass_rls`, `connection_limit`,
and `member_of` (roles each role belongs to). Parameter: `include_system`
(bool, default `false`) — include PostgreSQL's own `pg_*` roles.

### `list_grants`
Lists the privileges granted on a table — each with its `grantee`,
`privilege` (`SELECT`, `INSERT`, `UPDATE`, ...), `grantable` flag (`WITH
GRANT OPTION`), and `grantor`. Parameters: `schema`, `table` (strings).

### `list_policies`
Lists the Row-Level-Security policies on a table. Returns `rls_enabled`
(bool — policies are inert while off) and `policies` — each with its
`command`, `permissive` flag, `roles`, and `using`/`check` expressions.
Parameters: `schema`, `table` (strings).

### `list_sequences`
Lists the sequences defined in a schema — each with its data type, start
value, range (`min_value`/`max_value`), increment, `cycle` flag, and
`last_value` (`null` if unused or not readable). Parameter: `schema` (string).

### `list_enums`
Lists the enum types in a schema, each with its labels in sort order.
Parameter: `schema` (string).

### `list_domains`
Lists the domain types in a schema — each with its base type, nullable
flag, default expression, and the rendered `CHECK` constraint
definitions attached to the domain. Parameter: `schema` (string).

### `list_composite_types`
Lists the standalone composite types in a schema with their attributes
(name + rendered type). The catalog's implicit table row-types are
excluded. Parameter: `schema` (string).

### `list_foreign_keys`
Lists every foreign key in a schema, resolved to its `from_columns`,
referenced `to_schema`/`to_table`, and `to_columns`. The two column
arrays are aligned by ordinal position. Parameter: `schema` (string).

### `list_foreign_data_wrappers`
Lists the foreign-data wrappers installed in the database — name,
handler/validator (qualified function names or `null`), and options dict.

### `list_foreign_servers`
Lists the foreign servers defined in the database — name, wrapper, type,
version, and options dict.

### `list_foreign_tables`
Lists the foreign tables in a schema — name, server, and options dict.
Parameter: `schema` (string).

### `list_user_mappings`
Lists role-to-foreign-server mappings. The catch-all `PUBLIC` mapping
surfaces as `user="public"`.

### `list_publications`
Lists the logical-replication publications in the database — owner, the
`all_tables` flag, per-publication operations (`publishes_insert`,
`publishes_update`, `publishes_delete`, `publishes_truncate`), and the
qualified table names included (empty when `all_tables` is true).

### `list_subscriptions`
Lists the logical-replication subscriptions in the database — name,
owner, enabled flag, connection string, and the publications it
consumes. Reading `pg_subscription` requires superuser; non-privileged
roles get an empty list.

### `list_extensions`
Lists the extensions installed in the database.

### `list_available_extensions`
Lists every extension available to the database — name, default version,
installed version, and whether it is `installed`.

## Visualisation & diff (read)

### `generate_schema_diagram`
Renders a Mermaid ER diagram for a schema as a single string the agent
can paste into any Mermaid-aware renderer. Entities carry PK/FK column
markers; edges point from referenced parent to referencing child. Views
and foreign tables are excluded; partitions are excluded by default.
Parameters: `schema` (string), `include_partitions` (bool, default
`false`).

### `compare_schemas`
Returns the structural diff between two schemas. Reports tables added /
removed, and per-changed-table the same trichotomy for columns, indexes,
constraints, and foreign keys (`columns_changed` entries carry a
`fields_changed` list of differing `ColumnInfo` field names). Base
tables only; views and custom types are not compared. Identity is by
name — renames surface as a paired add + remove. Parameters:
`left_schema` and `right_schema` (strings).

## Query (read)

### `run_select`
Validates and runs a read-only SQL query. The statement is parsed and checked
against a safety allowlist; writes, DDL, and statement stacking are rejected.
Parameters: `sql` (string), `max_rows` (int, default 1000). Returns columns,
rows, `row_count`, and `truncated`.

### `explain_query`
Returns a query's `EXPLAIN (FORMAT JSON)` execution plan without running it.
Parameter: `sql` (string).

### `analyze_query_plan`
Summarises a query's execution plan — total cost, estimated rows, node types,
and sequentially-scanned tables. Parameter: `sql` (string).

## Health & tuning (read)

### `check_database_health`
Runs health checks: connection utilisation, buffer cache hit ratio, tables
needing vacuum, invalid indexes, replication lag (how far connected
standbys trail), and table bloat (tables far larger than their estimated
minimum size). Returns an overall `status` plus per-check results.

### `analyze_workload`
Returns the slowest queries by mean execution time, via the
`pg_stat_statements` extension. Parameter: `limit` (int, default 10). Reports
`available: false` if the extension is not installed.

### `recommend_indexes`
Flags large tables read mostly by sequential scan, and for each suggests
per-column index types from the column's data type — GIN for `jsonb` and
array columns, trigram GIN for text columns. A flagged partition is rolled
up to its partitioned parent (where the index belongs), with scan and row
counts summed across partitions and a `partitioned` flag set. Parameter:
`min_live_tuples` (int, default 10000).

### `fuzzy_search`
Ranks a text column's values by `pg_trgm` trigram similarity to a search
term. Parameters: `schema`, `table`, `column`, `term` (strings), `mode`
(`word` — default — matches fragments within longer text; `full` compares
whole strings), `limit` (int, default 10), `threshold` (float, default 0.3).
Reports `available: false` if `pg_trgm` is not installed.

### `full_text_search`
Ranks a text column's documents against a full-text query using PostgreSQL's
built-in `tsvector`/`tsquery` (no extension required). The query accepts
web-search syntax (quoted phrases, `or`, `-` exclusion). Parameters:
`schema`, `table`, `column`, `search_query` (strings), `config` (string,
default `english`), `limit` (int, default 10).

### `vector_search`
Finds the rows nearest to a query vector by `pgvector` distance. Parameters:
`schema`, `table`, `column` (strings), `query_vector` (array of numbers),
`metric` (`l2`, `cosine`, or `inner_product`; default `l2`), `limit` (int,
default 10). Each match is the row (excluding the embedding column) plus its
`distance`. Reports `available: false` if `pgvector` is not installed.

### `geo_search`
Finds the rows nearest to a lon/lat point by PostGIS distance. Parameters:
`schema`, `table`, `column` (strings), `longitude`, `latitude` (numbers),
`limit` (int, default 10). Each match is the row (excluding the geometry
column) plus its `distance`. Reports `available: false` if `postgis` is not
installed.

## Live operations (read)

### `list_active_queries`
Lists the queries currently running on the server, from `pg_stat_activity`.
Each entry carries the backend `pid`, `username`, `application`, `state`,
`wait_event` (`type:event` when waiting), `duration_seconds`, `query`, and
`blocked_by` — the PIDs holding locks it waits on. Idle connections,
PostgreSQL's background processes, and MCPg's own backend are excluded.

## Write (unrestricted mode only)

### `run_write`
Executes a single `INSERT`, `UPDATE`, or `DELETE` in a read-write transaction
committed on success. Multiple statements and non-DML are rejected. Add a
`RETURNING` clause to receive affected rows. Parameter: `sql` (string).

### `run_maintenance`
Runs `VACUUM` or `ANALYZE` against one table. Parameters: `operation`
(`vacuum`, `analyze`, or `vacuum_analyze`), `schema`, `table` (strings).
Requires `unrestricted` mode. The schema and table are quoted identifiers,
not parameters; both are escaped before reaching SQL.

### `cancel_query`
Cancels the query running on a backend PID (`pg_cancel_backend`); the
connection stays open. Parameter: `pid` (int). Returns `succeeded` —
`false` if no such backend exists. Requires `unrestricted` mode.

### `terminate_backend`
Terminates a backend PID (`pg_terminate_backend`), closing its connection.
Parameter: `pid` (int). Returns `succeeded` — `false` if no such backend
exists. Requires `unrestricted` mode.

### `run_ddl`
Executes a single DDL statement (`CREATE`/`ALTER`/`DROP` and related).
Requires `unrestricted` mode **and** `MCPG_ALLOW_DDL=true`. Parameter: `sql`
(string).

### `enable_extension`
Enables a known PostgreSQL extension (`CREATE EXTENSION IF NOT EXISTS`). Only
allowlisted extensions (`pg_trgm`, `vector`, `citext`, `postgis`, ...) may be
enabled. Requires `unrestricted` mode **and** `MCPG_ALLOW_DDL=true`.
Parameter: `name` (string).

## Data movement — read

### `export_query`
Runs a read-only SQL query through `run_select`'s safety checks and
serialises the rows to CSV or JSON. Truncates at `limit` (default 10 000)
with a `truncated` flag so callers can paginate via their own
`LIMIT`/`OFFSET`. Parameters: `sql`, `format` (`csv`/`json`), `limit`.

### `export_table`
Like `export_query` but takes `schema` + `table` directly. Names are
validated against the plain-identifier allowlist; anything that would
need delimited-identifier quoting is rejected.

## Data movement — write (`unrestricted` only)

### `import_csv`
Bulk-loads CSV `content` into `schema.table` via `COPY ... FROM STDIN`.
The text is sent verbatim; the caller is responsible for correctness
(matching column count, proper quoting). Parameters: `header` (skip the
first row), `delimiter` (single non-newline, non-quote character),
optional `columns` (explicit column list — each validated).

### `import_json`
Parses a JSON array of objects, derives the column list from the first
row (or an explicit `columns` arg), and runs a parametrised
`INSERT ... executemany`. Nested dict/list values are JSON-serialised
so they round-trip into `jsonb` columns; missing keys in later rows
bind as `NULL`.

## Data movement — subprocess (`unrestricted` + `MCPG_ALLOW_SHELL=true`)

These tools shell out to PostgreSQL binaries (`pg_dump`, `pg_restore`,
`psql`) through `mcpg.shell.run_pg_binary`, which enforces the
ADR-0004 policy: allowlisted binaries only, `asyncio.create_subprocess_exec`
(no shell), argv-only invocation, hard timeout, output cap with
truncation flag, libpq env-var credentials (never on argv).

### `dump_database`
Runs `pg_dump` against the configured database. `format='plain'`
returns SQL text; `'custom'` / `'tar'` return base64-encoded bytes.
`schema_only` toggles `--schema-only`. Result carries `exit_code`,
`output_bytes`, `output_truncated`, `timed_out`, and `stderr_tail`.

### `restore_database`
Pipes a dump into the configured database. `format='plain'` pipes the
SQL through `psql --single-transaction --set=ON_ERROR_STOP=on`;
`custom` / `tar` base64-decode `content` and pipe it through
`pg_restore --single-transaction --exit-on-error --no-owner
--no-privileges`. pg_restore is invoked with `--dbname=postgresql:///`
so libpq fills in the connection params from the `PG*` env vars —
credentials never appear on argv.

### `copy_table_between_databases`
Pipes `pg_dump --format=custom --table=schema.table` (source URL) into
`pg_restore --format=custom` (destination URL) with separate libpq
env dicts per leg. `include_schema` and `include_data` are required
(no defaults). A truncated dump raises before pg_restore runs; a
failed dump returns the dump stderr_tail with `restore_exit_code=-1`.

## LISTEN/NOTIFY bridge (`unrestricted` + `MCPG_ALLOW_LISTEN=true`)

Per ADR-0005, MCPg uses the tool-poll model — a dedicated PG
connection (separate from the request pool, opened lazily on first
subscribe) drains `notifies()` into per-subscription bounded queues.
Overflow drops oldest and surfaces `dropped_count` on the next poll's
first message. `MCPG_LISTEN_QUEUE_MAX` (default 1000) caps the queue.

### `subscribe_channel`
Opens `LISTEN "channel"` (idempotent per channel) and returns a
subscription id. Channel name must match `[A-Za-z_][A-Za-z0-9_]*`.

### `poll_notifications`
Drains up to `max_messages` (default 100) notifications from the
queue, waiting at most `timeout_ms` (default 0) for the first one
when the queue is empty. Each notification carries `channel`,
`payload`, `delivered_at`, `dropped_count`.

### `unsubscribe_channel`
Removes a subscription. `UNLISTEN` fires when the last subscriber on
the channel goes away. Returns `removed: true` if the subscription
existed.

### `list_notification_subscriptions`
Lists active `{subscription_id, channel}` pairs for visibility.

## Staged migrations (`unrestricted` + `MCPG_ALLOW_DDL=true`)

Per ADR-0006, MCPg uses a **same-database shadow schema** strategy.
`prepare_migration` clones the target schema's structure into
`mcpg_shadow_<id>` via introspection (no `pg_dump` shell-out), applies
the candidate SQL there, then runs `compare_schemas(target, shadow)`
so the agent reviews the structural diff before completion. State
lives in `mcpg_migrations.staged` (auto-created on first call).

### `prepare_migration`
Clones the target schema into a shadow, applies `candidate_sql` with
`SET LOCAL search_path` so unqualified identifiers resolve there, runs
`compare_schemas`, and persists the staged row. Returns the migration
id, shadow schema name, TTL, and structural diff. Candidate SQL that
needs to run outside a transaction (CREATE INDEX CONCURRENTLY, VACUUM,
ALTER SYSTEM) is refused with a clear error pointing at `run_ddl`.

### `complete_migration`
Applies the original candidate SQL to the target schema (same SET LOCAL
treatment) and drops the shadow. Refuses if the migration isn't in
`prepared` status or has expired.

### `cancel_migration`
Drops the shadow without applying. Idempotent — returns
`shadow_dropped=false` when the migration row doesn't exist.

### `list_pending_migrations`
Lists migrations in `prepared` status, newest first. Sweeps expired
entries (drops their shadows, flips status to `expired`) before
listing.

## Audit trail

### `list_audit_events`
Lists rows from `mcpg_audit.events` (newest first). Returns `[]` when
`MCPG_AUDIT_PERSIST` has never been turned on. Optional `tool` filter.

## ORM-DSL exporters

Every exporter is **read-only**. They share a v1 coverage boundary:
base tables, columns with PG-native types, primary keys, single-column
intra-schema foreign keys, and enum types. Cross-schema FKs and
composite FKs are documented v1 gaps for all of them. Views, foreign
tables, partitions, triggers, functions, and composite types are out
of scope.

### `generate_prisma_schema`
Emits a Prisma `.prisma` schema (mirrors `prisma db pull`). Default-
mapped types include `Int`, `BigInt`, `String`, `Boolean`, `Json`,
`DateTime`, `Decimal`, `Bytes`; unmapped types fall back to
`Unsupported("...")`.

### `generate_drizzle_schema`
Emits a Drizzle ORM TypeScript schema (`drizzle-orm/pg-core`).
`pgTable` consts with PG-native helpers (`integer`, `bigint`, `varchar`
with length, `timestamp` with `withTimezone: true`, `jsonb`, ...),
single-column FK chains via `.references(() => target.col)`, `pgEnum`
consts for enum types, `serial` / `bigserial` detected from `nextval`
defaults. The helper-import line is computed from what's actually
emitted — unused helpers don't clutter the output.

### `generate_sqlalchemy_models`
Emits a SQLAlchemy 2.0 declarative file (`DeclarativeBase` +
`Mapped[T]` + `mapped_column`). Core types from `sqlalchemy` and
PG-dialect types (`JSONB`) from `sqlalchemy.dialects.postgresql`.
Single-column FKs land inline via `ForeignKey("schema.table.col")`;
composite uniques go in `__table_args__` as `UniqueConstraint`. PG
enums become Python `enum.Enum` classes via the class-body form when
all labels are valid identifiers, or the functional
`enum.Enum("Name", {...})` form when any label needs sanitising
(hyphens, spaces, leading digits, keywords).

### `generate_sqlc_schema`
Emits a sqlc-friendly `schema.sql` — plain DDL ordered for clean
replay against an empty database: `CREATE SCHEMA` → `CREATE TYPE`
enums → `CREATE TABLE` (columns only) → `ALTER TABLE ADD CONSTRAINT`
(PK / unique / check / FK) → `CREATE INDEX`. In-process, no
`MCPG_ALLOW_SHELL` needed.

### `generate_diesel_schema`
Emits a Diesel ORM (Rust) `schema.rs` with one `table!` macro per
table (column → Diesel SQL type, `Nullable<T>` for nullable columns),
`joinable!` for single-column intra-schema FKs, and
`allow_tables_to_appear_in_same_query!` so multi-table joins
type-check. Enum types emit as `Text`-backed wrapper enums in a
`pg_enum` module — output works without `diesel_derive_enum`.

### `generate_jooq_config`
Emits a `jooq-codegen` `<configuration>` XML pointing at the live
database. Unlike the other exporters, jOOQ generates Java code itself
at build time — the artefact here is the config file the user feeds
to `mvn jooq-codegen:generate`. Explicit `<includes>` regex names
every base table; `<excludes>` covers MCPg's bookkeeping schemas;
`<forcedType>` entries map every `json` / `jsonb` column to
`org.jooq.JSON` / `org.jooq.JSONB`. Parameters: `target_package`,
`target_directory`.

### `generate_ent_schemas`
Emits Ent (Go) Schema struct files — returns `{filename: source}`.
Each file has the PascalCase struct, `Fields()` with `field.X(...)`
calls (`field.Int`, `field.Text`, `field.Bool`, `field.Time`,
`field.JSON`, `field.UUID`, `field.Enum`, ...), `Edges()` with
`edge.To(...)` for single-column FKs, and `field.Enum().Values(...)`
for enum-typed columns.

### `generate_ecto_schemas`
Emits Ecto (Elixir) schema modules — returns `{filename: source}`.
Files are named after the singularised table (`users` → `user.ex`,
per the Phoenix convention). Each module has `use Ecto.Schema`,
`@primary_key`, `field` declarations, `belongs_to` for single-column
FKs (stripping the `_id` suffix for the association name), and
`timestamps()` when both `inserted_at` and `updated_at` are present.
The Elixir top-level module is configurable via the `app_module`
arg (default `MyApp`).

## pg_cron writes (`unrestricted`)

### `pg_cron.schedule`
Schedules a SQL command via `cron.schedule(name, schedule, command)`.
Returns the job id. Requires the `pg_cron` extension enabled.

### `pg_cron.unschedule`
Removes a scheduled job by id or name.

### `pg_cron.update`
Updates a scheduled job's command or schedule.

## pg_partman writes (`unrestricted` + `MCPG_ALLOW_DDL=true`)

### `partman.create_parent`
Configures a table as a pg_partman-managed parent partition.

### `partman.run_maintenance`
Runs `partman.run_maintenance()` to create/retire partitions.

### `partman.drop_partition_time`
Drops time-based partitions older than the cutoff.

## Errors

Tools reject unsafe or invalid input before it reaches the database. Rejected
calls return an MCP error result; the message explains the cause (unsafe
statement, parse failure, non-positive `max_rows`, etc.). Every call —
success or failure — is recorded to the `mcpg.audit` logger. With
`MCPG_AUDIT_PERSIST=true`, every `run_write` / `run_ddl` is also written to
`mcpg_audit.events` with redacted arguments + result; query the table via
`list_audit_events`.
