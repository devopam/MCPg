# ADR-0001: Build approach — adopt `crystaldba/postgres-mcp` as a hard-forked base

- **Status:** accepted
- **Date:** 2026-05-20

## Context

The official `@modelcontextprotocol/server-postgres` is deprecated and archived
(July 2025) after a SQL-injection CVE. Phase 0 evaluated the strongest active
alternative, `crystaldba/postgres-mcp` ("Postgres MCP Pro"), hands-on to decide
between: (a) fork & contribute upstream, (b) hard-fork into our own product, or
(c) greenfield.

### Evaluation findings (`crystaldba/postgres-mcp` @ commit `07eb329`, Jan 2026)

**Strengths**

- **License: MIT** (Crystal Corp, 2025) — permissive, fork-friendly, attribution
  preservable.
- **Stack matches our recommendation exactly** — Python 3.12, official `mcp` SDK,
  `psycopg` 3, `psycopg-pool`, `pglast` 7.11.
- **Security kernel is real work** — `sql/safe_sql.py` is a ~1,000-line `pglast`
  AST allowlist validator. This is precisely the defense the archived official
  server lacked. Rewriting it from scratch would be reckless.
- **Strong test discipline** — ~6.4k test LOC vs ~7.3k src LOC; unit +
  integration tests run against **real Postgres** (Docker, PG 12/15/16).
- **Modular** — clean package split: `sql/`, `database_health/`, `explain/`,
  `index/`, `top_queries/`.
- **Transports** — stdio, SSE, and streamable HTTP already implemented.
- Mature analysis features (health checks, index tuning, EXPLAIN) we would
  otherwise spend Phases 5+ rebuilding.

**Weaknesses / debt**

- **Global mutable state** — `current_access_mode` and `db_connection` are module
  globals in `server.py`. Hurts testability, multi-instance use, and a
  tool-registry-gated-by-access-mode design.
- **Only 2 access modes** (`unrestricted`, `restricted`) — no distinct
  `read-only`; our broad-scope plan wants three.
- **Modernization debt** — `ruff` target still `py39`; tracked upstream as
  issue #129. Uses `pyright` (not `mypy`).
- **Maintenance cadence** — single dominant author; latest commit ~4 months
  before this evaluation; sizeable open-PR backlog. Upstream review would be a
  bottleneck for an external contributor.
- Carries an `instructor` LLM dependency we may not want in the core.

## Options considered

1. **Fork & contribute upstream** — fastest in theory, but our broad-scope
   vision (3 access modes, eliminate global state, multi-tenancy, our own tool
   taxonomy) diverges architecturally. With a stale cadence and PR backlog, our
   roadmap and resume-friendly cadence would be hostage to upstream review.
2. **Hard-fork as our own product (MCPg)** — adopt the base under MIT (keeping
   attribution), restructure aggressively, own the roadmap. Keeps the
   battle-tested security kernel; we still cherry-pick upstream fixes and may
   upstream security patches as goodwill.
3. **Greenfield** — cleanest TDD story, but discards ~1k lines of hard-won SQL
   safety code and months of analysis features. Unjustified risk and delay.

## Decision

**Adopt `crystaldba/postgres-mcp` as a hard-forked base (Option 2).**

The security kernel (`safe_sql.py`) and analysis modules are too valuable to
rewrite, and their permissive MIT license makes reuse clean. Hard-forking —
rather than contributing upstream — is chosen because our architecture diverges
and upstream cadence would gate our progress.

### Vendoring scope — minimise inherited surface

To avoid inheriting tech debt we did **not** take the whole ~7k LOC base.
Import-graph analysis confirmed the `sql/` subpackage is **fully
self-contained** — it imports only `pglast`, `psycopg`, `psycopg_pool`,
`typing_extensions`, the stdlib, and its own siblings. It has **no dependency**
on the upstream `server.py` (and its global state), the analysis modules
(`database_health/`, `explain/`, `index/`, `top_queries/`), or the heavy
`instructor` LLM dependency.

Therefore we vendor **only** `sql/` (6 files, ~2,453 LOC) into
`src/mcpg/_vendor/sql/`, pinned to upstream commit `07eb329`:

| File | Role |
|------|------|
| `safe_sql.py`       | `pglast` AST allowlist SQL validator (the security kernel) |
| `sql_driver.py`     | `DbConnPool`, `SqlDriver`, `obfuscate_password` |
| `bind_params.py`    | parameter binding / AST visitors |
| `extension_utils.py`| extension + PG-version checks |
| `index.py`          | `IndexDefinition` value type |
| `__init__.py`       | public exports |

Everything else — server bootstrap, access-mode engine, tool layer, config,
taxonomy, and the analysis features (health/explain/index) — is **authored
fresh by us under strict TDD**. Inherited surface drops from ~7.3k to ~2.5k LOC.

### Reconciling with the TDD mandate

- **Vendored kernel** (`src/mcpg/_vendor/`) is a **trusted, pinned foundation**.
  It keeps the subset of upstream tests that port without touching `server.py`
  (`test_safe_sql`, `test_obfuscate_password`, `test_sql_driver`). It is
  excluded from the coverage gate. Before modifying any vendored file we add
  **characterization tests** to pin behaviour, then change under TDD.
- **All newly authored code** is **strict TDD** (failing test first).
- The CI coverage gate applies to authored (non-vendored) code.

This keeps TDD honest for everything we author without discarding the
hard-won, security-critical SQL validator.

## Consequences

- **Easier:** immediate access to a battle-tested SQL-safety validator and
  connection layer without owning the upstream global state or LLM dependency.
- **Harder:** the analysis features (health/explain/index) are now authored by
  us under TDD rather than inherited — more work in Phases 5+, but clean and
  fully tested.
- **Licensing:** the MCPg repo is **AGPL-3.0**; the vendored `sql/` code is
  **MIT** (permissive, AGPL-compatible). We preserve the upstream MIT licence
  text and copyright in `src/mcpg/_vendor/LICENSE` and record provenance in
  `NOTICE` and `src/mcpg/_vendor/README.md`.
- **Follow-up:** track upstream `crystaldba/postgres-mcp` for security fixes to
  the `sql/` subpackage and cherry-pick into the vendored copy (re-sync
  procedure documented in `src/mcpg/_vendor/README.md`).
