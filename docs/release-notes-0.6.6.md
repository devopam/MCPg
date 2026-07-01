# MCPg v0.6.6 — release notes

**Released:** 2026-07-01
**Tool surface:** **252** tools across 19 capability buckets
**Tests:** 2623 pass (PG 14 / 15 / 16 / 17 / 18 / 19 / WarehousePG)
**CI:** PG 14-19 + a now-fully-passing WarehousePG lane

This is a **patch-level bump (0.6.5 → 0.6.6)**. It completes the entire
feature roadmap tracked in `docs/feature-shortlist.md` — six new
read-capable tools spanning live-ops, vector retrieval, and graph
projection — plus a set of real reliability fixes surfaced by finally
getting the WarehousePG CI lane to run against a genuine server. Every
addition is backward-compatible and additive.

## New tools

**Live database operations**
- **`analyze_table_bloat`** — per-table *and* per-index estimated bloat %
  (catalog estimate by default; precise `pgstattuple`/`pgstatindex` mode
  when installed), sorted worst-first, so VACUUM/REPACK can be targeted
  precisely instead of guessed at.
- **`dry_run_ddl`** — runs a proposed `ALTER TABLE` / non-concurrent
  `CREATE INDEX` under a tight `lock_timeout`, captures the lock mode
  held, wall-clock duration, and WAL bytes generated, then **always rolls
  back**. `CREATE INDEX CONCURRENTLY` and other non-transactional DDL are
  rejected up front as ineligible, since they can't run inside the
  wrapping transaction.
- **`run_select_tuned`** — runs a read-only SELECT with an elevated,
  bounded `work_mem` (hard-capped at 2 GiB) scoped to that single
  statement, for analytical queries that would otherwise spill to disk.

**Retrieval & vector**
- **`retrieve_with_context`** — "one-shot RAG": a pgvector k-NN search
  that also expands each hit one hop along foreign keys (parent + child
  rows), packing the match and its relational context into a single
  response.
- **`recommend_ivfflat_probes`** — the IVFFlat counterpart to the
  existing `recommend_hnsw_ef_search`: sweeps `probes`, measures
  recall@k and latency, and recommends the smallest value clearing a
  target recall.

**Graph**
- **`generate_graph_projection`** — projects a relational schema into an
  Apache AGE property graph by generating the openCypher `CREATE`/`MERGE`
  statements (rows→vertices, FKs→edges) **for review, never executed** —
  the same pattern as `generate_test_data` and `recommend_redistribute`.

## Enhancements

- **`audit_database`** now folds in sequence-exhaustion and
  `postgresql.conf`-sanity checks as two additional scorecard categories,
  so the comprehensive scan surfaces at-risk sequences and dangerous GUCs
  directly — no separate `audit_sequences`/`audit_settings` call needed.
- **`dump_database`** gains an optional `schemas` parameter to scope a
  `pg_dump` to specific schemas instead of the whole database. Purely
  additive — omitting it is byte-for-byte unchanged.

## Reliability fixes

- **The `warehousepg-latest` CI lane is now genuinely green** — for the
  first time. It had been failing since the day it was added: the pinned
  image never existed on Docker Hub, so the build failed before a single
  test ever ran, silently, for every PR. Repointed at a real published
  image, corrected its connection wiring, and fixed three real
  Greenplum/WarehousePG dialect incompatibilities the working lane then
  surfaced (distribution-key-aware uniqueness constraints, a PG13+-only
  `DROP DATABASE` flag, a default-schema collision in dump/restore).
- **`cancel_query` / `terminate_backend` could target the wrong server.**
  Both were marked "safe to route to a read replica," but a PID names a
  process on one specific physical server — with `MCPG_REPLICA_URLS`
  configured, the signal could silently miss or hit the wrong backend.
  Fixed: these are primary-only actions now, always.
- **The MCP Registry publish step had been silently failing since
  0.6.1** — the version it submitted was never bumped past the original
  registry-launch value, so every later release was rejected as a
  duplicate while PyPI/GHCR/GitHub Releases kept succeeding unnoticed.
  It now derives the version from the release tag automatically.

## Upgrade

```bash
pip install --upgrade mcpg   # once published to PyPI
docker pull ghcr.io/devopam/mcpg:0.6.6   # or :latest
```

No configuration changes required.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.6]` for the complete
itemised list.
