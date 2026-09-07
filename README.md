

# MCPg

[![MCP Toplist](https://mcptoplist.com/badge/io.github.devopam%2Fmcpg.svg)](https://mcptoplist.com/server/io.github.devopam%2Fmcpg)

**A production-grade [Model Context Protocol](https://modelcontextprotocol.io)
server for PostgreSQL.** It lets AI agents safely inspect, query, operate, and
tune a Postgres database — 254 tools spanning catalog introspection,
query intelligence, natural-language SQL, structural diffs, hybrid search,
graph queries, data movement, live ops, and more.


[![PyPI version](https://img.shields.io/pypi/v/mcpg.svg)](https://pypi.org/project/mcpg/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcpg.svg)](https://pypi.org/project/mcpg/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/devopam/MCPg/blob/main/LICENSE)
[![CI](https://github.com/devopam/MCPg/actions/workflows/ci.yml/badge.svg)](https://github.com/devopam/MCPg/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/devopam/MCPg/badge)](https://scorecard.dev/viewer/?uri=github.com/devopam/MCPg)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13958/badge)](https://www.bestpractices.dev/projects/13958)
[![Stars](https://img.shields.io/github/stars/devopam/MCPg)](https://github.com/devopam/MCPg)
[![smithery badge](https://smithery.ai/badge/devopam/mcpg)](https://smithery.ai/servers/devopam/mcpg)
[![MCPg MCP server](https://glama.ai/mcp/servers/devopam/MCPg/badges/score.svg)](https://glama.ai/mcp/servers/devopam/MCPg)
[![AllMCPs Verified](https://allmcps.com/api/badge/devopam-mcpg)](https://allmcps.com/mcp/devopam-mcpg?verify=63d9d537-be6b-4e07-a720-77dd0c41411b)
[![MCPVault: claimed](https://mcpvault.io/badge/mcpg.svg?theme=dark)](https://mcpvault.io/servers/mcpg/health?utm_source=external_badge&utm_medium=referral&utm_campaign=mcp_health_report)

> **Try it live:** point an MCP client — or the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) — at the hosted, read-only demo endpoint `https://devopam-mcpg-demo.hf.space/mcp`. It serves read tools against throwaway demo data; for real use, run MCPg next to your own database (see [Quick start](#quick-start)).

### 📍 Listed On

- **[Official MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.devopam/mcpg)**
- **[mcp.so](https://mcp.so/server/mcpg---production-grade-postgresql-mcp-server/Devopam%20Mittra)**
- **[mcpservers.org](https://mcpservers.org/servers/devopam/mcpg)**
- **[Smithery](https://smithery.ai/servers/devopam/mcpg)**
- **[Glama](https://glama.ai/mcp/servers/devopam/MCPg)**

---

| Aspect              | MCPg                              |
|---------------------|-----------------------------------|
| Safety              | Read-only default + AST validation |
| Transport           | stdio + HTTP/SSE                  |
| Install             | `pip install mcpg`                |
| Postgres Versions   | 14–19                             |
| Key Differentiator  | Production observability + multi-tenancy |

## Why MCPg

- **Safe by default.** Read-only access mode. Every user-supplied SQL
  statement parses through a validated AST allowlist before execution.
  Names that reach PostgreSQL as SQL identifiers are encoded with
  delimited-identifier quoting (see [Identifier policy](#identifier-policy)),
  not rejected merely for containing hyphens or other legal characters.
  Capabilities like DDL, shell, and `LISTEN/NOTIFY` are off until you
  opt in. Every tool publishes MCP `ToolAnnotations` (`readOnlyHint`,
  `openWorldHint`) derived from those same gates, so clients can
  auto-approve reads and gate writes without guessing.
- **One server, broad surface.** Application data access (queries, search,
  cursors, NL→SQL) *and* DBA-grade operations (health checks, index tuning,
  EXPLAIN analysis, locks, vacuum, dumps, replicas, migrations) in a
  single MCP server. Agents don't have to switch tools to switch tasks.
- **PostgreSQL-native everything.** No ORM, no abstraction tax — uses
  `psycopg3` directly, speaks every `pg_*` system view, integrates with
  TimescaleDB, pgvector, PostGIS, Apache AGE, and `pg_stat_statements`
  where they're available, and degrades gracefully when they aren't.
- **Production-shaped, not demo-shaped.** Connection pooling, per-request
  `SET ROLE` multi-tenancy, read-replica routing with degraded-host
  detection, server-side cursors with dedicated connections,
  rate-limiting, audit trail with regex redaction, PG TLS enforcement
  on startup, OIDC JWT bearer auth, per-session statement / lock
  timeouts.
- **Observability built in.** Prometheus `/metrics` endpoint on the
  HTTP transport surfaces `mcpg_tool_calls_total{tool,status}` +
  `mcpg_tool_duration_seconds`. Every tool call records a structured
  audit event with credential-redacted arguments.
- **Test-driven, multi-version.** 2,500+ unit tests plus an integration
  suite that runs against a real PostgreSQL container in CI — matrix
  covers PG **14, 15, 16, 17, 18** on every push, plus PG **19 (beta)**
  as an experimental (non-blocking) entry tracked under issue #120.

---

## Identifier policy

MCPg follows a **match the sink** rule: naming limits depend on where the
value is used, not on a single global "safe name" style.

### SQL and `pg_dump` / `pg_restore` paths

Tools that address real PostgreSQL objects (query, export, import, dump,
replication, roles used in `SET ROLE`, and similar) accept any **addressable**
PostgreSQL identifier. Values are encoded with `quote_identifier` (or dump
pattern encoding for `pg_dump`), so injection is prevented by **escaping**,
not by forbidding hyphens.

| Example | Notes |
|---------|--------|
| `adm-pgbench` | Hyphen — common real schema name (issue #329) |
| `My Schema` | Space |
| `Users` | Mixed case preserved only when quoted |
| `2fa_tokens` | Leading digit requires quoting in SQL |
| `app$cfg` | `$` is allowed in PostgreSQL unquoted identifiers; still fine when quoted |
| `données` | Non-ASCII letters |
| `order` / `user` | Reserved words — must be quoted as identifiers |
| `weird"name` | Embedded `"` is doubled to `""` inside quotes |

**Always rejected** (not valid addressable identifiers): empty string, embedded
NUL (`\x00`), names longer than **63 bytes** (PostgreSQL's `NAMEDATALEN - 1`;
the server would otherwise **silently truncate**).

Callers pass the **bare** name (e.g. `adm-pgbench`). Do not add SQL quotes
yourself.

### Plain-identifier-only tools (deliberate)

Some tools feed the name into a **different** language or channel. Those keep
a stricter allowlist, typically `[A-Za-z_][A-Za-z0-9_]*`:

| Sink | Examples | Why |
|------|----------|-----|
| Generated source | Prisma, Drizzle, Diesel, Ecto, Ent, jOOQ, sqlc, SQLAlchemy export | Name becomes an identifier in TypeScript / Go / Rust / Elixir / Python |
| Graph labels | Apache AGE / graph projection labels | Extension label rules, not PG delimited identifiers |
| Shell / host command | `schedule_logical_backup` paths, some `COPY TO PROGRAM` args | Shell metacharacters are a real injection surface |
| Some SQL *literals* | `regconfig` in text-search helpers | Embedded as a string literal, not as a delimited identifier |
| Config keys | `MCPG_SECONDARY_DATABASE_URLS` names | MCPg's own ids (`[a-z0-9_]+`), not PG object names |

If such a tool rejects a name that SQL tools accept, the error should state the
**sink** and point at SQL/dump tools for the real object name.

### What agents should do

1. Prefer tool descriptions and error text over assumptions from this README.
2. On a plain-identifier rejection, retry with SQL-oriented tools (`export_table`,
   `dump_database`, `run_select`, …) using the same bare name.
3. Do not strip hyphens or rename production schemas solely to satisfy an ORM
   exporter — map names in the generator layer if you need both.

## Install

### From PyPI (recommended)

```bash
pip install mcpg
# or, in an isolated venv exposed globally:
uv tool install mcpg
```

Verify:

```bash
mcpg --version
```
