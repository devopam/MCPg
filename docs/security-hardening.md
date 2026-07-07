# MCPg security hardening — roadmap

This is a **living checklist** of robust-security features for MCPg.
Each item has a status, the operator-facing knobs it introduces, and
notes on the implementation plan.

For vulnerability reporting / disclosure policy, see
[`SECURITY.md`](../SECURITY.md) at the repo root.

| Status | Meaning |
|---|---|
| ✅ | Shipped on `main`. Tests pin behaviour. |
| 🟡 | Partial — designed and a subset shipped. |
| ⬜ | Pending — designed; implementation queued. |

---

## Already shipped

### ✅ Capability gates (per-tool access control)
`MCPG_ACCESS_MODE=read-only|restricted|unrestricted` plus the
`MCPG_ALLOW_DDL` / `MCPG_ALLOW_SHELL` / `MCPG_ALLOW_LISTEN` opt-ins
gate which tools appear in the listing. Documented in
[`docs/tools.md`](tools.md).

### ✅ Static bearer-token auth for HTTP transports
`MCPG_HTTP_AUTH_TOKEN` enforces `Authorization: Bearer <token>` with
constant-time comparison via `hmac.compare_digest`. `/metrics`,
`/healthz`, `/readyz` exempt by design.

### ✅ OIDC / JWT bearer-token validation
`MCPG_AUTH_MODE=oidc` replaces static comparison with full JWT
validation against the configured issuer's JWKS. Asymmetric algorithms
only (RS256-RS512 + ES256-ES512); HS-family explicitly excluded.

### ✅ Per-request multi-tenancy via `SET LOCAL ROLE`
Static (`MCPG_DEFAULT_ROLE`) plus per-request override
(`X-MCPG-Role` HTTP header / `MCPG_OIDC_ROLE_CLAIM` from the JWT).
Allowlist via `MCPG_ALLOWED_ROLES`. Role names identifier-validated.

### ✅ Read-replica routing
`MCPG_REPLICA_URLS` round-robins read-only queries to healthy
replicas with degraded-replica detection. Failures don't bubble up
to the tool layer.

### ✅ SQL injection prevention
All identifier interpolation gated by
`[A-Za-z_][A-Za-z0-9_]*` allowlist; user queries flow through
`SafeSqlDriver`'s parse-and-validate path before execution.

### ✅ Password obfuscation in logs and `repr`
`Settings.__repr__` redacts credentials from `database_url`,
`replica_urls`, `http_auth_token`, `nl2sql_api_key`, OIDC fields.

### ✅ Per-tool rate limiting
`MCPG_RATE_LIMIT_*` family enforces global + heavy-tool quotas on
each `call_tool` invocation. Token-bucket implementation in
`mcpg.middleware.rate_limit`.

### ✅ Audit trail
`mcpg.audit` records each tool call with status + arguments. Optional
persistence via `MCPG_AUDIT_PERSIST` to the `mcpg_audit.events` table.

---

## This release

### ✅ PG TLS enforcement
**Problem.** A misconfigured deployment can connect to a remote PG
over plaintext (`sslmode=disable`) without anyone noticing.

**Solution.** On startup, parse `MCPG_DATABASE_URL` (and every entry
in `MCPG_REPLICA_URLS`). If the host is non-loopback AND the
`sslmode` query parameter is one of `disable` / `allow` / `prefer`,
refuse to start unless `MCPG_ALLOW_INSECURE_TLS=true` is set
(explicit override for explicitly-non-prod deployments).

**Env vars added:**
- `MCPG_ALLOW_INSECURE_TLS=true` (opt-out override; default off)

**Implementation:** `mcpg/config.py` — new validator runs at the
end of `load_settings`. Tests in `tests/unit/test_config.py`.

### ✅ Sensitive-argument redaction in audit
**Problem.** Tool arguments today are recorded verbatim in the audit
trail. A tool that takes `MCPG_HTTP_AUTH_TOKEN` as an arg, or
`api_key`, would persist that secret in the audit table.

