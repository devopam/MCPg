# Vendored code

This directory contains third-party code vendored into MCPg. It is **not**
authored by the MCPg project and is excluded from the coverage gate and from
`mypy --strict`.

## `sql/` — PostgreSQL SQL-safety kernel

- **Source:** [`crystaldba/postgres-mcp`](https://github.com/crystaldba/postgres-mcp)
- **Pinned commit:** `07eb329c8c48e49640e0d1b5b35465d4d024c3ee` (2026-01-22)
- **Licence:** MIT — see [`LICENSE`](LICENSE) (Copyright (c) 2025, Crystal Corp.)
- **What it is:** the `pglast`-based SQL allowlist validator (`safe_sql.py`),
  async connection pool / driver (`sql_driver.py`), parameter binding
  (`bind_params.py`), and extension utilities. See ADR-0001 for why only this
  subpackage was vendored.

### Local modifications

The files are verbatim copies. The **only** changes are import-path rewrites
in the accompanying tests (`tests/vendor/`): `postgres_mcp.sql` →
`mcpg._vendor.sql`. The `sql/` source files themselves use relative imports
and were copied unchanged.

### Re-sync procedure

To pull upstream security fixes:

1. `git clone https://github.com/crystaldba/postgres-mcp /tmp/pg-mcp`
2. Diff `/tmp/pg-mcp/src/postgres_mcp/sql/` against `./sql/`.
3. Apply relevant changes; do not introduce new imports outside the subpackage.
4. Update the pinned commit above and note the change in `CHANGELOG.md`.
5. Run `tests/vendor/` to confirm behaviour is preserved.
