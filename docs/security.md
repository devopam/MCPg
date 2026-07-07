# MCPg Security Model

MCPg's threat model and the controls that mitigate each threat.
Living document — kept in step with the implementation as new
mitigations land.

**Related documents.** For *how to report* a vulnerability, see
[`SECURITY.md`](../SECURITY.md) at the repo root. For the **living
roadmap** of shipped (✅) vs queued (⬜) hardening features, see
[`security-hardening.md`](security-hardening.md).

---

## What MCPg is

MCPg is an MCP server that exposes a PostgreSQL database to an AI
agent through a fixed set of tools. The agent does not get a raw
database connection — it can only call the tools MCPg registers,
and every call is validated and audited.

---

## Trust boundaries

```
  Agent / MCP client   ──tools──▶   MCPg server   ──SQL──▶   PostgreSQL
   (untrusted input)               (trusted code)          (the asset)
```

- **The agent is untrusted.** Tool arguments — especially SQL text
  passed to `run_select` / `explain_query` / `open_cursor` / NL→SQL
  generations — are treated as hostile input.
- **MCPg is trusted code**, but it assumes its own configuration
  (`MCPG_DATABASE_URL`, access mode, capability gates, audit
  redaction list) is set by a trusted operator.
- **PostgreSQL is the asset** being protected. MCPg is one client
  of it; it is not a substitute for correct database-side
  permissions.

---

## Assets

1. **Data in the database** — confidentiality and integrity.
2. **Database credentials** — both the DSN MCPg uses and any
   credentials nested in tool arguments / result payloads.
3. **Database availability.**
4. **The MCPg process itself** — its environment, its in-flight
   connections, its audit trail.

---

## Threats and mitigations

### T1 — SQL injection / unsafe statements

**Mitigation.**

- All agent-supplied SQL runs through the vendored `SafeSqlDriver`,
  which parses the statement with `pglast` (the real PostgreSQL
  grammar) and checks every AST node against an allowlist.
  Statement stacking, comment escapes, transaction-control escapes
  (`COMMIT` / `ROLLBACK` / `BEGIN`), DDL inside read paths, DML
  inside read paths, `COPY`, and `DO` blocks are rejected
  **before execution**.
- Read queries also run under a forced read-only transaction.
- Every interpolated identifier (schema / table / column / role
  names) flows through a `[A-Za-z_][A-Za-z0-9_]*` regex — user
  input never reaches the database through string concatenation.
- Locked in by an adversarial regression suite
  (`tests/unit/test_sql_safety.py` and the vendored kernel's own
  tests).
- Catalog introspection uses parameterised queries; no value is
  interpolated into SQL text.

### T2 — Unintended writes / privilege escalation through tool surface

**Mitigation.**

- The access mode defaults to **read-only**. The `mcpg.policy`
  engine gates which tools are registered: write tools are exposed
  only in `unrestricted` mode.
- DDL needs a second explicit opt-in (`MCPG_ALLOW_DDL`). Subprocess
  tools need `MCPG_ALLOW_SHELL`; `LISTEN/NOTIFY` tools need
  `MCPG_ALLOW_LISTEN`.
- `run_select` and `explain_query` force read-only transactions
  regardless of mode.
- Generated SQL from `translate_nl_to_sql` is passed through the
  same SafeSQL allowlist as hand-written SQL before any execution.

### T3 — Resource exhaustion / denial of service

**Mitigation.**

- **Per-session statement timeout** (`MCPG_STATEMENT_TIMEOUT_MS`,
  default 30 s) applied to every checked-out pool connection.
  Runaway queries self-terminate.
- **Per-session lock timeout** (`MCPG_LOCK_TIMEOUT_MS`, default
  5 s). Hanging lock waits self-terminate.
- **Result-row cap.** `run_select` caps returned rows
  (`max_rows`, default 1000) and reports truncation.
