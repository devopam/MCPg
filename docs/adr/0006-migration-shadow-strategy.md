# ADR-0006: Migration shadow-workflow strategy

- **Status:** accepted
- **Date:** 2026-05-24

## Context

Batch F (Phase 27) wants `prepare_migration(name, sql)` /
`complete_migration(id)` tools so an agent can stage a migration,
review the resulting structural diff (using Phase 18's
`compare_schemas`), and commit it — Neon-style "branch the database,
test the migration, merge". Phase 18 supplies the diff machinery; the
missing piece is "what gets diffed against what?"

PostgreSQL has no native copy-on-write database branching. Whatever
"shadow" mechanism Phase 27 uses has to be implemented on stock PG,
without external orchestration. Three approaches are viable.

## Options considered

1. **Same-database shadow schema.** `CREATE SCHEMA shadow_<id>`, clone
   the target schema's DDL into it via `pg_dump --schema-only -n
   <target> | sed 's/<target>/shadow_<id>/' | psql`, run the candidate
   migration against the shadow, diff the shadow against the original
   using `compare_schemas`, drop the shadow on commit-or-cancel.
   Cheap (no second database), fast (DDL-only clone), structural-only
   (no data, no triggers firing, no FK validations across schemas).
2. **Side-channel database via `TEMPLATE`.** `CREATE DATABASE shadow_<id>
   TEMPLATE <target>` — a full data clone (PG's copy-on-write at the
   storage level uses filesystem links so it's surprisingly fast for
   small DBs), apply the migration there, diff, drop. Full fidelity
   (data, indexes, constraints, everything). Heavy for production-sized
   databases (`CREATE DATABASE TEMPLATE` blocks the source from
   accepting new connections briefly and disk usage doubles
   temporarily).
3. **External orchestration via `pg_dump --schema-only` to a separate
   ephemeral container.** Cleanest isolation; requires container
   infrastructure MCPg doesn't currently ship and pushes the
   responsibility outside the server. Rejected on the same grounds as
   ADR-0004 Option 4.

## Decision

**Option 1 — same-database shadow schema.** Reasoning:

- Most migrations are structural — column additions, index creates,
  constraint adds. Structural diff is exactly what `compare_schemas`
  produces. Data-touching migrations (backfills) are out of scope for
  the "agent reviews the diff before commit" workflow; they want a
  different tool.
- "Heavy for production-sized databases" is a real concern for Option
  2 that we avoid entirely. A Phase-27 user with a 500 GB database
  cannot afford a `CREATE DATABASE TEMPLATE` clone for every staged
  migration.
- Same-DB shadows compose well with the existing audit trail
  (Phase 21): the `prepare_migration` and `complete_migration` tools
  appear as audited DDL operations on a `mcpg_migrations.staged` table
  that tracks state, no new infrastructure.

Implementation outline:

- A new `mcpg.migrations` module owns the state. Migrations are tracked
  in `mcpg_migrations.staged` with columns: `id text PK` (caller-
  supplied name + a timestamp suffix), `prepared_at timestamptz`,
  `target_schema text`, `shadow_schema text`, `candidate_sql text`,
  `status text` (`prepared`, `completed`, `cancelled`), `ttl_expires_at
  timestamptz`.
- `prepare_migration(name, target_schema, candidate_sql, ttl_minutes=60)`:
  1. Create `shadow_<name>_<ts>` schema.
  2. Replay the target schema's DDL into the shadow via a Python loop
     over `list_tables` / `describe_table` / `list_constraints` etc.
     (we already have all the introspection — no `pg_dump` shell-out
     needed; this avoids the Batch-D subprocess gate entirely).
  3. Apply `candidate_sql` inside a SAVEPOINT scoped to the shadow.
  4. Run `compare_schemas(target, shadow)` and return the diff.
  5. Insert the row into `mcpg_migrations.staged`. Audit-trail
     records the call when `MCPG_AUDIT_PERSIST` is on.
- `complete_migration(id)`:
  1. Look up the row; refuse if status != `prepared` or TTL expired.
  2. Inside a real transaction, apply the original `candidate_sql`
     against the target schema. The transaction commits or rolls back
     as a unit.
  3. Drop the shadow schema, set `status='completed'`.
- `cancel_migration(id)`:
  1. Drop the shadow schema, set `status='cancelled'`.
- `list_pending_migrations()` reads the table.
- A background task (or lazy lookup in `prepare_migration`) sweeps
  expired entries: any row past TTL whose status is still `prepared`
  has its shadow dropped and status set to `cancelled`.

All five tools gate under a new `Capability.MIGRATE` enum entry that
requires unrestricted mode + the existing `MCPG_ALLOW_DDL` opt-in (the
underlying ops are DDL).

## Consequences

What becomes easier:

- Agent-driven structural migrations get a "review the diff" UX
  without any new infrastructure. The diff IS the review surface.
- Failures cascade cleanly: a botched candidate SQL fails the
  SAVEPOINT and the shadow stays intact for inspection (or cancel).
- Audit trail integration is free — `complete_migration` is a DDL
  call, audited like any other.

What becomes harder:

- Same-DB shadows mean the original and shadow share oid space, role
  grants, and `current_database()`. DDL that references
  `current_database()` (rare but possible) won't see the right value
  in the shadow. Documented as a known limitation; an agent that
  needs full-fidelity isolation should use a real second database
  outside MCPg.
- DDL replay is not 100% perfect. Trigger functions defined elsewhere,
  cross-schema FKs, custom types from other schemas — these aren't
  cloned into the shadow. The Phase-18 diff will surface them as
  "removed in shadow", which is a noisy but correct signal. Tests
  must cover the common cases (PK, FK within the schema, CHECK
  constraints) and document the gaps.
- Long-prepared migrations accumulate shadow schemas; the TTL sweeper
  must work reliably. Pin the TTL behaviour with both unit tests
  (`prepare_migration` past TTL is cancellable) and an integration
  test that fast-forwards time.

Follow-ups:

- Decide the DDL-replay strategy in detail before Phase 27 starts:
  introspection-driven (no subprocess; uses Batch-A introspection),
  vs `pg_dump --schema-only` (better fidelity, requires the Batch-D
  subprocess gate to be live first). Lean toward introspection-driven
  for v1 since it avoids the cross-batch dependency.
- Sketch the `mcpg_migrations.staged` table schema and verify it
  doesn't collide with `mcpg_audit.events` (different schema,
  different intent — no conflict).
- Confirm `SAVEPOINT`-scoped DDL behaviour across PG 14-18; some DDL
  is auto-committed (CONCURRENTLY variants) and can't run inside a
  savepoint.
