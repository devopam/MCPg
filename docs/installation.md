# MCPg Installation Guide

How to install, configure, and verify MCPg. The complete
`MCPG_*` environment-variable reference lives in the
[README](../README.md#configuration); this guide focuses on
**getting a working install** and the most common configuration
paths.

---

## Prerequisites

- **Python 3.12 or newer** (3.12 + 3.13 are tested in CI).
- **PostgreSQL 14–18** reachable from where MCPg runs. CI matrix
  covers all five versions on every push. Older versions may work
  but aren't tested.
- A **least-privilege database role** for MCPg to connect with —
  see [Database privileges](#database-privileges) below.
- Optional, depending on path:
  - **[uv](https://docs.astral.sh/uv/)** for source installs or for
    `uv tool install mcpg`.
  - **Docker** if you'd rather run MCPg in a container.

---

## Install

### Option 1 — From PyPI (recommended)

```bash
pip install mcpg
```

Or, with `uv`'s globally-isolated tool install:

```bash
uv tool install mcpg
```

Either path puts an `mcpg` console script on your `PATH` and pulls
the runtime dependencies (`mcp[cli]`, `psycopg[binary]`,
`psycopg-pool`, `pglast`, `httpx`, `pyjwt[crypto]`).

Verify the install:

```bash
mcpg --version
# → mcpg 0.5.1
```

### Option 2 — Docker

```bash
docker build -t mcpg https://github.com/devopam/MCPg.git
docker run --rm -p 8000:8000 \
    -e MCPG_DATABASE_URL=postgresql://user:pass@host:5432/db \
    -e MCPG_ACCESS_MODE=read-only \
    mcpg
```

The image is a hardened multi-stage build: the runtime stage drops
the build toolchain and runs as `uid=10001 / gid=10001` with a
`nologin` shell. Application files are root-owned and read-only to
the runtime user.

### Option 3 — From source (developers)

```bash
git clone https://github.com/devopam/MCPg && cd MCPg
uv sync
uv run mcpg --version
```

`uv sync` creates a virtual environment with all runtime + dev
dependencies. Pick this path to follow `main`, run the test suite,
or contribute.

---

## Quick start

The minimum to get running locally:

```bash
export MCPG_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mydb
mcpg
```

That starts MCPg on the **stdio** transport in **read-only** mode,
ready to be consumed by an MCP client (Claude Desktop, Cursor,
Continue, etc.). See the next section for how to wire it into a
specific client.

For HTTP-based clients:

```bash
export MCPG_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mydb
export MCPG_TRANSPORT=streamable-http
export MCPG_HTTP_PORT=8000
export MCPG_HTTP_AUTH_TOKEN=...    # optional but strongly recommended
mcpg
```

---

## Wire it into an MCP client

### Claude Desktop (`stdio`)

`claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`;
Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mcpg": {
      "command": "uvx",
      "args": ["mcpg"],
      "env": {
        "MCPG_DATABASE_URL": "postgresql://user:pass@localhost:5432/mydb",
        "MCPG_ACCESS_MODE": "read-only"
      }
    }
  }
}
```

Restart Claude Desktop after editing the file. (If you've installed
MCPg via `pip install mcpg`, you can use `"command": "mcpg"` with no
`args`.)

### Cursor, Continue, or any HTTP MCP client

```bash
export MCPG_DATABASE_URL=postgresql://user:pass@localhost:5432/mydb
export MCPG_TRANSPORT=streamable-http
export MCPG_HTTP_AUTH_TOKEN=<random_long_token>
mcpg
```

Configure the client to connect to `http://<host>:8000/mcp` and
send `Authorization: Bearer <random_long_token>`.

For production HTTP deployments, prefer OIDC over a static token —
set `MCPG_AUTH_MODE=oidc`, `MCPG_OIDC_ISSUER`, `MCPG_OIDC_AUDIENCE`,
and optionally `MCPG_OIDC_ROLE_CLAIM` to map JWT claims to PG
roles. The OIDC flow validates JWTs against the issuer's JWKS
(asymmetric algorithms only — RS256/ES256 families).

