# Security-review sign-off — first-party SQL-safety kernel (roadmap 18.1)

Records the mandatory security-review gate for the de-vendored SQL kernel
(`docs/plans/devendor-sql-kernel.md`), run on PR 1 (`mcpg.sql`) before the
PR 2 consumer swing. **Outcome: PASS.**

## What was reviewed

The first-party `src/mcpg/sql/` package that replaces the vendored
`crystaldba/postgres-mcp` SQL-safety kernel — `driver.py` (pool + driver +
`obfuscate_password`), `allowlist.py` (policy as data), `safety.py`
(`SafeSqlDriver` validator). A "faithful re-author": the security
*behaviour* is intended to be identical to the vendored code; only the
structure, typing, and gate coverage change.

## Gate results

| # | Check | Result |
|---|---|---|
| 1 | **Differential parity** — a safe/unsafe/malformed corpus through *both* the vendored and first-party validators, asserting an identical accept/reject verdict + error type (`tests/unit/test_sql_kernel_differential.py`). | ✅ **50 cases, 0 divergence.** |
| 2 | **Allowlist audit** — statement / node / function / extension policy vs the threat model (below). | ✅ See findings. |
| 3 | **Fuzz / robustness** — 41 malformed / oversized / adversarial inputs must yield a clean verdict (accept or `ValueError`), never a crash / other exception / hang (`tests/unit/test_sql_kernel_fuzz.py`). | ✅ **41/41.** |
| 4 | **`/security-review`** over the full PR 1 diff. | ✅ **No HIGH/MEDIUM findings.** |
| 5 | **Threat-model note** (this document). | ✅ |

Plus the ported adversarial suite (`test_sql_kernel_safety.py`, 60 cases)
passes 100% against `mcpg.sql`.

## Allowlist audit findings

- **Statement allowlist** is 10 read-only types (`SelectStmt`, `ExplainStmt`,
  `VariableShowStmt`, `VacuumStmt`, cursor + prepared-statement management,
  gated `CreateExtensionStmt`). **No** `INSERT`/`UPDATE`/`DELETE`, DDL,
  `COPY`, `SET`, or `DO`. Multi-statement input is walked per statement, so
  stacking (`SELECT 1; DROP …`) is rejected. Default-deny: an unknown AST
  node type is rejected outright.
- **Function allowlist** (494 entries) contains **no** file-read
  (`pg_read_file`, `pg_ls_dir`), large-object (`lo_import`/`lo_export`),
  network (`dblink`), backend-control (`pg_terminate_backend`,
  `pg_cancel_backend`, `pg_reload_conf`), config (`set_config`), or sleep
  primitives. A function not on the list is rejected even if its extension
  is installed.

## Threat model

**Defends against:** an agent-supplied (or prompt-injected) query that tries
to do anything beyond a read-only `SELECT`/introspection — writes, DDL/DCL,
statement stacking, calling a non-allowlisted function, `SELECT … FOR
UPDATE`, `EXPLAIN ANALYZE` (which executes), or a `CREATE EXTENSION` for a
non-allowlisted extension. Rejection happens at parse+walk time, before the
statement reaches the database; execution additionally runs in a
`READ ONLY` transaction.

**Explicit non-goals (defence-in-depth, not a substitute for):**
- **Database-side least privilege.** The allowlist is a second line of
  defence; the connecting role should still be least-privilege (see
  `docs/installation.md`).
- **The connection role's own grants.** The validator restricts *statement
  shape*, not *object access* — RLS / GRANTs remain the DB's job.

## Pre-existing observations (unchanged; noted for future hardening)

These are properties inherited verbatim from the vendored kernel (the
differential test confirms they are not newly introduced), out of scope for
a faithful re-author but worth a future look:

- **`ALLOWED_EXTENSIONS` is broad** (includes `dblink`, `file_fdw`,
  `plpython3u`, etc.). `CreateExtensionStmt` is only reachable in
  `unrestricted` mode **and** with `MCPG_ALLOW_DDL`, and creating an
  extension does *not* add its functions to the function allowlist — so the
  blast radius is bounded — but the list could be tightened in a later,
  behaviour-changing PR (with its own review).

## Sign-off

The re-author does not move the SQL-safety boundary (proven by the
differential parity harness) and introduces no new vulnerability
(`/security-review`). The kernel is now inside `mypy --strict` / `ruff` /
`bandit` / coverage, from which the vendored original was excluded — a net
improvement. **Cleared to proceed to PR 2 (the consumer swing).**
