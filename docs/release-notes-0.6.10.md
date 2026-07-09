# MCPg v0.6.10 — release notes

**Released:** 2026-07-09
**Tool surface:** **252** tools across 19 capability buckets (read-only
mode exposes a subset)
**Tests:** unit + integration suite green (PG 14 / 15 / 16 / 17 / 18 / 19
/ WarehousePG)
**Runtime:** Python 3.14

A **patch bump (0.6.9 → 0.6.10)** headlined by a first-party SQL-safety
kernel and a batch of correctness fixes — most of them found by an
adversarial audit of the multi-database, caching, and advisor code.
Backward-compatible — no tool signatures changed.

## Headline: the SQL-safety kernel is now first-party

MCPg's SQL-safety core (the `pglast` parse + default-deny AST allowlist,
the connection pool, and the query driver) was previously **vendored**
from `crystaldba/postgres-mcp`. It has been **re-authored as a
first-party `mcpg.sql` package** — `allowlist.py` (the permitted
statement / node / function / extension policy, as data), `safety.py`
(`SafeSqlDriver`), and `driver.py` (`SqlDriver` / `DbConnPool`). Behaviour
is identical (proven by a differential parity harness with zero
divergence, the ported adversarial suite, a fuzz pass, and a security
review), and the kernel is now inside the same coverage / `mypy --strict`
/ `ruff` / `bandit` gates as the rest of MCPg. **No vendored runtime code
ships any more.** (Roadmap 18.1; supersedes ADR-0001 with ADR-0007.)

## Correctness fixes

- **Multi-database reads no longer bleed across databases.** With
  `MCPG_SECONDARY_DATABASE_URLS` configured, the shared read cache keyed
  entries without the target `database`, so a cached read against one
  database could be served for another — most visibly, `audit_database`
  against a secondary returned the primary's report. The database selector
  is now part of the cache key across all 70 read tools, and the Redis
  backend is additionally namespaced by physical-database identity so a
  cache shared across a mixed-database fleet can't collide.
- **`inner_product` vector recall advisors were reporting ~0 recall.**
  `vector_recall_at_k` / `recommend_hnsw_ef_search` /
  `recommend_ivfflat_probes` mixed pgvector's `<#>` operator (negated
  inner product) with the `inner_product()` function (raw) in opposite
  sort directions, collapsing recall for `metric="inner_product"` and
  wrongly advising an index rebuild. `l2` / `cosine` were always correct.
- **`recommend_index_drops` no longer flags covering indexes.** An index
  served by index-only scans has `idx_tup_fetch == 0` yet is doing real
  work; it is no longer mistaken for a droppable existence-check index.
- **`open_cursor` respects `MCPG_MAX_OPEN_CURSORS` under concurrency** —
  the open path no longer races its own cap check.
- **WarehousePG `check_segment_health`** no longer false-alarms every
  segment as "out of sync" on a mirrorless cluster.

## Developer experience

- **The primary database is addressable by its real name.** Previously the
  primary answered only to the generic id `"primary"` while secondaries
  used their real names, so `database="lookup"` errored even though
  `lookup` *is* the primary. `list_databases` and the routing now advertise
  the primary under its real database name (from `MCPG_DATABASE_URL`);
  `"primary"` and omitting the argument stay valid aliases.
- **Install docs now carry per-OS command blocks** (Linux/macOS · Windows
  PowerShell · Command Prompt), and the `docker run` examples name the
  container (`--name mcpg`).

## Also new

- **Three more built-in NL→SQL providers (now 22):** GLM (Zhipu / Z.ai),
  Doubao (ByteDance / Volcengine Ark), and Sakana Fugu — each a one-line
  entry in the provider registry, base URLs and key env vars verified
  against the vendors' own docs.
- **NL→SQL prompt-injection hardening + EXPLAIN dry-run pre-flight.**
  `translate_nl_to_sql` wraps the user request in delimiters and refuses
  anything beyond a read-only SELECT, and runs a non-executing `EXPLAIN`
  to catch structurally-valid-but-semantically-broken SQL before it runs
  (toggle via `explain_preflight`).

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.6.10   # or :latest
```

Or grab `mcpg-0.6.10.mcpb` from this release and double-click it into
Claude Desktop. No configuration changes required.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.10]` for the complete
itemised list.
