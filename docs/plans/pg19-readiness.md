# PostgreSQL 19 readiness

Tracking document for MCPg's PG 19 readiness work. Spawned by issue #120.

## Compatibility principle — no deprecations

**Adding PG 19 surface never removes existing surface.** Every tool that ships
today must keep working on PG 14-18, and a user upgrading their database to
PG 19 must keep every tool they relied on at PG 18. This is a hard rule —
not a soft preference — and applies to every PR in the Phase 3 plan below.

Concretely:

- **AGE-style `graph_operations` bucket stays.** SQL/PGQ (`property_graph_queries`
  bucket) ships alongside it. Agents pick by `get_pgq_status` — never forced.
- **pg_repack shell-out path stays** even after the in-server `REPACK` tool
  lands. PG ≤ 18 users continue to use pg_repack; PG 19 users can choose.
- **Existing advisor heuristics stay.** Skip-scan-aware ranking adds a new
  ``reason`` code (`pg19_skip_scan_candidate`) when PG 19 is detected; the
  existing reason codes remain valid.
- **`pg_get_acl()` upgrade in `list_grants` is opt-in via version detection**
  — the PG ≤ 18 catalog-walking query is kept as a fallback in the same
  function, not deleted. The result shape doesn't change; only the SQL
  underneath does.
- **Schema migrations are additive.** New columns are added with safe
  defaults; never `DROP COLUMN`, never rename. Existing snapshot rows stay
  comparable across versions.
- **Tool naming.** New tools take new names (`run_pgq` next to `run_cypher`).
  We never silently re-bind an existing tool name to a new behaviour.

When a PG 19 feature *would* obsolete an existing surface (e.g. SQL/PGQ
vs AGE-style Cypher), the right answer is **coexist** until a separate
deprecation conversation happens with telemetry behind it. That
conversation is not part of the PG 19 readiness work and would land
behind its own SemVer-major release.

The contract test at `tests/contract/test_tool_surface_snapshot.py` is the
operational guard: any tool removal trips the contract test, so a PG 19 PR
that accidentally deletes a tool fails CI.

## Current status

