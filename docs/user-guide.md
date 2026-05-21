# MCPg User Guide

How to use the MCPg PostgreSQL MCP server once it is installed. This is a
living document — it is updated as features are added.

For installation and configuration, see the
[Installation Guide](installation.md). For the exact parameters of each tool,
see the [Tool Reference](tools.md).

## What MCPg is

MCPg is an MCP server that exposes a PostgreSQL database to an AI agent
through a fixed, audited set of tools. The agent never gets a raw database
connection — it can only call the tools MCPg registers, and every call is
validated and logged.

## Access modes

The `MCPG_ACCESS_MODE` setting decides which tools are available:

| Mode           | Tools available |
|----------------|-----------------|
| `read-only`    | introspection, querying, health & tuning (all read-only) |
| `restricted`   | same as read-only (reserved for tighter execution limits) |
| `unrestricted` | the above **plus** `run_write`, and — with `MCPG_ALLOW_DDL=true` — `run_ddl` and `enable_extension` |

Read-only is the default. Writes and DDL are deliberately gated; DDL needs a
second explicit opt-in (`MCPG_ALLOW_DDL`).

## Connecting an MCP client

### stdio (local clients, e.g. Claude Desktop)

Add MCPg to the client's MCP server configuration:

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uv",
      "args": ["run", "mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://localhost/mydb",
        "MCPG_ACCESS_MODE": "read-only"
      }
    }
  }
}
```

### Streamable HTTP (remote clients)

Set `MCPG_TRANSPORT=streamable-http` and connect the client to
`http://<host>:<port>/mcp`. Authentication of remote clients is not yet
provided — see [`security.md`](security.md) before exposing MCPg remotely.

## Working with the tools

The tools group into a few workflows. Full parameters are in the
[Tool Reference](tools.md).

### Explore the schema

`list_schemas` → `list_tables` → `describe_table` map out the database.
`list_indexes` shows a table's indexes and their access method;
`list_extensions` and `list_available_extensions` show what extensions are
installed or available. `describe_table` reports the dimension of `pgvector`
`vector(N)` columns.

### Query data

`run_select` runs a read-only SQL query — it is validated against a safety
allowlist, executed read-only, and capped at `max_rows` (default 1000) with a
`truncated` flag. `explain_query` returns a query's execution plan;
`analyze_query_plan` summarises that plan (cost, node types, sequential
scans).

### Search text

`fuzzy_search` ranks a text column by trigram similarity (needs the
`pg_trgm` extension). `full_text_search` ranks documents with PostgreSQL's
built-in full-text search and accepts web-search syntax (quoted phrases,
`or`, `-`).

### Diagnose and tune

`check_database_health` reports connection use, cache hit ratio, vacuum
backlog, and invalid indexes. `analyze_workload` surfaces the slowest queries
(needs `pg_stat_statements`). `recommend_indexes` flags large
sequentially-scanned tables and suggests index types per column.

### Change data (unrestricted mode)

`run_write` executes a single `INSERT`/`UPDATE`/`DELETE` — add a `RETURNING`
clause to get affected rows back. `run_ddl` runs a single DDL statement, and
`enable_extension` enables an allowlisted extension; both need
`MCPG_ALLOW_DDL=true`.

## Optional extensions

Some tools rely on optional PostgreSQL extensions. They **degrade
gracefully**: if the extension is not installed, the tool returns
`available: false` rather than failing.

| Tool                | Needs extension |
|---------------------|-----------------|
| `fuzzy_search`      | `pg_trgm` |
| `analyze_workload`  | `pg_stat_statements` |

In `unrestricted` + `MCPG_ALLOW_DDL` mode, `enable_extension` can install an
allowlisted extension.

## Auditing

Every tool call — success or failure — is logged to the `mcpg.audit` logger
with the tool name, arguments (secrets masked), and outcome. Configure where
that logger's records are shipped in your deployment's logging setup.

## Troubleshooting

- **"configuration error" on start** — a required/invalid environment
  variable; the message names it. See the [Installation Guide](installation.md).
- **A write tool is missing** — set `MCPG_ACCESS_MODE=unrestricted` (and
  `MCPG_ALLOW_DDL=true` for `run_ddl` / `enable_extension`).
- **`analyze_workload` / `fuzzy_search` report `available: false`** — the
  required extension is not installed in the target database.
- **A query is rejected** — `run_select` only permits safe read-only
  statements; writes, DDL, and multi-statement input are refused by design.
- **Connection failures** — verify `MCPG_DATABASE_URL` and that the database
  is reachable; errors are reported with the password redacted.
