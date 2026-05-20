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

### Reconciling with the TDD mandate

A hard-fork inherits ~7k LOC that cannot be retroactively TDD'd. Strategy:

- **Inherited kernel** (`sql/safe_sql.py`, `explain/`, `database_health/`,
  `index/`, `top_queries/`) is treated as a **trusted vendored foundation**. It
  keeps its existing test suite. Before modifying any inherited module we add
  **characterization tests** to pin current behavior, then change under TDD.
- **All newly authored code** — server bootstrap, access-mode policy engine,
  tool registry/layer, config/settings, taxonomy, audit log — is **strict TDD**
  (failing test first).
- The CI coverage gate applies to new/changed code.

This keeps TDD honest for everything we author without discarding proven code.

## Consequences

- **Easier:** immediate access to a working server, real SQL-safety validator,
  and ops/tuning features; Phases 5+ become integration/refactor rather than
  greenfield.
- **Harder:** must absorb and understand inherited code; must refactor out
  global state early (new Phase 1 task); must maintain attribution and a
  `NOTICE`/`THIRD_PARTY` record; characterization-test burden when touching the
  kernel.
- **Follow-up:** Phase 0 now includes importing the upstream source onto our
  branch with preserved license attribution; Phase 1 adds an explicit
  "eliminate global state" task; Phase 3 adds the third access mode.
- We will track upstream `crystaldba/postgres-mcp` for security fixes to
  cherry-pick.
