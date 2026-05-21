# ADR-0003: Configurable connection-pool sizing

- **Status:** accepted
- **Date:** 2026-05-20

## Context

Phase 6 (scalability) needs the connection pool's `min_size`/`max_size` to be
configurable. The vendored `DbConnPool` (`src/mcpg/_vendor/sql/sql_driver.py`)
hardcodes `min_size=1, max_size=5` as literals inside `pool_connect`, with no
hook to override them.

## Options considered

1. **Subclass `DbConnPool` and override `pool_connect`.** No vendored edit, and
   `SqlDriver`'s `isinstance(conn, DbConnPool)` check still passes. But
   `pool_connect` is ~40 lines of retry/validation/health-probe logic; the
   override would have to duplicate all of it to change two literals — a far
   larger and more drift-prone copy than the change warrants.
2. **Manage our own `psycopg_pool` pool in `Database`.** `SqlDriver` only
   recognises a `DbConnPool` or a raw connection as its `conn`, so a
   self-managed pool would not be accepted — rejected.
3. **Patch the vendored `DbConnPool`.** Add `min_size`/`max_size` parameters to
   `__init__` (defaulting to `1`/`5`) and use them in `pool_connect`. A
   ~3-line, behaviour-preserving change.

## Decision

**Patch the vendored `DbConnPool` (Option 3).** ADR-0001 explicitly anticipated
modifying vendored code "under TDD" with behaviour pinned by tests. The change
is minimal and behaviour-preserving (defaults reproduce the old literals), and
the vendored `tests/vendor/sql/test_sql_driver.py` suite — already part of our
test run — pins `DbConnPool` behaviour and acts as the characterisation net.

Subclassing was rejected because duplicating 40 lines of non-trivial pool
logic is worse for maintenance than a localised 3-line patch.

## Consequences

- `src/mcpg/_vendor/sql/sql_driver.py` is no longer a verbatim copy; the local
  modification is recorded in `src/mcpg/_vendor/README.md`. On re-sync the
  3-line patch is trivially re-applied.
- New settings `MCPG_POOL_MIN_SIZE` / `MCPG_POOL_MAX_SIZE` flow through
  `Settings` into `Database` → `DbConnPool`.
- Future vendored-code changes follow the same recorded-modification practice.