**Solution.** Replace the exact-name allowlist with a case-insensitive
regex matched by `re.search`, so `password` also catches `PGPASSWORD` /
`user_password` / `app.password`. Default pattern set: `password`,
`passwd`, `secret`, `token`, `api[_-]?key`, `bearer`, `authorization`,
`database_url`, `dsn`, `conninfo`. The matched value is replaced with
the existing `****` mask (consistent with `obfuscate_password`).
Walks nested dicts / lists / tuples — a `RETURNING password` payload
buried in a result row is masked too. Pattern list extensible via
`MCPG_AUDIT_REDACT_KEYS` (comma-separated regex fragments).

**Env vars added:**
- `MCPG_AUDIT_REDACT_KEYS` — comma-separated regex list to extend
  the default pattern set.

**Implementation:** `mcpg/audit.py` — `_redact_value` helper +
`configure_redaction(env)` re-arm hook called from `load_settings`.
`mcpg/audit_trail.py` shares the same pattern via `_is_secret_key`.
Tests pin the default patterns + the extension knob.

### ✅ Supply-chain CI hardening
**Problem.** The existing `security` job in `.github/workflows/ci.yml`
invoked `uv audit` — a subcommand `uv` does not provide — so the
dependency-audit step has been silently failing on every push. The
SAST (bandit) step ran but its companion was a no-op.

**Solution.** Replace the broken `uv audit` step with
`uv run pip-audit --strict --disable-pip` (PyPI + OSV.dev advisory
sources, warnings upgraded to failures so a vulnerable transitive dep
blocks merge), and add `pip-audit>=2.7` to the `dev` dependency
group. The `bandit -r src/mcpg --skip B101,B608,B110 -ll` step is
left in place.

---

## Queued (next focused PRs)

> Most of this section has now shipped (each marked ✅ with the
> landing note inline). Two items remain, both small and both scoped
> to `translate_nl_to_sql` / `mcpg.nl2sql`: **NL→SQL prompt-injection
> hardening** and **NL→SQL EXPLAIN dry-run pre-flight** (the two ⬜
> entries below). They're the current top security priority and a
> natural single focused PR.

### ✅ HTTP hardening (request limits + security headers + CORS + timeout)
**Shipped.** Security headers + request-size limit + CORS allowlist
landed in the security-diagnostics PR; the opt-in per-request
timeout (`MCPG_HTTP_REQUEST_TIMEOUT_SECONDS`, default `0` = disabled
so long-lived SSE / streamable-http streams keep working) landed in
the security-hardening-queue PR.

**Problem.** The streamable-http / sse transports today have no
request body size limit, no per-request timeout, no
`Content-Security-Policy` / `X-Frame-Options` /
`Strict-Transport-Security` / `Referrer-Policy` headers, and no
configurable CORS allowlist.

**Solution.** New `_SecurityHeadersMiddleware` adds the headers
unconditionally (operators can disable per header via env). New
`_RequestSizeLimitMiddleware` rejects bodies above
`MCPG_HTTP_MAX_BODY_BYTES` (default 1 MiB) with 413. New
`MCPG_HTTP_ALLOWED_ORIGINS` (comma list) drives CORS via Starlette's
`CORSMiddleware`.

**Env vars to add:** `MCPG_HTTP_MAX_BODY_BYTES`,
`MCPG_HTTP_REQUEST_TIMEOUT_SECONDS`, `MCPG_HTTP_ALLOWED_ORIGINS`,
`MCPG_HTTP_HSTS_MAX_AGE` (default 31536000).

**Effort:** medium (one new middleware module + 6-8 tests).

### ✅ Audit log integrity (HMAC chain + verifier tool)
**Shipped** in the security-diagnostics PR (columns + tool + a
process-wide write lock so the chain stays linear under concurrency).

