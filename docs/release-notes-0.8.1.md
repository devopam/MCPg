# MCPg v0.8.1 — release notes

**Released:** 2026-09-01
**Tool surface:** **254** tools (256 all-flags-on maximal with
`MCPG_DYNAMIC_SESSION_INTENT` enabled) — unchanged from 0.8.0. This release
adds no new tools; every `FBT` (boolean-trap) fix below changed a Python
calling convention, never a tool's registered name, schema, or count.
**Tests:** 2950 passed, 3 skipped (`tests/unit`), plus 51 passed
(`tests/contract`, zero snapshot drift), on this exact commit ahead of the
tag; the full `ci.yml` matrix (lint, mypy, security audit, PG 14–19 +
WarehousePG integration) passed on GitHub Actions.
**Runtime:** Python 3.12–3.14 (`requires-python >=3.12`; CI/mypy target
3.14)

A **0.8.0 → 0.8.1** release: remediation of every finding from two audit
runs (the `python-code-review` and `project-incubation` skills, run
2026-08-25) — a common exception base, a security-relevant defaults
review, resilience around external calls, and a full repo-wide lint
sweep. **It ships two behavior-tightening defaults** (below) that are
genuinely breaking for an unconfigured deployment, even though this is a
patch-version release; read the upgrade-impact section if you run the HTTP
transport.

## ⚠️ Breaking changes (read this if you run the HTTP transport)

1. **HTTP transport now fails closed without auth.** Previously it started
   anyway and logged a warning when neither `MCPG_HTTP_AUTH_TOKEN` nor
   `MCPG_AUTH_MODE=oidc` was configured; it now raises `ConfigError` and
   refuses to start.
   *Migration:* configure auth (`MCPG_HTTP_AUTH_TOKEN=…` or
   `MCPG_AUTH_MODE=oidc`), **or** set `MCPG_HTTP_ALLOW_UNAUTHENTICATED=true`
   to opt out explicitly — which is loudly logged on every start. The
   default `stdio` transport is unaffected.
2. **Rate limiting now defaults to enabled** (`MCPG_RATE_LIMIT_ENABLED`,
   `false` → `true`). *Migration:* operators who want the previous
   unlimited behavior must set `MCPG_RATE_LIMIT_ENABLED=false` explicitly.

## Security fix: SQL injection in graph tools

`describe_graph` and `generate_graph_diagram` read Apache AGE label names
back from the catalog and interpolated them unescaped into both generated
SQL and generated Mermaid diagram text. A label planted by a prior
`run_cypher` write could carry attacker-controlled SQL into a later
re-interpolation. Both sinks are now identifier-validated (aborting rather
than silently skipping), matching the existing `graph_projection`
precedent; both tools' backing queries also now run `force_readonly=True`.

## New: resilience around every external call

- **Circuit breaker** (5 failures / 30s) around each NL→SQL provider's
  `complete()` and the OIDC discovery-document fetch — a degraded
  vendor/IdP fails fast instead of every caller separately paying the full
  request timeout.
- **Retry with backoff + jitter** (3 attempts, ~0.1–2s), layered *inside*
  the breaker, around the same calls — a single dropped connection or
  transient 5xx is retried transparently.
- NL→SQL providers and the OIDC verifier now reuse a persistent
  `httpx.AsyncClient` instead of handshaking fresh per call.

## New: `mcpg.errors.MCPgError`

A common base class all 65 domain exceptions (`ConfigError`,
`DatabaseError`, `CursorError`, and 62 others across 64 modules) now
subclass — catch "any MCPg-internal error" with one type. No exception's
name, message, or call sites changed; `TenancyError`/`DynamicIntentError`
keep their `ValueError` base too.

## Also added

- `/readyz` readiness probe on the HTTP transport, distinct from
  `/healthz`'s liveness-only check.
- Optional `TrustedHostMiddleware` via `MCPG_HTTP_TRUSTED_HOSTS`, off by
  default.
- HSTS `max-age` default bumped to 2 years (OWASP current guidance).
- `TranslationResult` (NL→SQL) now records `schema_context` — the schema
  evidence actually sent to the model — for provenance tracing.
- `py.typed` marker (the `Typing :: Typed` classifier was previously
  unbacked).

## Ruff sweep: 418 violations fixed, 7 new categories enforced

`pyproject.toml`'s lint `select` list now includes `C90`, `ASYNC`, `C4`,
`SIM`, `PTH`, `PYI`, and `FBT` — `ruff check .` is clean against all of
them, not just spot-checked. The substantive fix inside this sweep: a
security-relevant `force_readonly` flag on the SQL-safety kernel's
`execute_query` family is now keyword-only everywhere, so it can no
longer be passed in the wrong positional slot. No public MCP tool
signature or contract snapshot moved. Full breakdown in
[`CHANGELOG.md`](../CHANGELOG.md) `[0.8.1]`.

## Also fixed

- **`run_select`/`run_select_tuned`** now bound the *fetch* itself
  (`fetchmany`) instead of materializing the whole result set before
  truncating. Partial mitigation — see the follow-ups below.
- **`auto-merge-bot-prs.yml`** could never actually merge a bot PR: its
  own "wait for status checks" polling loop counted its own
  still-running check run in the total, guaranteeing a timeout. Replaced
  with native `gh pr merge --squash --auto`.
- Seven silent `except: pass` sites now log; tracebacks preserved at 23
  error-logging call sites.
- `license-files` no longer points at the removed vendored SQL kernel;
  `hatchling>=1.26` pinned.
- A broken `pg_isready`-based readiness probe on the WarehousePG CI lane
  (wrong binary for that image) was burning ~6 minutes per run; swapped
  to `psql`.

## Tracked, not fixed in this release

- **Bounded fetch is a partial mitigation** — `psycopg`'s client-side
  cursor still buffers the full result into libpq during `execute()`; a
  true wire-level bound needs a server-side cursor or an injected
  `LIMIT`. The original OOM finding stays open.
- **The new `RedactionFilter` doesn't cover exception tracebacks** —
  `formatException` bypasses it. This release raised `exc_info=True`
  sites in `src/mcpg` from 3 to 28, including 6 new sites in `cache.py`
  logging Redis errors; `obfuscate_password`'s pattern doesn't cover
  `redis://` URLs either. Follow-up: redact at the formatter level and
  extend the URL pattern.
- **`B608` (SQL-injection lint) remains globally skipped in bandit.** The
  sweep that found the vulnerability above stopped there by design; ~60
  of the original 95 hits were never individually verified, and the same
  unvalidated-catalog-identifier pattern is confirmed to persist at 4
  more sites. Follow-up: finish the narrowing and consolidate identifier
  validation into one shared helper.
- **`/readyz` has no OIDC/JWKS-readiness gate**, by design — gating on
  "JWKS ever cached" would deadlock a fresh OIDC instance out of rotation
  forever.
- `policy.PermissionError` shadows the Python builtin (pre-existing,
  verified inert — nothing imports or catches it).

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.8.1   # or :latest
```

Or grab `mcpg-0.8.1.mcpb` from this release and double-click it into
Claude Desktop.

If you run the HTTP transport, read the breaking-changes section above
before upgrading.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.8.1]` for the complete
itemised list.
