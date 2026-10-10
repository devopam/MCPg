# MCPg — threat model for OSS Scanner

Guidance for Anthropic's OSS Scanner (https://github.com/anthropics/oss-scanner).
MCPg is a pure-Python PostgreSQL **Model Context Protocol** server: an AI
agent drives it over stdio or HTTP to inspect, query, and operate a Postgres
database. The security-relevant surface is **application security**
(injection, authorization, SSRF, credential handling) — there is no
memory-safety / native surface.

## What this project does and where untrusted input enters

Everything an agent sends is **untrusted** (it may be prompt-injected by data
the agent read elsewhere). The untrusted inputs that matter:

- **Arbitrary SQL strings** passed to query tools (`run_select`,
  `run_analytical_query`, `explain_query`, `run_select_parallel`, …). These
  flow into the SQL-safety kernel (`src/mcpg/sql/`) — a `pglast` parse + AST
  allowlist that must reject anything beyond read-only `SELECT`/introspection
  **before** it reaches the database, with execution additionally wrapped in a
  `READ ONLY` transaction.
- **Tool arguments that become SQL identifiers** (schema/table/column names).
  These must be safely quoted (`quote_identifier` in
  `src/mcpg/identifiers.py`) or constrained — never string-concatenated into
  SQL.
- **Natural-language prompts to NL→SQL** (`src/mcpg/nl2sql.py`): sent to an
  operator-configured provider; the returned SQL is then executed through the
  same safety kernel.
- **HTTP transport credentials & headers**: bearer / OIDC JWTs
  (`src/mcpg/oidc.py`) and the per-request `X-MCPG-Role` tenancy header
  (`SET LOCAL ROLE`).
- **Shell-tool arguments** (dump/restore/`copy_table_between_databases` in
  `src/mcpg/shell.py`) — only when `MCPG_ALLOW_SHELL` is set in unrestricted
  mode.
- **Configuration** via `MCPG_*` env vars (DSNs, replica/JWKS URLs).

Trust boundary: **agent (untrusted) → MCPg → PostgreSQL**. The default access
mode is **read-only**; `restricted` adds DML; `unrestricted` adds everything,
with `DDL` / `SHELL` / `LISTEN` each behind an explicit `MCPG_ALLOW_*` opt-in.

## Components that matter most / least

**Most (focus here):**
- `src/mcpg/sql/` — the SQL-safety kernel. `allowlist.py` (policy-as-data:
  permitted statements / AST nodes / functions / extensions), `safety.py`
  (`SafeSqlDriver`: parse + walk + read-only execute), `driver.py` (pool +
  `obfuscate_password` credential redaction). **An allowlist bypass is the
  top risk.**
- `src/mcpg/policy.py` + the tenancy path — access-mode gates and the
  per-request `SET LOCAL ROLE` isolation (multi-tenant RLS).
- `src/mcpg/oidc.py` — JWT signature / claims / JWKS verification.
- `src/mcpg/shell.py` — subprocess invocation (command-injection surface).
- Identifier handling (`src/mcpg/identifiers.py` `quote_identifier`) wherever
  a tool arg becomes an identifier.

**Less (in scope, lower priority):** the ~250 read/introspection tools, the
NL→SQL provider plumbing, the audit trail + redaction, the rate limiter.

**Out of scope:** `tests/`, `docs/`, `benchmarks/`, `packaging/`; PostgreSQL
itself; third-party dependencies (`pglast`, `psycopg`, `pyjwt`, …) — report
those upstream; and the by-design "root" combination of `unrestricted` mode
**with** `MCPG_ALLOW_DDL=true`.

## How to exercise it

MCPg needs a live PostgreSQL to run end-to-end, and the scan VM has none — but
the **SQL-safety kernel validates by parsing, so it is fully exercisable
without a database**, as are the auth/policy units:

- `uv run pytest tests/unit/test_sql_kernel_safety.py tests/unit/test_sql_kernel_fuzz.py tests/unit/test_sql_kernel_internals.py tests/unit/test_sql_safety.py`
  — the adversarial + fuzz + allowlist corpora for the SQL-safety kernel.
- `uv run pytest tests/unit/test_oidc.py tests/unit/test_tenancy.py` — the
  auth / multi-tenancy paths.
- Directly probe the kernel: import `mcpg.sql.SafeSqlDriver`, feed it crafted
  SQL (stacked statements, non-allowlisted functions, `SELECT … FOR UPDATE`,
  `EXPLAIN ANALYZE`, `COPY`, `DO`, comment/encoding tricks), and assert it
  rejects at parse time. The allowlist itself is `src/mcpg/sql/allowlist.py`.
- The access-mode gates live in `src/mcpg/policy.py`; check that no
  read-only-reachable path crosses into a write/DDL/SHELL capability.

## How you rate severity

Framed for an agent-driven database server:

- **Critical** — any agent input that **bypasses the SQL allowlist** to run a
  write / DDL / DCL, a stacked statement, or a non-allowlisted function in a
  mode that should forbid it (above all in **read-only** mode); **command
  injection** via the shell tools; **auth bypass** (a forged/malformed JWT
  accepted by `oidc.py`).
- **High** — **capability/access-mode escalation** (a read-only or
  `restricted` deployment reaching a write/DDL/shell path without its gate);
  **tenancy/RLS escape** (a request executing under the wrong `SET LOCAL
  ROLE`); **SSRF / unintended egress** via NL→SQL provider, replica, or JWKS
  URLs; **credential leakage** that defeats `obfuscate_password` redaction in
  logs / the audit trail.
- **Medium** — unauthenticated **resource exhaustion / DoS** (unbounded
  result sets, rate-limiter or statement-timeout bypass); audit-chain
  integrity breaks.
- **Low / info** — hardening nits.

## Anything to leave alone

- Third-party code and dependencies (`pglast`, `psycopg`, `pyjwt`, and any
  `src/mcpg/_vendor/` contents) — report upstream, not here.
- **Intentional, documented, opt-in dangerous capabilities** when the operator
  has explicitly enabled their gate (`MCPG_ALLOW_DDL` / `MCPG_ALLOW_SHELL` /
  `MCPG_ALLOW_LISTEN` in `unrestricted` mode). These are by-design "you asked
  for root" trade-offs — **not** vulnerabilities unless a *lower* mode can
  reach them **without** the gate.
- The breadth of `ALLOWED_EXTENSIONS` in `allowlist.py` (already noted): it is
  only reachable under `unrestricted` **and** `MCPG_ALLOW_DDL`, and creating an
  extension does not add its functions to the function allowlist, so the blast
  radius is bounded.
</content>