---

## Configuration

MCPg is configured **entirely through environment variables**. The
only required one is `MCPG_DATABASE_URL`; all others have safe
defaults.

The full reference (all 38 `MCPG_*` variables, grouped by area, with
defaults and descriptions) is in the
[README](../README.md#configuration). The summaries below give you
the minimum set per common scenario.

### Common scenarios

| Scenario | Set |
|---|---|
| **Local exploration**, read-only | `MCPG_DATABASE_URL` |
| **Read-write app access** | `MCPG_ACCESS_MODE=restricted` |
| **DBA toolkit** (DDL, vacuum, etc.) | `MCPG_ACCESS_MODE=unrestricted` + `MCPG_ALLOW_DDL=true` |
| **Dump / restore** subprocess tools | `MCPG_ACCESS_MODE=unrestricted` + `MCPG_ALLOW_SHELL=true` |
| **`LISTEN/NOTIFY`** event streams | `MCPG_ACCESS_MODE=unrestricted` + `MCPG_ALLOW_LISTEN=true` |
| **HTTP transport** with static bearer | `MCPG_TRANSPORT=streamable-http` + `MCPG_HTTP_AUTH_TOKEN=…` |
| **HTTP transport** with OIDC | `MCPG_TRANSPORT=streamable-http` + `MCPG_AUTH_MODE=oidc` + `MCPG_OIDC_ISSUER=…` + `MCPG_OIDC_AUDIENCE=…` |
| **Multi-tenant SaaS** | `MCPG_DEFAULT_ROLE=tenant_a` + `MCPG_ALLOWED_ROLES=tenant_a,tenant_b,…` |
| **Read-replica fan-out** | `MCPG_REPLICA_URLS=postgresql://…?sslmode=require,postgresql://…?sslmode=require` |
| **NL→SQL** via Anthropic | `MCPG_NL2SQL_PROVIDER=anthropic` (auto-uses `ANTHROPIC_API_KEY`) |
| **Audit persistence** | `MCPG_AUDIT_PERSIST=true` |
| **Prometheus metrics** | (always on for HTTP transports — `GET /metrics`) |

### TLS enforcement (important)

By default MCPg **refuses to start** if `MCPG_DATABASE_URL` (or any
entry in `MCPG_REPLICA_URLS`) points at a **non-loopback host**
without TLS enforcement. PostgreSQL's libpq accepts plaintext
fallback under `sslmode=disable | allow | prefer` (and an unset
`sslmode` defaults to `prefer`) — which means a misconfigured
production DSN can leak credentials over the network without anyone
noticing.

To fix at the DSN level (recommended):

```
postgresql://user:pass@db.example.com:5432/app?sslmode=require
```

…or `verify-ca` / `verify-full` for stricter validation. Loopback
hosts (`localhost`, `127.0.0.1`, `::1`) are always exempt.

If you genuinely need to run plaintext for a temporary dev /
internal use, the explicit opt-out is:

```bash
export MCPG_ALLOW_INSECURE_TLS=true
```

The startup error message names exactly which DSN failed the check
(including the replica index if it was one of your
`MCPG_REPLICA_URLS` entries).

### Database privileges

Connect MCPg with a **least-privilege database role** — ideally one
granted only the privileges the workload needs.

MCPg's access-mode enforcement (`read-only` / `restricted` /
`unrestricted`) and capability gates (`MCPG_ALLOW_DDL` /
`MCPG_ALLOW_SHELL` / `MCPG_ALLOW_LISTEN`) are a **second line of
defence**, not a substitute for correct database-side permissions.
A misconfigured `unrestricted + MCPG_ALLOW_DDL=true` deployment with
a superuser DSN is by-design root access; ensure that combination
matches operator intent.

Typical setup for a read-only deployment:

