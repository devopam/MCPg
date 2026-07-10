# MCPg v0.6.11 — release notes

**Released:** 2026-07-10
**Tool surface:** **252** tools across 19 capability buckets (read-only
mode exposes a subset)
**Tests:** unit + integration suite green (PG 14 / 15 / 16 / 17 / 18 / 19
/ WarehousePG)
**Runtime:** Python 3.14

A **patch bump (0.6.10 → 0.6.11)** headlined by a multi-tenancy security
fix and a pre-review hardening pass. Backward-compatible — no tool
signatures changed.

## Security: per-request tenancy now works on HTTP/SSE

Per-request PostgreSQL role selection — the `X-MCPG-Role` header and the
per-request OIDC role claim — was silently **pinned to a session's first
request** on the `streamable-http` and `sse` transports. The role was set
in a ContextVar in the ASGI request task, but FastMCP dispatches tool
calls in a long-lived *per-session* task whose context is copied once at
session creation, so a later request's role never reached the query path.
Two tenants sharing one MCP session both ran under the first one's role.

The middleware now stashes the validated role on the request, and the
tenancy driver resolves it **per message** from the MCP SDK's request
context at query time — authoritative on every transport. A static
`MCPG_DEFAULT_ROLE` and the stdio transport were never affected.

## Advisor: `recommend_indexes` catches unindexed foreign keys

PostgreSQL auto-indexes `PRIMARY KEY` / `UNIQUE` columns but **not**
foreign keys, so an unindexed FK silently forces sequential-scan joins and
slow cascading `UPDATE`/`DELETE`. For each sequential-scan-heavy table the
advisor now recommends a **btree** on any single-column FK that has no
covering index — leading the type-driven GIN/trigram suggestions. (This is
exactly the planted flaw the `mcpg --demo` walkthrough demonstrates, which
the advisor now actually finds.)

## First-contact polish

- **`mcpg --help` / `-h`** prints usage instead of dying with a
  `MCPG_DATABASE_URL is required` config error; an unknown argument is
  reported clearly rather than falling through to the same message.
- **The stdio transport announces startup** — one stderr line
  (`ready on stdio (<mode> mode) — waiting for an MCP client`) so a
  first-time run doesn't look hung.
- **HTTP quickstart URL corrected** in the README to `/mcp` (`/sse` for
  SSE), matching where the handler is actually mounted.

## Hardening

- **Audit log error text is redacted** — a DSN embedded in an exception
  message is run through `obfuscate_password` before logging, matching the
  existing argument redaction, so a password can't reach the audit sink.
- **`EXPLAIN ANALYZE` (`io=True`) runs read-only** — the one agent-SQL
  path that executed at `force_readonly=False` now wraps execution in
  `BEGIN TRANSACTION READ ONLY` like every other path.

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.6.11   # or :latest
```

Or grab `mcpg-0.6.11.mcpb` from this release and double-click it into
Claude Desktop. No configuration changes required.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.11]` for the complete
itemised list.