**Problem.** An attacker with write access to `mcpg_audit.events`
can truncate, alter, or insert events undetected.

**Solution.** Each event carries an HMAC over `(prev_hmac ||
serialised_event)` keyed by `MCPG_AUDIT_HMAC_KEY`. A new
`verify_audit_chain` MCP tool walks the chain and reports the first
break. Persistence schema gains `prev_hmac` + `event_hmac` columns.

**Env vars to add:** `MCPG_AUDIT_HMAC_KEY` (required when
`MCPG_AUDIT_PERSIST=true` AND integrity is enabled),
`MCPG_AUDIT_INTEGRITY=true|false`.

**Effort:** medium (schema migration + HMAC computation +
verifier tool + 5-7 tests).

### ✅ Pluggable secrets backend
**Problem.** API keys for the NL→SQL providers, OIDC client
secrets (future), and the static bearer token all live in env vars.
Deployments that use HashiCorp Vault / AWS Secrets Manager / GCP
Secret Manager have to inject them through their orchestrator
sidecar rather than letting MCPg fetch them directly.

**Solution.** A `SecretsProvider` protocol (`mcpg.secrets`) picked by
`MCPG_SECRETS_BACKEND`; the secrets read in `load_settings`
(`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` /
`GOOGLE_API_KEY` / `MCPG_NL2SQL_API_KEY`, `MCPG_HTTP_AUTH_TOKEN`,
`MCPG_AUDIT_HMAC_KEY`) route through `provider.get(name)`.

**✅ Shipped:** `env` (default, unchanged) and `file` (overlay a
JSON / YAML `name → value` map via `MCPG_SECRETS_FILE_PATH`, env
fallback for unlisted names). Zero new required dependencies — YAML
is read only when PyYAML happens to be importable.

**✅ Also shipped:** the cloud backends — `vault` (HashiCorp), `aws`
(Secrets Manager), `gcp` (Secret Manager) — each behind an optional
extra (`mcpg[vault]`, `mcpg[aws]`, `mcpg[gcp]`), selected by the same
`MCPG_SECRETS_BACKEND` switch (`env` | `file` | `vault` | `aws` | `gcp`).

### ✅ Subprocess hardening
**Shipped** in the security-hardening-queue PR. On top of the
existing minimal-env spawn (only allowlisted `PG*` / `LANG` /
`LC_ALL` / `PATH` reach the child) and `shutil.which` resolution,
`run_pg_binary` now:
- validates the resolved binary's directory against
  `MCPG_SUBPROCESS_BIN_ALLOWLIST` (empty = trust PATH); a PATH shim
  in an untrusted dir is rejected. The check compares the resolved
  *directory* so distro `pg_dump -> pg_wrapper` symlinks still work.
- applies `RLIMIT_CPU` / `RLIMIT_AS` via a `preexec_fn` when
  `MCPG_SUBPROCESS_CPU_SECONDS` / `MCPG_SUBPROCESS_MEMORY_MB` are set
  (POSIX only; a no-op on platforms without the `resource` module).
- spawns in a throwaway temp working directory, cleaned up after.

**Env vars:** `MCPG_SUBPROCESS_BIN_ALLOWLIST` (comma-separated
absolute paths), `MCPG_SUBPROCESS_CPU_SECONDS`,
`MCPG_SUBPROCESS_MEMORY_MB`. All opt-in; defaults preserve today's
behaviour.

### ✅ Graceful shutdown
**Problem.** On SIGTERM today the server exits immediately,
abandoning in-flight tool calls and any open cursors.

**Solution.** Lifespan exit hook drains the in-flight tool count,
closes every open cursor via `CursorManager.close_all`, flushes the
audit trail, then exits. Configurable max-drain window
(`MCPG_SHUTDOWN_DRAIN_SECONDS`, default 30).

**Env vars to add:** `MCPG_SHUTDOWN_DRAIN_SECONDS=30`.