| Phase | Status | Notes |
|---|---|---|
| 1. CI matrix + compatibility surface | ✅ **Shipped** | PG 19 beta runs as an experimental (`continue-on-error`) matrix entry; pgvector built from source via `.github/ci-postgres-pg19.Dockerfile`. A WarehousePG (MPP) characterisation lane landed alongside. PostGIS still deferred until an apt package is published. |
| 2. Feature audit | ✅ **Done** | The Beta 1 sweep below is complete — every domain triaged through the product-owner lens. |
| 3. Incremental landing | ⏳ **Largely shipped** | Many tool families have landed: SQL/PGQ property graphs (`run_pgq`, `create_property_graph`, …), in-server `REPACK` (`repack_table`), skip-scan advisor (`recommend_skip_scan_indexes`), `WAIT FOR LSN` read-your-writes (`wait_for_lsn`), online data-checksum + on-demand logical-replication toggles, DDL introspection (`get_role_ddl` / `get_database_ddl` / `get_tablespace_ddl` / `validate_check_constraint`), partition `MERGE` / `SPLIT`, lock + recovery stats, and async-I/O coverage. See the [tool index](../tools.md#tool-index-252-tools) for the shipped surface and [feature-shortlist.md](../feature-shortlist.md) for the remaining items (tracked to GA). |

## Phase 1 — what landed

- `.github/workflows/ci.yml`: PG 19 added to the test matrix with `continue-on-error` (non-blocking until GA).
- `.github/ci-postgres-pg19.Dockerfile`: standalone Dockerfile that builds pgvector v0.8.0 from source on top of `postgres:19beta1`. Drop and route PG 19 back to the standard Dockerfile once pgvector ships a `pg19` image tag.
- `pyproject.toml` / `README.md`: PG version range updated.
- This document.

PostGIS is intentionally omitted from the PG 19 image — no `postgresql-19-postgis-3` apt package is published yet. Tests that require PostGIS will fail under PG 19; failures are non-blocking via the `continue-on-error` matrix entry.

## Phase 2 — comprehensive Beta 1 sweep (PO lens)

Re-swept against the official Beta 1 release announcement. Organised by domain, then re-prioritised through a product-owner lens — personas, business value, demo-ability, marketing surface, and strategic moat — rather than implementation cost alone.

### Personas this work serves

| Persona | Day-to-day | What they need from MCPg |
|---|---|---|
| **Dana the DBA** | Runs production PG clusters; on-call for incidents | Operational visibility, advisors, runbook automation, safe-by-default DDL |
| **Ari the App developer** | Building product features against PG | Schema introspection, NL→SQL, ORM codegen, validation tools |
| **Riya the RAG engineer** | Tunes retrieval pipelines and embedding workflows | Vector ops, telemetry, rerank analytics, advisor recommendations |
| **Sam the SRE** | Reliability / observability / cost | Health checks, alerting hooks, online maintenance, cost-aware advisors |
| **Aiden the AI agent** | LLM acting on behalf of any of the above | Discoverable, well-described, safe tool surface |

### Inventory — every Beta 1 feature, mapped

Reference: <https://www.postgresql.org/about/news/postgresql-19-beta-1-released-3313/>.

#### 1. SQL standards & developer experience

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **SQL/PGQ — property graph queries** (`GRAPH_TABLE`, `MATCH`) | "As Ari/Riya, I want to run Cypher-like graph queries in SQL so I don't need an external graph DB" | Ari, Riya, Aiden | New module `mcpg.pgq`; coexists with the existing AGE-style `graph_operations` bucket. Big strategic decision: replace AGE? offer both? | **Yes — huge.** "Cypher in SQL" is a release-blog headline. |
| **GROUP BY ALL** | "As Ari, I want SQL ergonomics that match Snowflake/DuckDB" | Ari, Aiden | `nl2sql.translate_nl_to_sql` (the translator can emit it); `run_select` accepts it natively (no MCPg change). | Low |
| **Temporal UPDATE/DELETE FOR PORTION OF** | "As Ari, I want SQL-standard temporal updates" | Ari | `run_write` allowlist check; characterisation test | Medium |
| **INSERT … ON CONFLICT DO SELECT** | "As Ari, I want upsert to return the conflicting row" | Ari | `run_write` shape change; new tool `upsert_select_returning` (optional) | Medium |
| **JSONPath string functions** (`lower`, `upper`, `initcap`, `replace`, `split_part`, `trim`) | "As Ari/Riya, I want richer jsonpath" | Ari, Riya | None directly; doc update on jsonpath helpers | Low |
| **Random date/timestamp generation** | "As Ari, I want one-call test data" | Ari | `mcpg.test_data.generate_test_data` extension | Low |

#### 2. Performance & query execution

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **Async I/O scaling** (`io_min_workers`, `io_max_workers`, auto-scale) | "As Dana/Sam, I want guidance on the new AIO tuning knobs" | Dana, Sam | **New advisor** `recommend_io_method` + extend `read_pg_stat_io` with new columns; `EXPLAIN ANALYZE (IO)` capture | **Yes — headline.** AIO is THE PG 19 story. |
| **Eager aggregation** (`enable_eager_aggregate` GUC) | "As Dana, I want to know if this helps my workload" | Dana | Extend `analyze_query_plan` to surface the new node type | Medium |
| **Anti-join optimizations** | "As Dana, I want my anti-join queries faster" | Dana | Planner-only; characterisation test | Low |
| **Incremental sort expansion** | Planner-only | Dana | Characterisation test | Low |
| **Foreign-key insert performance (2x)** | "As Ari, I get faster bulk inserts" | Ari | None | Low |
| **Parallel sequential scans (faster)** | Planner-only | Dana | None | Low |
| **LISTEN/NOTIFY scalability** | "As Ari/Sam, I want a more responsive event surface" | Ari, Sam | `mcpg.listen.poll_notifications` characterisation test | Low |

#### 3. Partitioning

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **MERGE PARTITIONS** | "As Dana, I want to consolidate partitions without rewriting data" | Dana | **New tool** `merge_partitions`; integrate with `partman_*` lifecycle | **Yes — strong.** |
| **SPLIT PARTITIONS** | "As Dana, I want to split a hot partition without downtime" | Dana | **New tool** `split_partition`; integrate with `partman_*` lifecycle | **Yes — strong.** |

#### 4. Vacuum & maintenance

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **REPACK / REPACK CONCURRENTLY** | "As Dana, I want a built-in replacement for pg_repack with online rebuild" | Dana, Sam | **New tool** `repack_table` (blocking + concurrent variants); deprecate the pg_repack shell-out path | **Yes — huge.** This is a top-3 PG 19 win for ops teams. |
| **Parallel autovacuum** (`autovacuum_max_parallel_workers`) | "As Dana, I want sane defaults for the new parallel autovacuum knobs" | Dana | Extend `run_advisors` with a parallel-autovacuum recommendation | Medium |
| **Autovacuum scoring system** | "As Dana, I want to know which tables autovacuum will prioritise" | Dana | New read tool `read_autovacuum_priority` | Medium |
| **Visibility marking strategy** | Internal optimisation | — | None | Low |

#### 5. Replication & federation

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **Sequence replication** | "As Ari/Dana, I want my logical-replica sequences to stay in sync" | Ari, Dana | `mcpg.replication.list_sequences_replicated` (new) | Medium |
| **CREATE PUBLICATION … EXCEPT** | "As Dana, I want to publish everything except one table" | Dana | Extend `create_publication`-style tool (does not exist yet); land alongside | Medium |
| **CREATE SUBSCRIPTION … SERVER** | "As Dana, I want subscriptions to ride foreign-server definitions" | Dana | Same as above | Medium |
| **On-demand logical replication** (no restart) | "As Dana, I want to turn on logical replication without a restart" | Dana, Sam | **New tool** `enable_logical_replication_on_demand` | **Yes — strong.** Avoiding restarts is a chart-topping DBA complaint. |
| **effective_wal_level** (preset) | "As Dana, I want to know if my cluster is actually at the WAL level I asked for" | Dana | Extend `get_server_info` to include this | Low |
| **WAIT FOR LSN** | "As Ari, I want read-your-writes consistency in a hot-standby read pool" | Ari, Sam | **New tool** `wait_for_lsn`; integrate with `read_replica_lag` advisor | **Yes — strong.** RYW consistency is a hard problem; making it a one-call MCP tool is differentiation. |
| **postgres_fdw pushdowns** (array ops, statistics) | "As Ari, I want better cross-cluster query perf" | Ari | `list_foreign_data_wrappers` doc update; characterisation test | Low |

#### 6. Security & auth

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **Server-side SNI** (`pg_hosts.conf`) | "As Sam, I want hostname-routed TLS certs" | Sam, Dana | Doc only; `verify_connection_encryption` can surface SNI status | Low |
| **Password expiration warnings** | "As Dana, I want to nudge users before their password expires" | Dana, Sam | **New tool** `list_password_expirations` + extend `audit_database` | Medium |
| **MD5 auth deprecation warnings** | "As Dana, I want a heads-up about MD5 users" | Dana | Extend `audit_database` | Medium |

#### 7. Observability & monitoring

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **`pg_stat_lock`** (new view) | "As Dana, I want per-lock-type statistics" | Dana, Sam | **New tool** `read_pg_stat_lock`; integrate with `find_blocking_chains` | **Yes — strong.** |
| **`pg_stat_recovery`** (new view) | "As Sam, I want visibility into recovery operations" | Sam | **New tool** `read_pg_stat_recovery` | Medium |
| **`stats_reset` column** in many `pg_stat_*` views | "As Dana, I want to know when my counters were last reset" | Dana | Extend every `read_pg_stat_*` family member | Low |
| **`pg_stat_progress_vacuum.started_by` + `.mode`** | "As Dana, I want to know who launched the vacuum and what flavour" | Dana | Extend `read_vacuum_progress` (or add tool if missing) | Low |
| **`pg_stat_progress_analyze.started_by`** | Same as above for ANALYZE | Dana | Same | Low |
| **`EXPLAIN ANALYZE (IO)`** | "As Dana/Ari, I want to see AIO costs in a plan" | Dana, Ari | Extend `analyze_query_plan` / `explain_query` with an `io=true` option | Medium |
| **Per-process log levels** (`log_min_messages` per process) | "As Sam, I want to tune log verbosity per worker class" | Sam | Doc only | Low |
| **WAL full-page-write reporting** in VACUUM/ANALYZE logs | "As Dana, I want to know how much WAL my maintenance produces" | Dana | Extend `run_maintenance` description; integrate with cost reporting | Low |

#### 8. DDL & schema management

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **`pg_get_*` DDL functions** (roles, tablespaces, databases) | "As Ari/Dana, I want to dump role/db DDL without shelling out to pg_dumpall" | Ari, Dana | **New tool** `get_role_ddl` / `get_database_ddl` / `get_tablespace_ddl`; extend `generate_*` codegen tools | Medium |

#### 9. Operational improvements

| Feature | User story | Personas | MCPg surface | Demo? |
|---|---|---|---|---|
| **Online data checksums** (enable/disable without restart) | "As Dana, I want to turn on data checksums without a restart window" | Dana, Sam | **New tool** `enable_data_checksums` / `disable_data_checksums` | **Yes — strong.** |
| **JIT off by default** | "As Dana, I want to know if this regresses my workload" | Dana | Doc only | Low |
| **LZ4 default TOAST compression** | "As Dana, I get smaller tables out of the box" | Dana | Doc only | Low |
| **RADIUS auth removal** | Breaking | Dana | **Verify** `verify_connection_encryption` doesn't assume RADIUS | Low |
| **PL/Python event triggers** | "As Ari, I want event triggers in Python" | Ari | Niche; doc only | Low |

### Strategic positioning matrix

Scoring each candidate on five axes (each 1-5; higher = more strategic):

- **Value** — how often the persona will use the tool
- **Moat** — how hard it is to replicate without MCPg (advisor / automation logic adds moat; thin wrappers don't)
- **Demo** — can we show it in a 60-second screen-share
- **Marketing** — does this earn a release-blog headline
- **PG 19 dependence** — does PG 19 unlock genuinely new value or just thin polish

**PO score = value + 2 * moat + demo + marketing + PG 19 dependence** (max 25).

| # | Feature | Value | Moat | Demo | Marketing | PG 19 dep. | **PO score** | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | **SQL/PGQ — property graph queries** | 5 | 3 | 5 | 5 | 5 | **26** | Top of the list per user request. Strategic question: replace AGE, or run both? See sidebar below. |
| 2 | **REPACK / REPACK CONCURRENTLY tool** | 5 | 3 | 4 | 5 | 5 | **25** | Replaces pg_repack shell-out path. Huge ops win. |
| 3 | **Async I/O advisor (`recommend_io_method`)** | 5 | 3 | 4 | 5 | 5 | **25** | Headline PG 19 differentiator. |
| 4 | **MERGE / SPLIT partitions tools** | 5 | 2 | 5 | 4 | 5 | **23** | Two tools, single PR. Pair with the existing `partman_*` lifecycle. |
| 5 | **`pg_stat_lock` advisor integration** | 5 | 3 | 4 | 3 | 5 | **23** | Extends `find_blocking_chains` into per-lock-type analysis. |
| 6 | **Online data checksums tool** | 4 | 2 | 5 | 4 | 5 | **22** | Turn on checksums without a restart — high-impact, single-call. |
| 7 | **WAIT FOR LSN tool + RYW advisor** | 4 | 3 | 4 | 3 | 5 | **22** | Read-your-writes on hot standbys, made one-call. |
| 8 | **On-demand logical replication** | 4 | 2 | 4 | 4 | 5 | **21** | Avoid restart = pure DBA delight. |
| 9 | **Skip-scan-aware `recommend_indexes`** | 5 | 3 | 2 | 3 | 4 | **20** | Quiet win but lifts the most-used advisor across the board. |
| 10 | **`validate_check_constraint` tool** | 4 | 2 | 3 | 2 | 4 | **17** | Closes the `NOT VALID` loop. |
| 11 | **`pg_get_*` DDL tool family** | 4 | 2 | 3 | 2 | 4 | **17** | Cleaner than the pg_dumpall shell-out. |
| 12 | **`pg_stat_recovery` tool** | 3 | 2 | 2 | 2 | 5 | **15** | Read-only observability tool. |
| 13 | **Password expiration / MD5 advisor enrichment** | 3 | 2 | 2 | 2 | 4 | **14** | Quiet `audit_database` extension. |
| 14 | **Autovacuum scoring read tool** | 3 | 2 | 2 | 2 | 4 | **14** | New read tool over the new scoring view. |
| 15 | **EXPLAIN ANALYZE (IO) capture** | 3 | 1 | 3 | 2 | 4 | **12** | Extension on existing tool. |
| 16 | **`stats_reset` column propagation** | 2 | 1 | 1 | 1 | 3 | **8** | Defensive — propagate the new column through every read. |
| 17 | **PG 19 characterisation tests** | 2 | 1 | 1 | 1 | 3 | **8** | Defensive batch (partition expressions, MERGE … RETURNING, interval-hash). |
| 18 | **GROUP BY ALL / temporal UPDATE / ON CONFLICT DO SELECT** | 2 | 1 | 1 | 1 | 3 | **8** | NL→SQL surface + `run_write` shape pass-through. |
| 19 | **`effective_wal_level` exposure in `get_server_info`** | 2 | 1 | 1 | 1 | 3 | **8** | Tiny but cheap. |
| 20 | **`postgres_fdw` pushdown coverage doc + tests** | 2 | 1 | 1 | 1 | 3 | **8** | Doc + characterisation test. |
| 21 | **`pg_get_acl()` migration in `list_grants`** | 3 | 1 | 1 | 1 | 2 | **8** | Drop-in upgrade with PG ≤ 18 fallback. |
| 22 | **Per-process log levels doc** | 2 | 1 | 1 | 1 | 3 | **8** | Doc only. |
| 23 | **OAuth `pg_hba.conf` doc** | 2 | 1 | 1 | 1 | 3 | **8** | Doc only. |

### Strategic sidebar — SQL/PGQ vs AGE

MCPg already exposes a `graph_operations` bucket built on Apache AGE-style schema. PG 19's SQL/PGQ is the upstream-standard alternative. Three viable paths:

1. **Coexist** — keep AGE-style `run_cypher`, add SQL/PGQ as `run_pgq`. Easy migration story; longer-term maintenance burden. **Recommended for the first PR.**
2. **Replace** — deprecate AGE on PG 19+; users on PG ≤ 18 keep AGE; users on PG 19+ get SQL/PGQ as the default. Cleaner long term; breaks existing users.
3. **Bridge** — keep one MCP tool surface (`run_graph_query`) that detects PG version and dispatches. Hides the choice from the agent. Most agent-friendly. Highest implementation cost.

PO recommendation: ship (1) first to get SQL/PGQ in users' hands fast, then evaluate (3) as a follow-up once we have telemetry on which surface agents actually use.

### Re-ordered landing plan (PO view)

Sequenced by PO score, with bundling where related items share a PR. Each row carries a tagline for the release blog.

| Order | PR | Bundles rows | PO score (sum) | Blog tagline |
|---|---|---|---:|---|
| **PR-1** | **SQL/PGQ MVP** | #1 | 26 | "Cypher-in-SQL on PG 19, end-to-end via one MCP call." |
| **PR-2** | **REPACK tools** | #2 | 25 | "Online table rebuild without pg_repack — one tool, one transaction." |
| **PR-3** | **Async I/O advisor** | #3 + #15 | 25 + 12 = 37 | "Tell me which `io_method` is right for this workload." |
| **PR-4** | **Lock + blocking-chain refresh** | #5 + #12 | 23 + 15 = 38 | "Per-lock-type analytics + recovery progress, exposed to your agent." |
| **PR-5** | **Partition reorganisation** | #4 | 23 | "MERGE / SPLIT partitions through the same advisor that already runs partman." |
| **PR-6** | **Online checksums + on-demand logical replication** | #6 + #8 | 22 + 21 = 43 | "Two restart-free toggles your DBA will actually use." |
| **PR-7** | **WAIT FOR LSN + RYW advisor** | #7 | 22 | "Read-your-writes from any hot standby, in one MCP call." |
| **PR-8** | **Skip-scan-aware `recommend_indexes`** | #9 | 20 | "PG 19 makes index recommendations strictly better — automatically." |
| **PR-9** | **`validate_check_constraint` + `pg_get_*` DDL family** | #10 + #11 | 17 + 17 = 34 | "Validate-and-ship constraints; dump role/db DDL without pg_dumpall." |
| **PR-10** | **PG 19 small-tools batch** | #13 + #14 + #18 + #19 + #21 | ~46 sum | "All the small things — autovacuum scoring, password warnings, ACL upgrade, …" |
| **PR-11** | **PG 19 characterisation tests** | #16 + #17 + #20 | ~24 sum | "Defensive — keeps PG 14-18 green while PG 19 lands." |
| **PR-12** | **Docs sweep** | #22 + #23 + JSONpath, JIT, LZ4, RADIUS | — | "PG 19 operations playbook updates." |

### Sequencing — PO rationale

- **PR-1 first (SQL/PGQ)** — the marketable headline; gets agent telemetry early; lets us validate the coexist-vs-replace decision before committing the bigger refactor.
- **PR-2 and PR-3 next** — both are "Yes / huge" demos. We can run the release blog as a three-part series.
- **PR-4 / PR-5 / PR-6** — bundle related ops surface so the agent gets coherent batches.
- **PR-7 onwards** — depth fills, each one ships in its own week.
- **PR-11 always lands behind a feature batch** so characterisation tests anchor the latest behaviour.
- **PR-12** is the "polish" PR — defer until other PRs are in.

### Cost ladder (rough)

| Effort tier | PRs | Per-PR scope |
|---|---|---|
| S (≤ 500 LOC + tests) | PR-2, PR-5, PR-6, PR-7, PR-8, PR-9, PR-12 | One module + tool family + 15-25 tests |
| M (≤ 1000 LOC + tests + module) | PR-3, PR-4, PR-10, PR-11 | Module + advisor + cross-bucket touches |
| L (multi-module) | PR-1 | New `mcpg.pgq` module + agent-friendly bridge + AGE coexistence semantics |

### Personas' MoSCoW for the next quarter

- **Dana the DBA** — MUST: PR-2, PR-3, PR-4, PR-6. SHOULD: PR-5, PR-7. COULD: PR-10, PR-12.
- **Ari the App developer** — MUST: PR-1, PR-7. SHOULD: PR-9. COULD: PR-12.
- **Riya the RAG engineer** — MUST: PR-1 (graph for retrieval), PR-3 (AIO under vector workloads). SHOULD: PR-8.
- **Sam the SRE** — MUST: PR-4, PR-6. SHOULD: PR-7, PR-13.
- **Aiden the AI agent** — MUST: PR-1 (richer graph surface); benefits from every readability gain.

## Phase 3 — incremental landing plan

| Milestone | Work | Gate |
|---|---|---|
| Now (Beta 1) | CI matrix + Phase 1 compat shims | This PR |
| Beta 2/3 | Feature-specific PRs based on Phase 2 audit | ~5-10 PRs |
| RC | Final pass; promote PG 19 to a required CI job (drop `continue-on-error`) | One PR |
| **GA day-0** | **Full tool-surface verification sweep against an actual PG 19 server.** Spin up `postgres:19` (no longer beta) locally and in CI, run the complete unit + integration suite with each Phase 3 tool exercised end-to-end — `recommend_io_method` against a real workload, `repack_table` against a real table, `run_pgq` against a real `CREATE PROPERTY GRAPH`, `validate_check_constraint`, etc. Confirm every recommendation / DDL we emit is accepted by the GA server unmodified. File any drift fixes as same-day patch PRs before flipping the PyPI classifier. | One verification PR (per Phase 3 tool family) |
| GA + 1 week | Release advertising PG 19 support in README + PyPI classifiers | Release PR |

> **GA-verification note** (user direction, captured 2026-06-20): once PG 19 hits GA we re-run **every** Phase 3 tool end-to-end against the real release — not just the version probe; we exercise the advisor heuristics, the DDL paths, and any SQL syntax we compose. Any tool whose generated `ready_to_run_sql` or recommendation doesn't behave as designed on the GA build gets fixed in a same-day patch PR before the README / classifier bump. Items 12 + 13 from the Phase 2 audit (GIN OR-of-AND perf bench, per-sequence access methods) get re-evaluated for prioritisation at the same time.

## Why bother now

1. **The compatibility check is cheap.** Most failures will be shape changes in `pg_stat_*` views or new columns mid-`SELECT *` queries. Fixing them in advance avoids panic when a user upgrades to PG 19 in staging.
2. **Skip-scans alone are worth the early read.** `recommend_indexes` becomes meaningfully more accurate when it can suggest indexes that benefit from the new B-tree skip-scan optimisation.
3. **AIO is genuinely new operational surface.** Operators will want guidance on `io_method=io_uring` vs `worker` vs `sync`. Having `recommend_io_method` ready at GA is differentiation.
4. **Community signal.** Being explicitly "PG 19 day-one ready" on the PyPI page is a stronger positioning than waiting.

## Local testing

### One-command smoke harness (recommended)

```bash
# Build PG 19 image, spin up a container, exercise every Phase 3 tool
# end-to-end, tear down. Idempotent — re-run anytime.
scripts/smoke_test_pg19.sh

# Want to leave the container running for manual poking afterwards:
scripts/smoke_test_pg19.sh --keep

# Tear down a leftover container:
scripts/smoke_test_pg19.sh --down
```

The harness prints a capability matrix (each Phase 3 status probe →
available / unavailable) followed by per-tool JSON output. Use it as
the "what does MCPg actually do on PG 19" demo, and as the
verification gate for every Phase 3 PR before merge. The launcher is
in `scripts/smoke_test_pg19.sh`; extend the per-tool exercises in
`scripts/smoke_test_pg19.py`.

### Manual setup (if you don't want the harness)

```bash
# Build the PG 19 image locally:
docker build -f .github/ci-postgres-pg19.Dockerfile -t mcpg-pg19 .

# Run the test suite against it:
docker run -d --name mcpg-pg19 -p 5432:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=mcpg_test mcpg-pg19
MCPG_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mcpg_test \
  uv run pytest -q --cov
```

Expected behaviour: PostGIS-dependent tests skip / fail (PostGIS isn't built into the PG 19 beta image); everything else should pass. Failures outside that category are the Phase 2 triage queue.

## Cross-references

- Beta 1 release notes: <https://www.postgresql.org/about/news/postgresql-19-beta-1-released-3313/>
- Tracking issue: #120
- Phase A static fact pack (Phase B observation harness baseline): `docs/reviews/tool-surface-fact-pack.md`