- **Subprocess caps.** `MCPG_SHELL_TIMEOUT_SEC` (default 60 s) and
  `MCPG_SHELL_MAX_OUTPUT_BYTES` (default 64 MiB) bound shell-tool
  runtime and output.
- **Cursor caps.** Each server-side cursor holds a dedicated
  connection but has a 5-minute idle TTL; `list_cursors()` makes
  the population visible.
- **NL→SQL caps.** `MCPG_NL2SQL_MAX_TOKENS` (default 2048, hard
  limit 16384) bounds per-call output.
- **Rate limiting** (`MCPG_RATE_LIMIT_ENABLED`). Token-bucket per
  tool with a separate quota for heavy tools.
- **Connection-pool ceiling** (`MCPG_POOL_MAX_SIZE`) bounds
  concurrent DB load.

### T4 — Credential disclosure

**Mitigation.**

- `Settings.__repr__` redacts the database password, replica
  passwords, HTTP auth token, and NL→SQL API key.
- Audit logging masks values whose key name matches a configurable
  case-insensitive regex (default: `password`, `passwd`, `secret`,
  `token`, `api[_-]?key`, `bearer`, `authorization`,
  `database_url`, `dsn`, `conninfo`; extend via
  `MCPG_AUDIT_REDACT_KEYS`). Walks nested dicts / lists / tuples,
  including result payloads (so a `RETURNING password` doesn't
  leak).
- String leaves are passed through the `obfuscate_password` helper
  so an embedded DSN credential nested anywhere is scrubbed.
- Connection errors are passed through the same obfuscator.

### T5 — Plaintext database connection

**Mitigation.**

- **TLS enforcement on startup.** MCPg refuses to start if
  `MCPG_DATABASE_URL` (or any entry in `MCPG_REPLICA_URLS`) points
  at a non-loopback host with `sslmode=disable | allow | prefer`
  (or unset — libpq's default is `prefer`, which falls back to
  plaintext on TLS failure).
- The DSN is parsed via `psycopg.conninfo.conninfo_to_dict`, so the
  check covers URI DSNs, keyword DSNs (`host=… sslmode=…`), and
  failover multi-host URIs (`postgresql://h1,h2/db`).
- DSNs without an explicit host are also refused — libpq could
  resolve `PGHOST` to a non-loopback default.
- Loopback hosts (`localhost`, `127.0.0.1`, `::1`) are exempt.
- Bypass: `MCPG_ALLOW_INSECURE_TLS=true` (explicit operator
  opt-out for dev / internal use).
- Replica-DSN errors identify the offending index
  (`MCPG_REPLICA_URLS[1]`) for fast diagnosis.

### T6 — Unauthenticated remote access (HTTP transports)

**Mitigation.**

- **IP allowlist.** `MCPG_HTTP_IP_ALLOWLIST` (comma-separated
  IPv4 / IPv6 / CIDR) gates the HTTP transport **before** auth.
  Requests from outside the allowlist are dropped with 403 without
  ever touching token comparison or JWT validation. The match is
  against the immediate connecting peer; `X-Forwarded-For` is
  deliberately **not** honoured (trusting a forwarded header
  without a verified upstream is a well-known spoofing vector).
  Deployments behind a reverse proxy must enforce the allowlist at
  the proxy layer, which composes naturally with the proxy's own
  auditing.
- **TLS at the transport.** `MCPG_HTTP_TLS_CERTFILE` /
  `MCPG_HTTP_TLS_KEYFILE` terminate TLS in MCPg directly (no
  external proxy needed). Both required or both unset — partial
  config is rejected at startup so a deployment can't silently
  serve plaintext.
- **Mutual TLS (mTLS).** `MCPG_HTTP_TLS_CA_CERTS` +
  `MCPG_HTTP_TLS_CLIENT_CERT_REQUIRED=true` require clients to
  present a valid cert chaining to the configured CA. Setting the
  flag without `CA_CERTS` is rejected — there'd be nothing to
  verify against.
