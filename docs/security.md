# MCPg Security Model

This document describes MCPg's threat model and the controls that mitigate
each threat. It reflects the implementation as of Phase 3; it is updated as
the project evolves.

## What MCPg is

MCPg is an MCP server that exposes a PostgreSQL database to an AI agent
through a fixed set of tools. The agent does not get a raw database
connection — it can only call the tools MCPg registers, and every call is
validated and audited.

## Trust boundaries

```
  Agent / MCP client   ──tools──▶   MCPg server   ──SQL──▶   PostgreSQL
   (untrusted input)               (trusted code)          (the asset)
```

- **The agent is untrusted.** Tool arguments — especially SQL text passed to
  `run_select` / `explain_query` — are treated as hostile input.
- **MCPg is trusted code**, but it assumes its own configuration
  (`MCPG_DATABASE_URL`, access mode) is set by a trusted operator.
- **PostgreSQL is the asset** being protected. MCPg is one client of it; it
  is not a substitute for correct database-side permissions (see below).

## Assets

1. Data in the database (confidentiality and integrity).
2. The database connection string / credentials.
3. Database availability.

## Threats and mitigations

### T1 — SQL injection / unsafe statements

The reference PostgreSQL MCP server was retired after a SQL-injection
vulnerability. MCPg's mitigation:

- All agent-supplied SQL runs through the vendored `SafeSqlDriver`, which
  parses the statement with `pglast` (the real PostgreSQL grammar) and checks
  every node against an allowlist. Statement stacking, comment escapes,
  transaction-control escapes (`COMMIT`/`ROLLBACK`/`BEGIN`), DDL, DML,
  `COPY`, and `DO` blocks are rejected **before execution**.
- Queries also run under a forced read-only transaction.
- This is locked in by an adversarial regression suite
  (`tests/unit/test_sql_safety.py`).
- Catalog introspection uses parameterised queries; no value is interpolated
  into SQL text.

### T2 — Unintended writes

- The access mode defaults to **read-only**. The `mcpg.policy` engine gates
  which tools are registered: write tools (Phase 4) are exposed only in
  `unrestricted` mode.
- `run_select` and `explain_query` force read-only transactions regardless of
  mode.

### T3 — Resource exhaustion / denial of service

- Queries run with a per-query execution timeout.
- `run_select` caps returned rows (`max_rows`, default 1000) and reports
  truncation, bounding result size.
- Connection use is bounded by the pool.

### T4 — Credential disclosure

- `Settings.__repr__` redacts the database password.
- Audit logging masks secret-named arguments and obfuscates connection-string
  passwords embedded in argument values.
- Connection errors are passed through the vendored password obfuscator.

### T5 — Lack of attribution

- `AuditedFastMCP` records every tool invocation — name, redacted arguments,
  and outcome (success or error) — to the `mcpg.audit` logger.

## Operator responsibilities (defence in depth)

MCPg is not a replacement for database-side security. Operators should:

- Connect MCPg with a **least-privilege database role** — ideally one with
  only the `SELECT` privileges the workload needs. MCPg's read-only
  enforcement is a second line of defence, not the only one.
- Use a dedicated role per deployment so audit logs and database logs can be
  correlated.
- For multi-tenant data, see "Multi-tenancy and Row-Level Security" below.
- Configure where the `mcpg.audit` logger's records are shipped and retained.

## Multi-tenancy and Row-Level Security

PostgreSQL Row-Level Security (RLS) policies are evaluated against the
**connecting role** (and session settings). MCPg connects with a *single*
database role and pools those connections, so every agent request is, from
the database's perspective, the same principal.

**Implication:** RLS that distinguishes tenants by `current_user` will *not*
isolate tenants if MCPg connects with one shared role. Do not rely on MCPg
alone for tenant isolation.

**Recommended deployment for multi-tenant databases:**

- Run **one MCPg instance per tenant**, each configured (`MCPG_DATABASE_URL`)
  with a tenant-specific, least-privilege database role. RLS policies keyed on
  that role then isolate the tenant correctly, and audit logs are per-tenant.
- Alternatively, restrict each instance to a tenant-specific schema or
  database via the connection URL and role privileges.

**Planned enhancement (post-1.0):** an optional per-request role / session
variable (`SET ROLE` or `SET app.tenant_id`) so a single MCPg instance can
serve multiple tenants under RLS. This requires careful pooled-connection
session-state management and is deliberately deferred — see `PLAN.md`.

## Out of scope (current)

- Authentication of the MCP client itself (relevant to the remote HTTP
  transport; to be addressed before remote deployment is recommended).
- Rate limiting per client.
- Encryption configuration of the database connection (use `sslmode` in the
  connection URL).

## Reporting a vulnerability

Open a security issue using the bug-report template, or contact the
maintainers privately for sensitive reports. Do not disclose details
publicly until a fix is available.
