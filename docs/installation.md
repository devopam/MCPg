# MCPg Installation Guide

How to install, configure, and verify MCPg. The complete
`MCPG_*` environment-variable reference lives in the
[README](../README.md#configuration); this guide focuses on
**getting a working install** and the most common configuration
paths.

---

## Prerequisites

- **Python 3.12 or newer** (3.12–3.14 supported; CI runs the
  suite on 3.14).
- **PostgreSQL 14–18** reachable from where MCPg runs. CI runs the
  full suite against 14–18 on every push, plus an experimental
  PG 19 lane and a WarehousePG characterisation lane. Older
  versions may work but aren't tested.
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
# → mcpg 0.6.10
```

### Option 2 — Docker

Build the image (identical on every OS):

```bash
docker build -t mcpg https://github.com/devopam/MCPg.git
```

Then run it. The only per-OS difference is the line-continuation
character — pick the block for your shell:

**Linux / macOS (bash/zsh)**

```bash
docker run --rm --name mcpg -p 8000:8000 \
    -e MCPG_DATABASE_URL=postgresql://user:pass@host:5432/db \
    -e MCPG_ACCESS_MODE=read-only \
    mcpg
```

**Windows (PowerShell)**

```powershell
docker run --rm --name mcpg -p 8000:8000 `
    -e MCPG_DATABASE_URL=postgresql://user:pass@host:5432/db `
    -e MCPG_ACCESS_MODE=read-only `
    mcpg
```

**Windows (Command Prompt)**

```bat
docker run --rm --name mcpg -p 8000:8000 -e MCPG_DATABASE_URL=postgresql://user:pass@host:5432/db -e MCPG_ACCESS_MODE=read-only mcpg
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

MCPg is configured entirely through environment variables, and the
**only per-OS difference in this whole guide is how you set them**. The
`mcpg` command itself, `pip`, `uv`, and `docker` are identical on every
platform. Set variables like this:

| Shell | Set a variable |
|---|---|
| **Linux / macOS** (bash/zsh) | `export MCPG_DATABASE_URL=postgresql://…` |
| **Windows — PowerShell** | `$env:MCPG_DATABASE_URL = "postgresql://…"` |
| **Windows — Command Prompt** | `set MCPG_DATABASE_URL=postgresql://…` |

Every `export …` example below uses the Linux/macOS form; translate it
to your shell with the table above.

The minimum to get running locally (stdio transport, read-only mode):

**Linux / macOS (bash/zsh)**

```bash
export MCPG_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mydb
mcpg
```

**Windows (PowerShell)**

```powershell
$env:MCPG_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/mydb"
mcpg
```

**Windows (Command Prompt)**

```bat
set MCPG_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mydb
mcpg
```

That starts MCPg on the **stdio** transport in **read-only** mode,
ready to be consumed by an MCP client (Claude Desktop, Cursor,
Continue, etc.). See the next section for how to wire it into a
specific client.

For HTTP-based clients:

**Linux / macOS (bash/zsh)**

```bash
export MCPG_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mydb
export MCPG_TRANSPORT=streamable-http
export MCPG_HTTP_PORT=8000
export MCPG_HTTP_AUTH_TOKEN=...    # optional but strongly recommended
mcpg
```

**Windows (PowerShell)**

```powershell
$env:MCPG_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/mydb"
$env:MCPG_TRANSPORT = "streamable-http"
$env:MCPG_HTTP_PORT = "8000"
$env:MCPG_HTTP_AUTH_TOKEN = "..."    # optional but strongly recommended
mcpg
```

**Windows (Command Prompt)**

```bat
set MCPG_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mydb
set MCPG_TRANSPORT=streamable-http
set MCPG_HTTP_PORT=8000
set MCPG_HTTP_AUTH_TOKEN=...
mcpg
```

---

## Wire it into an MCP client

### Claude Desktop — one-click extension (recommended)

Download `mcpg-<version>.mcpb` from the
[latest GitHub release](https://github.com/devopam/MCPg/releases/latest)
and open it with Claude Desktop (double-click, or Settings →
Extensions → drag it in). The install dialog prompts for:

- **PostgreSQL connection URL** — stored in the operating system's
  keychain (never in plain-text config).
- **Access mode** — defaults to `read-only`.

The bundle is a ~2 kB `uv`-type MCPB: Claude Desktop resolves the
pinned `mcpg` release from PyPI for your platform at install time, so
there is nothing else to install. Additional environment variables
(replicas, capability gates, NL→SQL keys) can still be layered on via
the manual config below if you need them.

### Claude Desktop (`stdio`, manual config)

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

The full reference (every `MCPG_*` variable, grouped by area, with
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
| **HTTP transport** with IP allowlist | `MCPG_HTTP_IP_ALLOWLIST=10.0.0.0/8,192.168.1.0/24` (applied before auth; matched against the immediate peer — `X-Forwarded-For` is **not** honoured, so deployments behind a reverse proxy must enforce the allowlist at the proxy layer) |
| **HTTP transport** with TLS | `MCPG_HTTP_TLS_CERTFILE=/etc/mcpg/cert.pem` + `MCPG_HTTP_TLS_KEYFILE=/etc/mcpg/key.pem` |
| **HTTP transport** with mTLS | the TLS pair above + `MCPG_HTTP_TLS_CA_CERTS=/etc/mcpg/ca.pem` + `MCPG_HTTP_TLS_CLIENT_CERT_REQUIRED=true` |
| **Cloud secrets** (Vault) | `MCPG_SECRETS_BACKEND=vault` + `MCPG_VAULT_ADDR=…` + `MCPG_VAULT_TOKEN=…` (optional `MCPG_VAULT_NAMESPACE=…`, `MCPG_VAULT_PATH_PREFIX=secret/mcpg`) |
| **Cloud secrets** (AWS) | `MCPG_SECRETS_BACKEND=aws` + `MCPG_AWS_SECRET_ID=arn:aws:secretsmanager:…` |
| **Cloud secrets** (GCP) | `MCPG_SECRETS_BACKEND=gcp` + `MCPG_GCP_SECRET_NAME=projects/<id>/secrets/<name>/versions/latest` |
| **OpenTelemetry tracing** | `pip install 'mcpg[otel]'` + `MCPG_OTEL_ENABLED=true` (+ optional `MCPG_OTEL_SERVICE_NAME=mcpg-prod`) — emits one span per `call_tool`; argument values are deliberately not attached |
| **Slow-call logging** | `MCPG_SLOW_CALL_THRESHOLD_MS=500` (any tool slower than this logs a structured record); `MCPG_LOG_FORMAT=json` for structured logging |
| **Multi-tenant SaaS** | `MCPG_DEFAULT_ROLE=tenant_a` + `MCPG_ALLOWED_ROLES=tenant_a,tenant_b,…` |
| **Read-replica fan-out** | `MCPG_REPLICA_URLS=postgresql://…?sslmode=require,postgresql://…?sslmode=require` |
| **Multiple databases (read-only secondaries)** | `MCPG_SECONDARY_DATABASE_URLS=analytics=postgresql://…?sslmode=require,reporting=postgresql://…?sslmode=require` — read-capable tools take an optional `database` arg; secondaries are read-only (PostgreSQL-enforced). Call `list_databases` to discover ids. |
| **NL→SQL** — single provider | Set `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` / `GEMINI_API_KEY`). MCPg auto-picks the default. |
| **NL→SQL** — multi-provider routing | Set all the vendor keys you want active; callers pass `provider="anthropic"\|"openai"\|"gemini"` per call. |
| **Audit persistence** | `MCPG_AUDIT_PERSIST=true` |
| **Prometheus metrics** | (always on for HTTP transports — `GET /metrics`) |

### TLS enforcement (important)

By default MCPg **refuses to start** if `MCPG_DATABASE_URL` (or any
entry in `MCPG_REPLICA_URLS` / `MCPG_SECONDARY_DATABASE_URLS`) points
at a **non-loopback host** without TLS enforcement. PostgreSQL's libpq accepts plaintext
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
`MCPG_REPLICA_URLS` entries, or the secondary name if it was an
`MCPG_SECONDARY_DATABASE_URLS` entry).

### HTTP transport TLS / mTLS

The TLS settings above protect the **database** connection. The
**HTTP transport** can be terminated by an external proxy
(nginx / Envoy) or by MCPg directly — the in-process option drops
the proxy dependency for small deployments.

```bash
export MCPG_HTTP_TLS_CERTFILE=/etc/mcpg/cert.pem
export MCPG_HTTP_TLS_KEYFILE=/etc/mcpg/key.pem
```

Both must be set together; configuring only one is rejected at
startup so a deployment can't silently fall back to plaintext.

For mutual TLS (clients must present a cert chaining to a known
CA — useful for service-to-service deployments where bearer tokens
aren't enough):

```bash
export MCPG_HTTP_TLS_CA_CERTS=/etc/mcpg/ca.pem
export MCPG_HTTP_TLS_CLIENT_CERT_REQUIRED=true
```

Setting `CLIENT_CERT_REQUIRED=true` without `CA_CERTS` is rejected
at startup — there'd be nothing to verify against.

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
