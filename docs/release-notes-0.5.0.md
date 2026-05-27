# MCPg v0.5.0 — release notes

**Released:** 2026-05-27
**Tool surface:** 74 → **107**  (+33)
**Tests:** 874 pass / 106 skipped
**CI:** PG 14 / 15 / 16 / 17 / 18

This release closes the **entire `docs/feature-shortlist.md`** — every
pick from Tier A, Tier B, and Tier C — plus a natural-language → SQL
helper that was in the "Defer for now" bucket. Six PRs landed on top
of v0.4.0:

| PR | Theme | Tools added |
|----|-------|-------------|
| #18 | Tier A: HTTP auth + Prometheus + TimescaleDB + pgvector + composites + advisors | +16 |
| #19 | Tier B: PII heuristic + N+1 detector + migration validation + `SET ROLE` multi-tenancy | +3 + runtime |
| #20 | Tier C: catalog readers + naming linter + FK cascade graph + parallel select + server-side cursors + RLS tester + test-data factory | +13 |
| #21 | NL → SQL via Anthropic / OpenAI / Gemini | +1 |

## Headlines

### NL → SQL (`translate_nl_to_sql`)

The headline new capability. Send a natural-language question; MCPg
gathers a compact schema brief (tables, columns, foreign keys), asks
the configured LLM provider to translate, parses the JSON response,
and — when `execute=true` — passes the generated SQL through the
existing `SafeSqlDriver` safety allowlist before running it.

```text
translate_nl_to_sql(question="how many orders shipped last week?",
                    schema="app",
                    execute=true)
```

Pluggable provider: `MCPG_NL2SQL_PROVIDER=anthropic|openai|gemini`.
HTTPS calls go through `httpx` directly — no SDK dependencies. The
OpenAI path accepts `MCPG_NL2SQL_BASE_URL` so it can target Ollama,
vLLM, LM Studio, OpenRouter, or any other OpenAI-compatible gateway.

Safety: hard 16384 max_tokens cap; identifier-validated schema /
table filter; writes / DDL / multi-statement input rejected at the
execution layer regardless of what the model produced.

### Per-request `SET ROLE` multi-tenancy

One MCPg process now serves N tenants from one connection pool.
Two paths:

* Static: `MCPG_DEFAULT_ROLE=app_tenant_42` applies to every query.
* Per-request: HTTP requests can send `X-MCPG-Role: <role>`; the
  middleware validates the identifier, checks the optional
  `MCPG_ALLOWED_ROLES` allowlist (403 if missing), and stashes the
  role in a `ContextVar`. The new `TenantSqlDriver` reads the var
  and wraps every query in `BEGIN ... SET LOCAL ROLE "<role>" ...`
  so the role auto-resets at txn end. No state leak into the pool.

The tenant middleware sits above the bearer-auth middleware in the
stack so unauthenticated requests can't reach the role parser.

### HTTP transport bearer-token auth + Prometheus `/metrics`

`MCPG_HTTP_AUTH_TOKEN` gates the streamable-http / sse transports.
Missing or wrong token → 401 with `WWW-Authenticate: Bearer
realm="mcpg"`. `/metrics`, `/healthz`, `/readyz` are exempt so
Prometheus / load balancer / k8s probes don't need the MCP token.

`/metrics` emits the standard text-exposition format (v0.0.4) with
three series:
* `mcpg_tool_calls_total{tool, status}` (counter)
* `mcpg_tool_duration_seconds_bucket{tool, le}` (histogram)
* `mcpg_tool_duration_seconds_sum/_count{tool}`

Zero runtime dependency — the format is rendered in-process.
`get_metrics_exposition` returns the same payload over MCP for stdio
deployments.

### Server-side cursors

Four tools — `open_cursor` / `fetch_cursor` / `close_cursor` /
`list_cursors` — let an agent page through millions of rows without
loading them all. Each open cursor holds a **dedicated** psycopg
connection (NOT a pool checkout) so N long-lived cursors can't
starve the pool other tools use. SQL validated by the same allowlist
as `run_select`; opened in a `READ ONLY` transaction.

Per-cursor `asyncio.Lock` serialises concurrent fetch / close on the
same cursor — psycopg AsyncConnection isn't safe for concurrent task
access. Hard cap of 16 concurrent cursors; default 5-minute idle TTL
with lazy sweep.

### TimescaleDB

