# ADR-0007: First-party SQL-safety kernel (de-vendor `crystaldba/postgres-mcp`)

- **Status:** accepted
- **Date:** 2026-07-08
- **Supersedes:** [ADR-0001](0001-build-approach.md) (the hard-fork / vendor decision)

## Context

[ADR-0001](0001-build-approach.md) chose to hard-fork MCPg from
`crystaldba/postgres-mcp` and **vendor** its `sql/` subpackage — the
`pglast`-AST SQL-safety allowlist validator plus the async connection
pool / driver — near-verbatim under `src/mcpg/_vendor/sql/` (MIT, pinned
commit `07eb329`). That was the right call to ship fast, but it left two
standing costs:

1. **~1.3k lines of security-critical code outside every quality gate.**
   The vendored kernel — including the SQL allowlist itself — was excluded
   from the coverage gate, `mypy --strict`, `ruff`, and `bandit`
   (`pyproject.toml` carve-outs).
2. **Manual re-syncs.** Upstream changes had to be re-applied by hand with
   local modifications tracked in a vendor README.

It was also the last third-party runtime *code* MCPg shipped. Roadmap 18.1
set out to own it.

## Decision

Replace the vendored kernel with a **first-party** `src/mcpg/sql/` package,
re-authored from the MIT-licensed original (attribution retained in
`NOTICE`). Recon showed three of the vendored modules (`bind_params.py`,
`extension_utils.py`, `index.py`) had no consumer outside `_vendor/`, so
they were **deleted, not rebuilt**. The kernel is re-architected to
**separate policy from mechanism**:

- `sql/allowlist.py` — the permitted statement / AST-node / function /
  extension sets, as **data** (the single auditable decision surface).
- `sql/safety.py` — `SafeSqlDriver`: the `pglast` parse + AST-walker +
  read-only execute path. Reads policy from `allowlist.py`; cannot widen it.
- `sql/driver.py` — `SqlDriver` / `DbConnPool` / `obfuscate_password`
  (pool + execution + credential redaction; no policy).

The public seam (`SqlDriver`, `SafeSqlDriver`, `DbConnPool`,
`obfuscate_password`) is unchanged, so the ~74 consuming modules only
changed an import path.

**Faithful re-author, not a redesign.** The security *behaviour* is pinned
identical to the vendored validator:

- A **differential parity harness** ran a safe/unsafe/malformed corpus
  through both validators — **0 divergence**.
- The 760-LOC adversarial suite (ported) passes 100% against `mcpg.sql`.
- A **fuzz pass** confirms the validator only ever returns a clean verdict
  (accept / `ValueError`) — never a crash / hang — on adversarial input.
- A dedicated security-review gate (allowlist audit, `/security-review`,
  threat model) passed — see
  [`../reviews/devendor-sql-kernel-security-review.md`](../reviews/devendor-sql-kernel-security-review.md).

## Consequences

- The SQL-safety kernel is now inside the coverage gate (90%),
  `mypy --strict`, `ruff`, and `bandit` — a net security improvement over
  the vendored state.
- `src/mcpg/_vendor/` and `tests/vendor/` are deleted; the five
  `pyproject.toml` `_vendor` carve-outs are removed; the module map and
  docs (`architecture.md`, `CLAUDE.md`, `SECURITY.md`, `NOTICE`) are
  first-party.
- MCPg ships **no vendored runtime code**. (`pglast`, `psycopg`, and
  `psycopg-pool` remain upstream *library* dependencies — not vendored
  source.)
- Attribution to `crystaldba/postgres-mcp` (MIT) is retained in `NOTICE`
  as the design lineage; the in-query `/* crystaldba */` marker is kept
  for now (a cosmetic rename is a possible follow-up).

## Alternatives considered

- **Keep vendoring.** Rejected — perpetuates the gate carve-outs and the
  manual re-sync burden; ADR-0001's "ship fast" rationale no longer applies.
- **Redesign the validator (stricter/simpler allowlist).** Deferred — a
  behaviour change would need its own threat model + review. This ADR is a
  faithful re-author; any policy tightening is a separate, reviewed change
  (see the pre-existing observations in the security-review note).
