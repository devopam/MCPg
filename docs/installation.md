# MCPg Installation Guide

How to install and configure the MCPg PostgreSQL MCP server. This is a living
document — it is updated as installation and configuration options change.

## Prerequisites

- **Python 3.12 or newer**.
- **PostgreSQL 14–18** reachable from where MCPg runs. (Older versions may
  work but are not tested.)
- **[uv](https://docs.astral.sh/uv/)** for installing from source.
- Optionally **Docker**, to run MCPg as a container.

## Install from PyPI (recommended)

```bash
pip install mcpg
# or, if you prefer uv:
uv tool install mcpg
```

`pip install mcpg` puts the `mcpg` console script on your PATH and pulls
the runtime deps (`mcp[cli]`, `psycopg[binary]`, `psycopg-pool`, `pglast`,
`httpx`, `pyjwt[crypto]`). Verify with:

```bash
mcpg --version
```

`uv tool install mcpg` is the equivalent for the `uv` toolchain — it
isolates MCPg in its own venv and exposes the `mcpg` script globally,
without affecting other Python projects on the same machine.

## Install from source

```bash
git clone https://github.com/devopam/MCPg
cd MCPg
uv sync
```

`uv sync` creates a virtual environment and installs MCPg with the `mcpg`
console script. Pick this path if you want to follow `main`, run the test
suite, or develop against the codebase.

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

**Core:**

| Variable             | Default       | Description |
|----------------------|---------------|-------------|
| `MCPG_DATABASE_URL`  | *(required)*  | PostgreSQL connection URL (`postgresql://user:pass@host:port/db`) |
| `MCPG_ACCESS_MODE`   | `read-only`   | `read-only`, `restricted`, or `unrestricted` |
| `MCPG_ALLOW_DDL`     | `false`       | Unlock `run_ddl`, `enable_extension`, migrations, TimescaleDB writes, AGE writes (also needs `unrestricted`) |
| `MCPG_ALLOW_SHELL`   | `false`       | Unlock `dump_database`, `restore_database`, `copy_table_between_databases` (also needs `unrestricted`) |
| `MCPG_ALLOW_LISTEN`  | `false`       | Unlock the LISTEN/NOTIFY family (also needs `unrestricted`) |
| `MCPG_TRANSPORT`     | `stdio`       | `stdio`, `streamable-http`, or `sse` |
| `MCPG_HTTP_HOST`     | `127.0.0.1`   | Bind host for the HTTP transports |
| `MCPG_HTTP_PORT`     | `8000`        | Bind port for the HTTP transports |
| `MCPG_POOL_MIN_SIZE` | `1`           | Minimum pooled connections |
| `MCPG_POOL_MAX_SIZE` | `5`           | Maximum pooled connections (peak query concurrency) |
| `MCPG_AUDIT_PERSIST` | `false`       | Write the audit trail to `mcpg.audit_events` table (otherwise in-memory only) |
| `MCPG_LOG_LEVEL`     | `INFO`        | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

**HTTP authn + multi-tenancy (HTTP transports only):**

| Variable | Default | Description |
|---|---|---|
| `MCPG_HTTP_AUTH_TOKEN` | unset | Static bearer token. Required when `MCPG_AUTH_MODE=static` (the default) and the HTTP transport is in use. |
| `MCPG_AUTH_MODE` | `static` | `static` (constant-time token compare) or `oidc` (full JWT validation). |
| `MCPG_OIDC_ISSUER` / `MCPG_OIDC_AUDIENCE` | unset | Required when `auth_mode=oidc`. The issuer's `/.well-known/openid-configuration` is fetched and cached at startup. |
| `MCPG_OIDC_JWKS_URL` | unset | Optional override — skip OIDC discovery and point straight at the JWKS endpoint. |
| `MCPG_OIDC_ROLE_CLAIM` | unset | When set, the named JWT claim becomes the per-request PG role (composes with multi-tenancy). |
| `MCPG_DEFAULT_ROLE` | unset | Static default PG role for `SET LOCAL ROLE`-driven multi-tenancy. |
| `MCPG_ALLOWED_ROLES` | empty | Comma-separated allowlist for `MCPG_DEFAULT_ROLE` and the `X-MCPG-Role` header / OIDC role claim. |

**Read-replica routing:**

| Variable | Default | Description |
|---|---|---|
| `MCPG_REPLICA_URLS` | unset | Comma-separated read-replica DSNs. When set, `force_readonly` queries round-robin across healthy replicas; writes always go to the primary. Failed replicas degrade for 30 s and self-heal. |

**Natural-language → SQL (optional):**

| Variable | Default | Description |
|---|---|---|
| `MCPG_NL2SQL_PROVIDER` | unset | `anthropic`, `openai`, or `gemini`. Unset → `translate_nl_to_sql` reports unavailable. |
| `MCPG_NL2SQL_API_KEY` | unset | API key. Falls back to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `GOOGLE_API_KEY` if unset. |
| `MCPG_NL2SQL_MODEL` | provider-default | Override the model id (e.g. `claude-sonnet-4-6`, `gpt-4o-mini`, `gemini-2.0-flash`). |
| `MCPG_NL2SQL_BASE_URL` | provider-default | OpenAI-compatible endpoint override (Ollama, vLLM, OpenRouter, ...). |
| `MCPG_NL2SQL_MAX_TOKENS` | `2048` | Per-call response budget. Hard cap 16384. |

**Listener tuning:**

| Variable | Default | Description |
|---|---|---|
| `MCPG_LISTEN_QUEUE_MAX` | `1000` | Max queued NOTIFY messages per subscription. |
| `MCPG_SHELL_TIMEOUT_SEC` | `60` | Hard timeout for `dump_database` / `restore_database` / etc. |
| `MCPG_SHELL_MAX_OUTPUT_BYTES` | `64 MiB` | Hard cap on subprocess output the agent receives. |

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