```sql
CREATE ROLE mcpg_reader LOGIN PASSWORD 'change-me';
GRANT CONNECT ON DATABASE mydb TO mcpg_reader;
GRANT USAGE ON SCHEMA public TO mcpg_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcpg_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO mcpg_reader;
```

See [`security.md`](security.md) for the full threat model and
[`security-hardening.md`](security-hardening.md) for the shipped
and queued hardening features.

---

## Verify the installation

```bash
MCPG_DATABASE_URL=postgresql://localhost/mydb mcpg
```

On stdio (the default), the server waits silently for an MCP
client to attach. To exercise a tool round-trip, point a client at
it and call `get_server_info` — see the
[User Guide](user-guide.md#connecting-an-mcp-client).

For HTTP transports, MCPg also exposes:

- `GET /healthz` → liveness probe.
- `GET /readyz` → readiness probe (verifies a pool connection).
- `GET /metrics` → Prometheus-format metrics
  (`mcpg_tool_calls_total{tool,status}` and
  `mcpg_tool_duration_seconds_*`).

---

## Troubleshooting

- **Startup error: "configuration error: …"**
  A required env var is missing or invalid; the message names it.
  See the [README env-var reference](../README.md#configuration).
- **Startup error: "…points at a remote host … but its sslmode is
  `prefer`"**
  TLS enforcement caught a plaintext-capable DSN. Add
  `?sslmode=require` to the DSN, or set
  `MCPG_ALLOW_INSECURE_TLS=true` if it's intentional.
- **`mcpg: command not found`** after a `pip install mcpg`
  Your Python `bin/` is not on `PATH`. Either activate the venv or
  use `python -m mcpg`.
- **A write tool is missing**
  Set `MCPG_ACCESS_MODE=unrestricted` plus the matching gate:
  `MCPG_ALLOW_DDL=true` for DDL / migrations / extensions,
  `MCPG_ALLOW_SHELL=true` for `dump_database` / `restore_database` /
  `copy_table_between_databases`, `MCPG_ALLOW_LISTEN=true` for
  `subscribe_channel` / `poll_notifications` / …
- **`fuzzy_search` / `analyze_workload` / `vector_search` reports
  `available: false`**
  The corresponding PostgreSQL extension (`pg_trgm` /
  `pg_stat_statements` / `vector` / `postgis` / `timescaledb` /
  `age`) isn't installed in your database. MCPg degrades
  gracefully rather than failing — install the extension when
  ready.
- **A query is rejected by `run_select`**
  Only safe read-only statements pass the SafeSQL allowlist; writes,
  DDL, and multi-statement input are refused by design. Use
  `run_write` / `run_ddl` (under `unrestricted` mode) for those.
- **Connection failures**
  Verify `MCPG_DATABASE_URL` and that the database is reachable.
  Errors are logged with the password redacted.
- **`prepare_migration` refuses with "cannot run inside a
  transaction"**
  The candidate SQL contains a `CONCURRENTLY` / `VACUUM` /
  `ALTER SYSTEM` statement. The staged-migration workflow always
  wraps the candidate in `BEGIN ... COMMIT`; for those, use
  `run_ddl` directly.
- **`mcpg --version` doesn't print anything**
  Pre-v0.5.1 builds shipped without the `--version` flag. Update
  with `pip install --upgrade mcpg`.

---

## Next steps

- [User Guide](user-guide.md) — concepts, connecting clients, and a
  feature-by-feature walkthrough.
- [Tool Tour](tour.md) — compact discovery of every tool MCPg
  registers, grouped by intent.
- [Cookbook](cookbook.md) — task-oriented recipes for common
  workflows.
- [Tool Reference](tools.md) — exhaustive per-tool documentation.
- [Security model](security.md) and the
  [security hardening roadmap](security-hardening.md).
- [Release process](release-process.md) — how new versions ship to
  PyPI.
- [Architecture](architecture.md) — module map and design.
