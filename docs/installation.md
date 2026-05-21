# MCPg Installation Guide

How to install and configure the MCPg PostgreSQL MCP server. This is a living
document — it is updated as installation and configuration options change.

## Prerequisites

- **Python 3.12 or newer**.
- **PostgreSQL 14–17** reachable from where MCPg runs. (Older versions may
  work but are not tested.)
- **[uv](https://docs.astral.sh/uv/)** for installing from source.
- Optionally **Docker**, to run MCPg as a container.

## Install from source

```bash
git clone https://github.com/devopam/MCPg
cd MCPg
uv sync
```

`uv sync` creates a virtual environment and installs MCPg with the `mcpg`
console script. A PyPI package is planned for a future release.

## Install with Docker

```bash
docker build -t mcpg .
docker run --rm -p 8000:8000 \
    -e MCPG_DATABASE_URL=postgresql://user:pass@host:5432/db \
    -e MCPG_ACCESS_MODE=read-only \
    mcpg
```

The image runs as an unprivileged user and defaults to the streamable-HTTP
transport bound to `0.0.0.0:8000`.

## Configuration

MCPg is configured entirely through environment variables.

| Variable             | Default       | Description |
|----------------------|---------------|-------------|
| `MCPG_DATABASE_URL`  | *(required)*  | PostgreSQL connection URL (`postgresql://user:pass@host:port/db`) |
| `MCPG_ACCESS_MODE`   | `read-only`   | `read-only`, `restricted`, or `unrestricted` |
| `MCPG_ALLOW_DDL`     | `false`       | Allow the `run_ddl` / `enable_extension` tools (also needs `unrestricted`) |
| `MCPG_TRANSPORT`     | `stdio`       | `stdio`, `streamable-http`, or `sse` |
| `MCPG_HTTP_HOST`     | `127.0.0.1`   | Bind host for the HTTP transports |
| `MCPG_HTTP_PORT`     | `8000`        | Bind port for the HTTP transports |
| `MCPG_POOL_MIN_SIZE` | `1`           | Minimum pooled connections |
| `MCPG_POOL_MAX_SIZE` | `5`           | Maximum pooled connections (peak query concurrency) |
| `MCPG_LOG_LEVEL`     | `INFO`        | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

A missing or invalid variable causes a clear `configuration error` on
startup, naming the offending variable. Credentials are never written to
logs — the settings repr and the audit log redact them.

### Database privileges

Connect MCPg with a **least-privilege database role** — ideally one granted
only the privileges the workload needs. MCPg's access-mode enforcement is a
second line of defence, not a substitute for correct database-side
permissions. See [`security.md`](security.md).

## Verify the installation

```bash
MCPG_DATABASE_URL=postgresql://localhost/mydb uv run mcpg
```

The server starts on the configured transport (`stdio` by default). To
confirm it can reach the database, connect an MCP client and call
`get_server_info` — see the [User Guide](user-guide.md).

## Next steps

- [User Guide](user-guide.md) — concepts, connecting a client, using the tools
- [Tool Reference](tools.md) — every MCP tool and its parameters
- [Architecture](architecture.md) — how MCPg is built