- **Static bearer.** `MCPG_HTTP_AUTH_TOKEN` enforces
  `Authorization: Bearer <token>` with `hmac.compare_digest`
  constant-time comparison. `/metrics`, `/healthz`, `/readyz` are
  exempt by design.
- **OIDC JWT.** `MCPG_AUTH_MODE=oidc` swaps the static comparison
  for full JWT validation against the configured issuer's JWKS.
  Validates `iss` + `aud` + `exp` + `nbf` + signature with 30 s
  clock leeway. **Only asymmetric algorithms** (RS256/RS384/RS512
  + ES256/ES384/ES512) — HS-family is rejected by design to
  preserve the OIDC trust model.
- The OIDC discovery document and JWKS are fetched on first use
  and cached.

### T7 — Tenant-isolation bypass

**Mitigation.**

- **Per-request `SET LOCAL ROLE`.** MCPg supports a static
  `MCPG_DEFAULT_ROLE` and per-request overrides via the
  `X-MCPG-Role` HTTP header (or, with `MCPG_OIDC_ROLE_CLAIM`, the
  named JWT claim). Each query is wrapped in
  `BEGIN ... SET LOCAL ROLE "<role>" ... <stmt> ... COMMIT` so
  RLS policies keyed on `current_user` isolate tenants correctly
  from a single pooled connection.
- `MCPG_ALLOWED_ROLES` provides an allowlist — header / claim
  values not in the list are rejected with 403.
- Role names are identifier-validated before being inlined into
  `SET ROLE`.

### T8 — Lack of attribution / audit gaps

**Mitigation.**

- `AuditedFastMCP` records **every** tool invocation — name,
  redacted arguments, redacted result (for persisted entries), and
  outcome (success / error code / error message) — to the
  `mcpg.audit` logger.
- With `MCPG_AUDIT_PERSIST=true`, every `run_write` and `run_ddl`
  is also persisted to a `mcpg_audit.events` table for after-the-
  fact queryability via `list_audit_events`.
- For per-tenant attribution, combine `MCPG_DEFAULT_ROLE` /
  `X-MCPG-Role` with `MCPG_AUDIT_PERSIST=true` so each row in
  `mcpg_audit.events` carries the responsible role.

### T9a — Credentials in plaintext env / config files

**Mitigation.**

- **Secrets backends.** `MCPG_SECRETS_BACKEND` swaps the default
  env-var lookup (`env`) for one of `file`, `vault`, `aws`, or `gcp`:
  - `file` — JSON/YAML overlay from `MCPG_SECRETS_FILE_PATH`.
  - `vault` — HashiCorp Vault KV v2 (`MCPG_VAULT_ADDR`,
    `MCPG_VAULT_TOKEN`, optional `MCPG_VAULT_NAMESPACE` /
    `MCPG_VAULT_PATH_PREFIX` — default `secret/mcpg`).
  - `aws` — AWS Secrets Manager (optional `MCPG_AWS_SECRETS_PREFIX`;
    region + credentials from the standard AWS SDK chain).
  - `gcp` — GCP Secret Manager (`MCPG_GCP_PROJECT_ID` required;
    optional `MCPG_GCP_SECRETS_PREFIX`).
- Credentials are fetched lazily and live only in process memory —
  never written back to disk or environment.
- Auth-error surfaces are specific (`Forbidden` / `Unauthorized` for
  Vault, `AccessDenied` / `UnrecognizedClient` / `InvalidSignature` /
  `ExpiredToken` for AWS, `PermissionDenied` / `Unauthenticated` for
  GCP) so operators can tell a missing-grant problem from a missing-
  secret problem.

### T9b — Observability leakage

**Mitigation.**

