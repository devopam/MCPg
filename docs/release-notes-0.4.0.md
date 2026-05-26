# MCPg v0.4.0 — release notes

**Released:** 2026-05-26
**Tool surface:** 45 → **74**  (+29)
**Tests:** 723 pass / 9 skipped, **96% coverage**
**CI:** PG 14 / 15 / 16 / 17 / 18

This release closes the **post-0.3.0 roadmap (Batches D / E / F / G)**.
Batches A–G are now fully shipped.

## Headlines

### Batch D — data movement (5 tools)

The data-movement family lets an agent pull data out, push it in,
and copy it between databases without leaving the MCP surface.

| Tool | What it does |
| --- | --- |
| `dump_database` | Runs `pg_dump` against the configured database via the ADR-0004 subprocess gate. Returns SQL text (plain) or base64-encoded bytes (custom/tar). |
| `restore_database` | Pipes SQL through `psql --single-transaction --set=ON_ERROR_STOP=on`, or base64-encoded archives through `pg_restore --single-transaction --exit-on-error`. |
| `copy_table_between_databases` | Pipes `pg_dump --format=custom --table=schema.table` (source URL) into `pg_restore` (destination URL) with separate libpq envs per leg. Refuses to restore a truncated dump. |
| `import_csv` | Bulk-loads CSV via in-process `COPY ... FROM STDIN`. No subprocess gate needed. |
| `import_json` | Parses a JSON array of objects, derives columns from the first row, runs parametrised `INSERT ... executemany`. |

**Credentials never reach argv** — every subprocess invocation routes
credentials through the libpq `PG*` env vars and the result includes
a redacted env dict for audit logging.

### Batch E — LISTEN/NOTIFY bridge (4 tools)

Per **ADR-0005**, MCPg picks the tool-poll model over server-sent
notifications. A new `mcpg.listen` module owns server-lifetime
subscription state on a dedicated PG connection separate from the
request pool. Four tools:

- `subscribe_channel(channel)` — opens `LISTEN "channel"` (idempotent
  per channel) and returns a subscription id.
- `poll_notifications(subscription_id, timeout_ms, max_messages)` —
  drains a bounded queue with overflow drop-oldest semantics.
- `unsubscribe_channel(subscription_id)` — removes the subscription
  and `UNLISTEN`s when the last subscriber on the channel goes away.
- `list_notification_subscriptions()` — reports active subs.

New `Capability.LISTEN` + `MCPG_ALLOW_LISTEN` opt-in, plus a
`MCPG_LISTEN_QUEUE_MAX` knob (default 1000).

### Batch F — staged-migration workflow (4 tools)

Per **ADR-0006**, MCPg uses a **same-database shadow-schema**
strategy (no full-DB clone — a 500 GB database does not pay the
`CREATE DATABASE TEMPLATE` cost per staged migration). Four tools:

- `prepare_migration(name, target_schema, candidate_sql, ttl_minutes)`
  clones the target schema's structure into `mcpg_shadow_<id>` via
  introspection (tables, columns, PK / UNIQUE / CHECK / FK constraints,
  indexes), applies `candidate_sql` against the shadow with
  `SET LOCAL search_path`, runs `compare_schemas(target, shadow)`,
  and persists the staged row in `mcpg_migrations.staged`.
- `complete_migration(id)` applies the original candidate SQL to the
  target schema and drops the shadow.
- `cancel_migration(id)` drops the shadow without applying.
- `list_pending_migrations()` lists prepared migrations, sweeping
  expired entries before returning.

New `Capability.MIGRATE` reuses the existing `MCPG_ALLOW_DDL`
opt-in (the underlying ops are DDL).

### Batch G — ORM-DSL exporters (3 new tools, +1 existing)

Generate a ready-to-use schema/model file for any of four
ecosystems from a live PG catalog:

| Tool | Output |
| --- | --- |
| `generate_prisma_schema` | Prisma `.prisma` (TS/JS). *Shipped in v0.3.0.* |
| `generate_drizzle_schema` | Drizzle ORM TypeScript (`drizzle-orm/pg-core`). |
| `generate_sqlalchemy_models` | SQLAlchemy 2.0 declarative (`DeclarativeBase` + `Mapped[T]` + `mapped_column`, jsonb via the PG dialect, enum types as Python `enum.Enum`). |
| `generate_sqlc_schema` | Replayable plain DDL ordered PK → FK so sqlc can compile it against an empty database. |

All four exporters are **read-only**.