Five new tools — read (`list_hypertables`, `list_chunks`) plus DDL
writes (`create_hypertable`, `add_compression_policy`,
`add_retention_policy`). Every interval / identifier is
allowlist-validated before being inlined into SQL. Each tool
degrades to `available=false` when the extension is missing.

### pgvector advanced search

* `hybrid_search` fuses vector + FTS rankings via reciprocal-rank
  fusion (RRF). Closes the biggest unmet need in agentic RAG: pure
  vector misses keyword / identifier hits, pure FTS misses semantic
  synonyms.
* `vector_range_search` finds every row within `max_distance` of a
  query vector (not top-k).
* `recommend_vector_quantization` flags `vector(N)` columns that
  could halve their storage on pgvector ≥ 0.7 by switching to
  `halfvec(N)`.

### Composite + advisor tools

* `summarize_table` — one-stop snapshot replacing 4-5 round trips
  (columns + PK/FK + indexes + storage + sample).
* `why_is_this_slow` — `EXPLAIN (FORMAT JSON)` + concurrent-query
  snapshot + blocking pairs + cache hit ratio + categorised
  suggestions, all without executing the query.
* `find_unused_objects` — `pg_stat_user_*` scan for zero-scan tables
  / indexes, with the context needed to decide drops.

### Tier-B + Tier-C surface (+17 tools)

* `find_sensitive_columns` — PII / secret heuristic across seven
  categories (credential / financial / contact / identifier / health
  / government_id / location).
* `detect_n_plus_one` — `pg_stat_statements` walker for ORM
  lazy-load loops.
* `validate_migration` — apply a candidate against a transient
  shadow with real-shape sample data; catches what a structural diff
  misses (NULL violations, CHECK failures, type narrowings, trigger
  errors).
* `lint_naming_conventions` — majority-style detection + index
  prefix rule.
* `generate_fk_cascade_graph` — Mermaid graph of blast-radius FK
  chains.
* `run_select_parallel` — concurrent fan-out with per-statement
  error isolation.
* `test_rls_for_role` — RLS debugging as a target role inside
  `READ ONLY` + `SET LOCAL ROLE`.
* `generate_test_data` — synthetic INSERT statements for seeding.
* `list_generated_columns`, `list_locks`, `find_blocking_chains`,
  `read_pg_stat_io` — catalog / runtime readers.

## New env vars

| Variable | Purpose |
|----------|---------|
| `MCPG_HTTP_AUTH_TOKEN` | Bearer token enforced on HTTP transports. |
| `MCPG_DEFAULT_ROLE` | Static PG role applied to every query. |
| `MCPG_ALLOWED_ROLES` | Comma-separated allowlist for `X-MCPG-Role` + default. |
| `MCPG_NL2SQL_PROVIDER` | One of `anthropic`, `openai`, `gemini`. |
| `MCPG_NL2SQL_API_KEY` | API key (or vendor-conventional fallback). |
| `MCPG_NL2SQL_MODEL` | Override the default model id. |
| `MCPG_NL2SQL_BASE_URL` | Self-hosted gateway for OpenAI-compatible. |
| `MCPG_NL2SQL_MAX_TOKENS` | Per-call budget (≤ 16384). |

## Backwards compatibility

* `Settings.__repr__` adds the new fields; the API-key field renders
  as `'set'` / `'unset'` and never leaks the actual value.
* `AppContext` gained a `cursor_manager` field. Direct constructors
  in your own tests need it. `create_server()` accepts an optional
  `cursor_manager=` arg the same way it does for `database=` and
  `listen_manager=`.
* The vendored `SqlDriver` is unchanged. `TenantSqlDriver` subclasses
  it; it's only instantiated when tenancy is configured, so the
  zero-overhead path is preserved for non-tenant deployments.

## Test plan

* `uv run pytest` — 874 passed / 106 skipped
* `uv run ruff check .` + `uv run ruff format --check .` — clean
* `uv run mypy src` — no issues in 50 source files
* CI matrix PG 14 / 15 / 16 / 17 / 18 — all green on the merge commits

## Acknowledgements

Gemini Code Assist reviewed every PR in this release and surfaced
several real issues that were fixed before merge — notably the
hybrid_search row-key bug, the TimescaleDB case-folding hazard, the
cursor concurrency hazard, the NL→SQL parse-non-dict crash, and the
brace-in-question format crash. Two findings (RLS bypass + multi-
statement result failure) were verified empirically and replied to
on-thread.
