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

### Search

`fuzzy_search` ranks a text column by trigram similarity (needs `pg_trgm`).
`full_text_search` ranks documents with PostgreSQL's built-in full-text
search and accepts web-search syntax (quoted phrases, `or`, `-`).
`vector_search` finds the rows nearest to a query vector (needs `pgvector`),
and `geo_search` finds the rows nearest to a lon/lat point (needs `postgis`).

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
| `vector_search`     | `vector` (pgvector) |
| `geo_search`        | `postgis` |
| `pg_cron.*`         | `pg_cron` |
| `partman.*`         | `pg_partman` |

In `unrestricted` + `MCPG_ALLOW_DDL` mode, `enable_extension` can install an
allowlisted extension.

## Data movement (post-0.3.0)

Five tools cover moving data in / out of the database:

- **In-process exports**: `export_query` / `export_table` serialise rows
  to CSV or JSON. Read-only, no opt-in.
- **In-process imports**: `import_csv` / `import_json` bulk-load via
  `COPY ... FROM STDIN` and parametrised `executemany`. Require
  `unrestricted` (WRITE capability).
- **Subprocess dump / restore**: `dump_database` / `restore_database`
  shell out to `pg_dump` / `psql` / `pg_restore`. Require `unrestricted` +
  `MCPG_ALLOW_SHELL=true`.
- **Cross-DB copy**: `copy_table_between_databases` pipes `pg_dump` on
  one URL into `pg_restore` on another. Same opt-in.

## Reactive workflows: LISTEN/NOTIFY (post-0.3.0)

`subscribe_channel` opens a PG `LISTEN` on a dedicated connection and
returns a subscription id; `poll_notifications` drains the per-sub
bounded queue with an optional wait; `unsubscribe_channel` removes the
subscription; `list_notification_subscriptions` reports the active
ones. Requires `unrestricted` + `MCPG_ALLOW_LISTEN=true`. The
`MCPG_LISTEN_QUEUE_MAX` env var caps queue size (default 1000); overflow
drops the oldest message and surfaces `dropped_count` on the next poll.

## Staged migrations (post-0.3.0)

`prepare_migration` clones the target schema's structure into a shadow,
applies your candidate SQL there, and runs `compare_schemas` so you
review the structural diff. `complete_migration` lands it on the
target. `cancel_migration` drops the shadow without applying.
`list_pending_migrations` shows what's staged. Requires `unrestricted`
+ `MCPG_ALLOW_DDL=true` (the existing DDL gate; no new env var).

## ORM bridges (post-0.3.0)

Eight read-only exporters generate a starting schema/model file from
the live PG catalog: `generate_prisma_schema`, `generate_drizzle_schema`,
`generate_sqlalchemy_models`, `generate_sqlc_schema`,
`generate_diesel_schema`, `generate_jooq_config`, `generate_ent_schemas`,
`generate_ecto_schemas`. All share a v1 coverage boundary (base tables,
columns, PKs, single-column intra-schema FKs, enums). See
[`tools.md`](tools.md#orm-dsl-exporters) for details per exporter.

## Auditing

Every tool call — success or failure — is logged to the `mcpg.audit` logger
with the tool name, arguments (secrets masked), and outcome. Configure where
that logger's records are shipped in your deployment's logging setup.

With `MCPG_AUDIT_PERSIST=true`, every `run_write` and `run_ddl` is **also**
persisted to `mcpg_audit.events` (auto-created) with the redacted arguments
+ result + status. Query the table via the `list_audit_events` tool.

## Troubleshooting

- **"configuration error" on start** — a required/invalid environment
  variable; the message names it. See the [Installation Guide](installation.md).
- **A write tool is missing** — set `MCPG_ACCESS_MODE=unrestricted` (and
  `MCPG_ALLOW_DDL=true` for `run_ddl` / `enable_extension` / migration tools;
  `MCPG_ALLOW_SHELL=true` for `dump_database` / `restore_database` /
  `copy_table_between_databases`; `MCPG_ALLOW_LISTEN=true` for the
  LISTEN/NOTIFY tools).
- **`analyze_workload` / `fuzzy_search` report `available: false`** — the
  required extension is not installed in the target database.
- **A query is rejected** — `run_select` only permits safe read-only
  statements; writes, DDL, and multi-statement input are refused by design.
- **Connection failures** — verify `MCPG_DATABASE_URL` and that the database
  is reachable; errors are reported with the password redacted.
- **`prepare_migration` refuses with "cannot run inside a transaction"** —
  the candidate SQL contains a `CONCURRENTLY` / `VACUUM` / `ALTER SYSTEM`
  statement. The staged-migration workflow always wraps the candidate in
  a SET LOCAL transaction; run those statements directly via `run_ddl`
  instead.
