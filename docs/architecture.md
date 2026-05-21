# MCPg Architecture

How MCPg is built. This is a living document — it is updated as the
architecture evolves. For the historical roadmap see [`../PLAN.md`](../PLAN.md);
for individual decisions see [`adr/`](adr/).

## Overview

MCPg is a single-process, async ([`asyncio`](https://docs.python.org/3/library/asyncio.html))
MCP server. An MCP client calls tools; each tool validates its input, runs
read-only or read-write SQL against PostgreSQL through a connection pool, and
returns a typed result. Every call is audited.

```
   MCP client ──stdio / streamable-HTTP / SSE──▶ AuditedFastMCP
                                                      │  (audit every call)
                                                      ▼
                                            tool wrapper (mcpg.tools)
                                                      │
                                                      ▼
                                   logic module (query / introspection / ...)
                                                      │
                                            SqlDriver / SafeSqlDriver
                                                      │  psycopg3 pool
                                                      ▼
                                                 PostgreSQL
```

## Request lifecycle

1. The client invokes a tool. `AuditedFastMCP.call_tool` (a `FastMCP`
   subclass) wraps every invocation and records an audit event on success or
   failure.
2. The tool wrapper in `mcpg.tools` pulls the request's `AppContext` (settings
   + database) from the server lifespan and obtains a `SqlDriver`.
3. The wrapper delegates to a **logic module** — `query`, `introspection`,
   `write`, `health`, `workload`, `indexing`, `textsearch`, `extensions` —
   which builds and runs the SQL and maps rows to typed dataclasses.
4. Read queries go through the vendored `SafeSqlDriver` (allowlist
   validation, forced read-only); writes go through `SqlDriver` after
   single-statement validation.

## Module map

| Module | Responsibility |
|--------|----------------|
| `mcpg.config` | Env-driven, validated `Settings` |
| `mcpg.database` | Connection-pool lifecycle (`Database`) |
| `mcpg.context` | `AppContext` shared with every tool |
| `mcpg.server` | `FastMCP` bootstrap, `AuditedFastMCP`, transports, `run` |
| `mcpg.policy` | Access-mode → capability permission table |
| `mcpg.audit` | Audit-event records and redaction |
| `mcpg.tools` | Thin MCP tool wrappers + `register_tools` |
| `mcpg.introspection` | Schema/catalog inspection queries |
| `mcpg.query` | Safe read-only query execution + plan analysis |
| `mcpg.write` | Single-statement DML / DDL execution |
| `mcpg.health` | Database health checks |
| `mcpg.workload` | Slow-query analysis (`pg_stat_statements`) |
| `mcpg.indexing` | Index recommendations |
| `mcpg.textsearch` | Trigram fuzzy search + full-text search |
| `mcpg.extensions` | Extension management (`enable_extension`) |
| `mcpg._vendor` | Vendored third-party code (see below) |

The `__main__` module is the `mcpg` console entry point.

## The vendored SQL-safety kernel

`src/mcpg/_vendor/sql/` is a pinned copy of the SQL-safety subpackage from
[`crystaldba/postgres-mcp`](https://github.com/crystaldba/postgres-mcp) (MIT).
It provides `SafeSqlDriver` — a `pglast` AST allowlist validator — and the
connection pool / driver. It is kept near-verbatim, excluded from the
coverage gate and `mypy`, and re-synced via the procedure in
`src/mcpg/_vendor/README.md`. See [ADR-0001](adr/0001-build-approach.md).

## Access-mode & capability model

`mcpg.policy` maps each access mode to a set of capabilities
(`READ`, `WRITE`, `DDL`). `register_tools` consults the policy so a tool is
only exposed when its capability is permitted: reads in every mode, writes in
`unrestricted`, DDL in `unrestricted` **and** with `MCPG_ALLOW_DDL`. There is
no module-level mutable state — settings and the database live in the server
lifespan's `AppContext`.

## Security model

Read-only by default; every agent-supplied SQL statement is parsed and
allowlist-checked before execution; writes are validated as a single
statement of an expected kind; identifiers in search tools are validated and
quoted; credentials are redacted from logs. The full threat model is in
[`security.md`](security.md).

## Graceful degradation for optional extensions

Tools that depend on an optional extension (`fuzzy_search` → `pg_trgm`,
`analyze_workload` → `pg_stat_statements`) check for the extension first and
return an `available: false` result instead of failing when it is absent.
`describe_table` and `list_indexes` surface `pgvector` / index-method details
when present without requiring the extension otherwise.

## Testing approach

MCPg is test-driven. Authored code is covered by unit tests (using fake
drivers) under a coverage gate, and by integration tests that run against a
real PostgreSQL. CI exercises the suite against PostgreSQL 14–17 on
pgvector-enabled images. The vendored kernel keeps its own upstream tests.

## Configuration & deployment

Configuration is entirely environment-variable driven (see the
[Installation Guide](installation.md)). MCPg ships as a `uv` project and a
Docker image. Scaling characteristics and tuning are documented in
[`scaling.md`](scaling.md).