- **OpenTelemetry.** With `MCPG_OTEL_ENABLED=true` (and the
  `mcpg[otel]` extra), MCPg emits one span per `call_tool`. The
  span carries `mcp.tool.name`, `mcp.tool.argument_count`, and
  outcome status — **argument values are deliberately not
  attached** so a span exporter (Jaeger / Tempo / Honeycomb / SaaS
  vendor) can't become a side-channel for credentials or PII.
- Audit redaction (T4) runs on the local logger path; the OTel
  path stays narrow for the same reason.

### T9 — Supply-chain compromise

**Mitigation.**

- CI runs `bandit` (SAST) and `pip-audit --strict` (PyPI advisory
  DB + OSV.dev) on every push. Vulnerable transitive dependencies
  block merge.
- Releases publish via PyPI **Trusted Publishing** (OIDC) — no
  long-lived API tokens stored as GitHub Action secrets.
- Production PyPI uploads gated on a maintainer reviewer approval
  in the `pypi` GitHub environment.
- Tags are GPG-signed; the publish workflow refuses to build if
  the tag's version doesn't match `pyproject.toml`'s `version`.

---

## Operator responsibilities (defence in depth)

MCPg is not a replacement for database-side security. Operators
should:

- **Use a least-privilege database role** — ideally one granted
  only the privileges the workload needs. MCPg's access-mode
  enforcement is a second line of defence, not the only one. A
  superuser DSN combined with
  `unrestricted + MCPG_ALLOW_DDL=true + MCPG_ALLOW_SHELL=true` is
  by-design root access.
- **Use a dedicated role per deployment** so audit logs and
  database logs can be correlated.
- **Enable TLS** on the database (`sslmode=require` / `verify-ca`
  / `verify-full`); MCPg refuses to start without it for remote
  hosts by default.
- **For multi-tenant data**, prefer the `SET ROLE` workflow
  (`MCPG_DEFAULT_ROLE` + `MCPG_ALLOWED_ROLES`, OIDC claim
  mapping); fall back to one MCPg instance per tenant only when
  the tenant boundary is at the database level.
- **Ship audit-logger records** somewhere durable. The default
  Python logger sends to stderr.
- **Rotate the static-bearer `MCPG_HTTP_AUTH_TOKEN`** on the
  cadence your environment requires.

---

## Multi-tenancy and Row-Level Security

PostgreSQL Row-Level Security (RLS) policies are evaluated against
the **connecting role** (and session settings). Without
per-request role switching, MCPg connects with a single database
role and pools those connections, so every agent request would
look like the same principal from the database's perspective —
and RLS keyed on `current_user` would not isolate tenants.

**MCPg's mitigation:** the `SET LOCAL ROLE` workflow described in
T7. Set `MCPG_DEFAULT_ROLE` for a static deployment, or wire HTTP
clients / OIDC to send a per-request role and pair with
`MCPG_ALLOWED_ROLES`. The tenant driver wraps every query in a
transaction with `SET LOCAL ROLE`, so RLS sees the correct
principal even on a shared pool.

When the tenant boundary is at the **database level** (separate
databases per tenant, not separate roles), run one MCPg instance
per tenant with a tenant-specific `MCPG_DATABASE_URL`.

---

## Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md) at the repo root for the
full reporting policy. Summary:

- Email `devopam@gmail.com` with the issue, impact, repro, and
  MCPg version (`mcpg --version`).
- You'll receive an acknowledgement within **3 business days**.
- 90-day coordinated-disclosure window; critical issues ship
  faster (typically within 14 days).
- Reporters are credited in the release notes unless they prefer
  otherwise.

**Do not** file public issues for security reports.

---

## See also

- [`security-hardening.md`](security-hardening.md) — living
  roadmap of shipped (✅) vs queued (⬜) hardening features.
- [`SECURITY.md`](../SECURITY.md) — vulnerability-reporting
  policy.
- [`architecture.md`](architecture.md) — the trust boundaries
  rendered at the code-module level.
- [`installation.md`](installation.md) — TLS-enforcement details +
  least-privilege role setup.