## What's new under the hood

- **`mcpg.shell`** — subprocess execution policy (ADR-0004).
  Allowlist (`pg_dump`/`pg_restore`/`psql`), argv-only invocation,
  hard timeout, output cap with truncation flag, libpq-env credentials
  redacted in the result for audit.
- **`mcpg.listen`** — server-lifetime subscription state + background
  reader task; iterating `notifies()` with `timeout=0.5` so the
  psycopg connection lock releases periodically for concurrent
  `UNLISTEN execute()` calls.
- **`mcpg.migrations`** — introspection-driven DDL replay into a
  shadow schema (no `pg_dump` shell-out, no Batch-D dependency).
- **`Database.copy_from_stdin`** / **`Database.execute_many`** —
  raw connection access through the pool for in-process bulk loads.
- **Capability surface grew** to include `SHELL`, `LISTEN`, `MIGRATE`
  alongside the existing `READ` / `WRITE` / `DDL`.
- **PG 18** is now in the CI matrix.

## Notable fixes

The 10 fixes from PR #17's code review:

1. `restore_database` (custom/tar) now passes
   `--dbname=postgresql:///` so pg_restore connects via `PG*` env
   instead of falling into "convert to SQL script" mode.
2. `ListenManager` recovers from a dead listener connection — the
   reader-loop clears `_conn` and sets `_needs_resubscribe`, the
   next subscribe opens a fresh conn and re-issues `LISTEN` for
   every active channel.
3. Migration DDL replay only rewrites schema references on
   `foreign_key` constraints, not on every constraint — a CHECK
   constraint with a string literal like `'public.%'` is no longer
   corrupted.
4. `mcpg.sqlc` enum labels are apostrophe-escaped (PG `''` doubling).
5. `mcpg.sqlalchemy_export` falls back to the functional
   `enum.Enum("Name", {...})` form when any label isn't a valid
   Python identifier — generated files import cleanly even with
   labels like `in-progress`, `1st`, or `class`.
6. `mcpg.drizzle` default rendering applies PG-→-JS escape
   translation in the right order — `'it''s'` → `"it's"`, `'a\nb'`
   no longer becomes a TS newline.
7. Shadow schema names are capped to fit PG's 63-byte
   `NAMEDATALEN` limit.
8. The migration workflow refuses non-transactional candidate SQL
   (CREATE INDEX CONCURRENTLY, VACUUM, ALTER SYSTEM) with a clear
   error pointing at `run_ddl`.
9. `mcpg.shell._write_stdin` always closes the child's stdin in a
   `finally` block.
10. `ListenManager.close()` bounds `conn.close()` at 2s so a libpq
    hang can't wedge server shutdown.

## Upgrade notes

- **New env vars** added — defaults are all safe; you only need to
  set them if you want the new features:
  - `MCPG_ALLOW_SHELL` (default `false`) — gates `dump_database` /
    `restore_database` / `copy_table_between_databases`.
  - `MCPG_SHELL_TIMEOUT_SEC` (default 60).
  - `MCPG_SHELL_MAX_OUTPUT_BYTES` (default 64 MiB).
  - `MCPG_ALLOW_LISTEN` (default `false`) — gates the LISTEN
    bridge.
  - `MCPG_LISTEN_QUEUE_MAX` (default 1000).
- **New capability gates** —
  - `dump_database` / `restore_database` /
    `copy_table_between_databases` require unrestricted mode +
    `MCPG_ALLOW_SHELL`.
  - LISTEN tools require unrestricted mode + `MCPG_ALLOW_LISTEN`.
  - Migration tools require unrestricted mode + `MCPG_ALLOW_DDL`
    (the existing DDL opt-in is reused — no new env var).
  - `import_csv` / `import_json` require unrestricted mode (WRITE
    capability) — no new env var; they're in-process.
- **Database fixture** — if your codebase mocks `Database` directly,
  add the new `copy_from_stdin` and `execute_many` methods to your
  fake (see `tests/unit/_fakes.py:FakeDatabase`).
- **AppContext** grew a `listen_manager` field — tests constructing
  `AppContext` directly need to pass a `ListenManager` instance.
  `create_server` accepts an optional `listen_manager` kwarg for
  injection in tests.

## What's next

- Additional schema → DSL exporters (Diesel / Ent / jOOQ / Ecto)
  under the same Batch G umbrella.
- Documentation pass: tool tour, integration examples.
- New feature work — open direction now that A–G is closed.