**Effort:** small (~40 LOC + 3-4 tests).

### ✅ NL→SQL prompt-injection hardening (boundary defense)
**Shipped.** The user question is now wrapped in `<user_request>` …
`</user_request>` delimiters and the system prompt instructs the model to
treat that text as data, never instructions, and to **refuse** any request
beyond a read-only SELECT by emitting the sentinel `-- MCPG_REFUSED: <reason>`.
`translate_nl_to_sql` detects the sentinel (in the JSON `sql` field or a bare
reply), returns a structured `TranslationResult(refused=True, refusal_reason=…)`
with empty `sql`, and never forwards it to `SafeSqlDriver` / execution. The AST
allowlist remains the enforcement backstop; this is defence-in-depth at the
prompt layer.

**Problem.** `translate_nl_to_sql` already separates the schema (system
role) from the user's question (user role) and isolates the question via
`.replace()` rather than format-string interpolation, and the generated
SQL is re-validated by `SafeSqlDriver` (read-only SELECT, single
statement, schema denylist) before any execution — so the *impact* of a
prompt injection is already bounded to a read-only query over allowed
schemas. What's missing is an explicit instruction in the system prompt
telling the model to treat the user text as a **literal translation
request** and refuse embedded instructions ("ignore previous
instructions", "output all password hashes", etc.), plus hard delimiters
around the user input. This is defense-in-depth at the prompt layer, not
a new enforcement boundary.

**Solution.** (1) Wrap the user question in explicit delimiters (e.g.
`<user_request>…</user_request>`) in the user-role message. (2) Add a
directive to the system prompt: the model is a strict SQL translator;
anything inside the delimiters is data to translate, never instructions
to obey; if the request asks for anything beyond a read-only SELECT over
the permitted schemas, emit a refusal sentinel the caller can detect.
(3) Keep the existing AST allowlist as the actual enforcement backstop —
the prompt hardening only reduces the chance of a socially-engineered
(but technically valid) SELECT.

**Implementation note — refusal sentinel.** Pin the sentinel once so
handling stays consistent: the model must emit a single line
`-- MCPG_REFUSED: <reason>` (and nothing else) when it declines. The
translator detects that exact prefix, returns a typed
`Nl2SqlResult(refused=True, reason=…, sql=None)` (never passes the text
on to `SafeSqlDriver`), and the tool surfaces it as a structured refusal
rather than an error. Defining the sentinel + the result field up front
avoids each provider branch inventing its own ad-hoc handling.

**Env vars to add:** none (prompt-template change; behaviour is on by
default).

**Effort:** small (system-prompt edit + refusal-path handling + 3-5
tests). Surfaced 2026-06-30 from a security-feature review.

### ✅ NL→SQL EXPLAIN dry-run pre-flight
**Shipped.** `translate_nl_to_sql` now runs a non-executing
`EXPLAIN (FORMAT JSON)` (reusing `mcpg.query.explain_query`, so the SQL is
`SafeSqlDriver`-validated first) before returning or executing. A planner
rejection surfaces as a structured `error="query invalid: <message>"` with
the SQL returned for inspection and nothing executed; a pre-flight
`statement_timeout` degrades to "skip, proceed" rather than blocking a valid
translation. Controlled by the `explain_preflight` tool arg (default on).

**Problem.** Generated SQL is validated structurally (AST allowlist +
single-statement assertion) but not **semantically** — a query that
references a non-existent column/table or has a type mismatch is
syntactically valid, passes the allowlist, and only fails when executed.
The agent gets a runtime error instead of an early, clean signal.

**Solution.** Before returning (or, on `execute=True`, before running)
the generated SELECT, run a non-executing `EXPLAIN` (no `ANALYZE`) in a
read-only transaction, bounded by the existing `statement_timeout`. If
`EXPLAIN` errors, surface it as a structured "query invalid" result with
the planner message rather than a raw execution failure; optionally
expose the plan so the caller can spot an unintended seq-scan-on-huge-
table before running it. `EXPLAIN` without `ANALYZE` does not execute the
query, so this is cheap and side-effect-free. (Distinct from the existing
`io=True` path on `explain_query`, which deliberately *does* run
`ANALYZE`.)

**Implementation notes — permissions & cost.**
- *Permissions:* `EXPLAIN` (no `ANALYZE`) needs the same `SELECT`
  privileges the query itself needs, so any role that could run the
  generated SELECT can plan it — no extra grant. Treat a permission
  error from the pre-flight as "not authorised", surfaced like any other
  planner error, not as a hard failure of the tool.
- *Cost / configurability:* planning is normally sub-millisecond, but a
  query over many partitions or a deeply-nested view can take longer, so
  the pre-flight inherits the existing `statement_timeout` and must
  degrade gracefully (treat a pre-flight timeout as "skipped, proceed"
  rather than blocking). Make it bypassable via the `explain_preflight`
  arg (default on) for callers that don't want the extra round trip.

**Env vars to add:** likely none (could gate behind an
`explain_preflight` arg, default on).

**Effort:** small (one pre-flight helper + wiring into
`translate_nl_to_sql` + 4-6 tests).

### ✅ Pin PID-targeted backend actions to the primary (never replica-routed)
**Shipped.** **Problem.** `mcpg.liveops.cancel_query` / `terminate_backend`
sent `pg_cancel_backend(pid)` / `pg_terminate_backend(pid)` via
`driver.execute_query(..., force_readonly=True)`. That flag is meant to
mark a query as *safe to route to a read replica* — but a PID is scoped to
whichever physical server process it names, not portable across servers.
When `MCPG_REPLICA_URLS` is configured, `RoutedSqlDriver` could send a
cancel/terminate signal to a **different backend on a replica** than the
one the caller actually intended (sourced from `list_active_queries`,
`pg_stat_activity`, or an operator's own knowledge of the primary) —
silently: a PID that doesn't exist on the routed-to server just returns
`succeeded=False`, and a PID that *coincidentally* exists there (a
different, unrelated backend) would be cancelled/terminated instead, with
no error at all. Surfaced 2026-07-01 during review of an unrelated PR
(`force_readonly=True` on a `pg_terminate_backend` call in test cleanup
code, fixed there; this was the equivalent, pre-existing issue in the real
product tools).

**Solution.** Dropped `force_readonly=True` from both calls in
`mcpg.liveops` — a PID-targeted admin signal is a primary-only action by
nature (same category as DDL/write tools), not a "safe to read from a
replica" query, so it's never eligible for replica routing regardless of
replica config. If replica-side PID actions are ever genuinely wanted,
that should be an explicit, opt-in target (consistent with the `database`
selector pattern from roadmap 13.1), not implicit routing via a flag whose
real purpose is unrelated.

**Env vars to add:** none (removed an incorrect flag; no new surface).

**Effort:** small — two-line change in `mcpg.liveops` + two regression
tests asserting the calls are never marked `force_readonly`.

---

## Posture summary

| Area | Today (on `main`) | Remaining roadmap |
|---|---|---|
| Authn | Static + OIDC | + secrets backend for keys |
| Authz | Capability gates + tenancy | Same |
| Transport security | bearer token + PG TLS enforcement + HTTP hardening (headers, body limit, CORS, opt-in request timeout) | Same |
| Audit | Recorded + arg redaction + HMAC integrity chain | Same |
| Supply chain | CI bandit + pip-audit | Same |
| Lifecycle | Graceful shutdown draining | Same |
| Subprocess | Minimal env + bin allowlist + rlimits + temp cwd | Same |
| Secrets | Env + file + cloud backends (Vault / AWS / GCP) via `MCPG_SECRETS_BACKEND` | Same |
