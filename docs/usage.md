# MCPg Usage Guide

How to install, configure, run, and connect to the MCPg PostgreSQL MCP
server.

## Install

MCPg targets Python 3.12+. From source (until a PyPI release is published):

```bash
git clone https://github.com/devopam/MCPg
cd MCPg
uv sync
```

This installs the `mcpg` console script into the project environment.

## Configure

MCPg is configured entirely through environment variables.

| Variable             | Default       | Description |
|----------------------|---------------|-------------|
| `MCPG_DATABASE_URL`  | *(required)*  | PostgreSQL connection URL (`postgresql://user:pass@host:port/db`) |
| `MCPG_ACCESS_MODE`   | `read-only`   | `read-only`, `restricted`, or `unrestricted` |
| `MCPG_ALLOW_DDL`     | `false`       | Allow the `run_ddl` tool (also needs `unrestricted`) |
| `MCPG_TRANSPORT`     | `stdio`       | `stdio`, `streamable-http`, or `sse` |
| `MCPG_HTTP_HOST`     | `127.0.0.1`   | Bind host for HTTP transports |
| `MCPG_HTTP_PORT`     | `8000`        | Bind port for HTTP transports |
| `MCPG_POOL_MIN_SIZE` | `1`           | Minimum pooled connections |
| `MCPG_POOL_MAX_SIZE` | `5`           | Maximum pooled connections (peak query concurrency) |
| `MCPG_LOG_LEVEL`     | `INFO`        | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |

Connection credentials are never written to logs; the settings repr and audit
log redact them.

### Access modes

| Mode           | Tools exposed |
|----------------|---------------|
| `read-only`    | introspection, `run_select`, `explain_query`, `analyze_query_plan`, health/tuning |
| `restricted`   | same as read-only (execution constraints applied per tool) |
| `unrestricted` | the above **plus** `run_write` (and `run_ddl` if `MCPG_ALLOW_DDL=true`) |

Read-only is the safe default. Grant the database role only the privileges
the workload needs — MCPg's enforcement is defence in depth, not a substitute
(see [`security.md`](security.md)).

## Run

```bash
MCPG_DATABASE_URL=postgresql://localhost/mydb uv run mcpg
```

This runs the server on the configured transport (`stdio` by default).

## Connect an MCP client

For a stdio client such as Claude Desktop, add MCPg to the client's MCP
server configuration:

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

For remote use, set `MCPG_TRANSPORT=streamable-http` and connect the client
to `http://<host>:<port>/mcp`. Authentication of remote clients is not yet
provided — see [`security.md`](security.md).

## Tools

See [`tools.md`](tools.md) for the full tool reference.

## Troubleshooting

- **"configuration error" on start** — a required/invalid environment
  variable; the message names it.
- **A write tool is missing** — check `MCPG_ACCESS_MODE=unrestricted` (and
  `MCPG_ALLOW_DDL=true` for `run_ddl`).
- **`analyze_workload` reports `available: false`** — the `pg_stat_statements`
  extension is not installed in the target database.
- **Connection failures** — verify `MCPG_DATABASE_URL` and that the database
  is reachable; errors are reported with the password redacted.
