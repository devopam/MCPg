# ALTER SYSTEM tools

MCP tools for persistent PostgreSQL configuration changes (not HTTP API
endpoints, and **not** part of `run_ddl`).

| Tool | Gate | Notes |
|------|------|-------|
| `inspect_guc` | READ (always) | Live `pg_settings` metadata, context, `pending_restart` |
| `alter_system` | unrestricted + `MCPG_ALLOW_ALTER_SYSTEM` | SET or RESET one GUC; requires `confirm=true` + token |
| `alter_system_reset_all` | same | `ALTER SYSTEM RESET ALL`; requires `confirm=true` + token |
| `reload_config` | same | `pg_reload_conf()` |

## Why not `run_ddl`?

`ALTER SYSTEM` writes `postgresql.auto.conf`, cannot run inside a transaction
block, and is instance-scoped. `run_ddl` uses a transactional allowlist of
structural DDL. Putting `AlterSystemStmt` on that list would still fail at
execution time.

Writes use `Database.run_unmanaged` (autocommit), the same path as `VACUUM`.

## Agent / human confirmation flow

1. Optional: `inspect_guc(name)` — see context and whether reload or restart is needed.
2. `alter_system(..., dry_run=true)` — plan only.
3. `alter_system(...)` without `confirm` → `status=confirmation_required`, a
   **warning** string and a `confirmation_token`. The agent should show the
   warning to the user.
4. User agrees → call again with `confirm=true` **and** that `confirmation_token`
   to apply. The token is derived from the exact statement (for `RESET ALL`,
   from the current override set), so a changed value or a changed set of
   overrides asks for confirmation again. It is stateless: it proves the
   confirmation refers to this action, not that a human saw it.
5. If `reload=none` was used, call `reload_config` when appropriate.

Notes:

- Values are checked against `pg_settings` metadata (boolean, enum, numeric
  range) before the plan is returned; PostgreSQL still has the final say.
- GUC names are case-insensitive and lowercased.
- `inspect_guc` redacts values of command, connection-string and password-like
  settings (`archive_command`, `primary_conninfo`, ...). With replica routing
  configured it may be served by a replica; the write tools always read the primary.
- `RESET ALL` enforces the allow/deny policy against every name currently
  overridden in `postgresql.auto.conf` (read from `pg_file_settings`); if that
  cannot be read it refuses.
- Reload success reflects the boolean `pg_reload_conf()` returns.
- The write itself is bounded by `MCPG_STATEMENT_TIMEOUT_MS`.
- A retried `RESET ALL` whose first attempt already committed will clear
  overrides written by others in between; the token only protects against a
  changed override set at confirmation time.

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `MCPG_ALLOW_ALTER_SYSTEM` | `false` | Register write tools (`alter_system`, `alter_system_reset_all`, `reload_config`) |
| `MCPG_ALTER_SYSTEM_ALLOWLIST` | empty | If set, only these GUC names may be changed |
| `MCPG_ALTER_SYSTEM_DENYLIST` | empty | Extra names blocked on top of the built-in denylist |

Built-in denylist includes connectivity and preload foot-guns
(`listen_addresses`, `port`, `ssl`, `shared_preload_libraries`, …) and settings that
run OS commands or load libraries (`archive_command`, `restore_command`, `jit_provider`, …).

## Privilege

The connection in `MCPG_DATABASE_URL` must be allowed to run `ALTER SYSTEM`
(typically superuser). There is no separate superuser credential. Managed
hosts often forbid `ALTER SYSTEM` entirely.

The write tools run on the pool connection as the login user, so they refuse
to run when `MCPG_DEFAULT_ROLE` / `MCPG_ALLOWED_ROLES` (tenancy) is configured.

## See also

- `src/mcpg/guc.py` — core logic
- `src/mcpg/guc_tools.py` — MCP registration
- `tests/unit/test_guc.py`
- `docs/tools.md` — capability gates and tool index
- `docs/security.md` — capability model
