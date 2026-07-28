# MCPg v0.6.12 — release notes

**Released:** 2026-07-28
**Tool surface:** **254** tools across 19 capability buckets (read-only
mode exposes a subset)
**Tests:** unit + integration suite green (PG 14 / 15 / 16 / 17 / 18 / 19
/ WarehousePG)
**Runtime:** Python 3.14

A **0.6.11 → 0.6.12** release that adds two tools — a long-running
analytical read path and a cache-freshness escape hatch — plus a
dependency security bump. Backward-compatible: no existing tool
signature changed, and both new tools default to the previous behaviour
being available (the analytical tool is on by default; the cache still
behaves exactly as before unless you ask for `fresh` data).

## New: `run_analytical_query` — long-running reads on an isolated pool

`run_select` is deliberately bounded to a short (~30 s) timeout on the
shared connection pool so an agent can't pin a connection with a runaway
query — which made genuine analytical queries (large aggregations,
multi-table joins, window functions) infeasible, and
`MCPG_STATEMENT_TIMEOUT_MS` didn't help (it moves Postgres's
`statement_timeout`, not the client-side asyncio cap).

The new tool runs a read-only SELECT through the **same** allowlist +
tenancy/RLS + read-only transaction as `run_select`, but on a
**dedicated connection pool** isolated from the main one, with an
**elevated, bounded** timeout — so a slow analytical query can never
starve the fast-path tools. A per-call `timeout_ms` (clamped to the
configured maximum) and optional `work_mem` are exposed, and a
`run_select` timeout now points the agent at the tool so it needn't
predict a query's duration up front.

Boot-time knobs: `MCPG_ENABLE_ANALYTICAL_QUERIES` (default `true` — the
authoritative off-switch; set `false` to withdraw the tool),
`MCPG_ANALYTICAL_TIMEOUT_MS` (default 120000, 2 min),
`MCPG_ANALYTICAL_MAX_TIMEOUT_MS` (600000, 10 min ceiling), and
`MCPG_ANALYTICAL_MAX_CONCURRENCY` (2 — the isolated pool size, which is
also the concurrency cap). Read-only and primary-database only for now.

## New: cache-freshness controls for out-of-band schema changes

MCPg's read cache is invalidated automatically by MCPg's own write/DDL
tools, but it could serve stale introspection/advisor results for up to
`MCPG_CACHE_TTL_SECONDS` (default 300 s) after a schema change made
*outside* MCPg — a direct `psql`/migration change, another connection,
or a second process. Re-running index/constraint validation after
altering a foreign key could return the pre-change answer. Two escape
hatches:

- a per-call **`fresh: bool = False`** argument on the
  introspection/advisor reads (`describe_table`, `list_indexes`,
  `list_constraints`, `list_foreign_keys`, `get_compact_schema`,
  `recommend_indexes`, `audit_database`) — bypasses the cache read,
  re-queries live, and refreshes the entry;
- a new **`clear_cache`** tool that flushes the whole result cache in one
  call.

## Security: `mcp` SDK bumped to ≥ 1.28.1

Raised the `mcp` SDK floor from `≥ 1.25.0` to `≥ 1.28.1` to clear three
advisories in the resolved `mcp` 1.27.1 (CVE-2026-52870,
CVE-2026-52869, CVE-2026-59950). No MCPg source change was required —
the per-request tenancy role path is unchanged in 1.28.1, and the full
unit + contract suite passes. `pip-audit --strict` on the resolved
runtime deps is clean.

## Also in this release

- **`run_analytical_query` hardening.** Query timeouts raise a typed
  `QueryTimeoutError` detected structurally across the full
  `__cause__`/`__context__` exception chain — covering both the
  client-side asyncio cap and the server-side `statement_timeout`
  (SQLSTATE 57014) — so the self-correcting hint fires reliably and
  locale-independently.
- **`pglast` 7.15 → 8.x** (the SQL-safety kernel's parser), tracking the
  PostgreSQL 18 grammar. No behaviour change — the full adversarial +
  fuzz + differential-parity SQL-kernel suites pass unchanged.

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.6.12   # or :latest
```

Or grab `mcpg-0.6.12.mcpb` from this release and double-click it into
Claude Desktop. No configuration changes required.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.12]` for the complete
itemised list.
