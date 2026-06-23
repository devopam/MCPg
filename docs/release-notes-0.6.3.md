# MCPg v0.6.3 — release notes

**Released:** 2026-06-23
**Tool surface:** 141 → **223** (+82 since v0.6.0; +49 in the PG 19 sprint alone)
**Tests:** 2,200+ pass
**CI:** PG 14 / 15 / 16 / 17 / 18 / 19 (PG 19 experimental, non-blocking until GA)

This release is the **PostgreSQL 19 readiness wave**: nine focused
modules cover the headline PG 19 features end-to-end (SQL/PGQ
property graphs, in-server REPACK, async-I/O advisor, lock + recovery
analytics, online runtime toggles, partition reorganisation,
skip-scan-aware index advisor, WAIT FOR LSN read-your-writes
advisor, and the `pg_get_*def()` DDL family). Every new tool ships
with the no-deprecation rule honoured — PG ≤ 18 operators keep their
existing fallback paths, advertised by per-tool status probes.

Alongside the PG 19 work: **structured `outputSchema` on the wire**
(LangChain / LangGraph integration), **pg_prewarm + redis_fdw
coverage**, the **operations playbook**, the **observation-loop
roadmap convention**, and the **CI noise cleanups** that bring the
PG 19 matrix entry to a clean steady state.

## Themes

| Theme | PRs | Highlights |
|---|---|---|
| **PG 19 SQL & query surface** | #127 (PR-1), #141 (PR-4), #144 (PR-9), #145 (PR-5), #146 (PR-7) | SQL/PGQ (`mcpg.pgq`), lock + recovery analytics (`mcpg.pg19_stats`), `pg_get_*def()` DDL family (`mcpg.pg19_ddl`), MERGE / SPLIT PARTITION (`mcpg.pg19_partitions`), WAIT FOR LSN + RYW advisor (`mcpg.wait_for_lsn`) |
| **PG 19 operations + maintenance** | #129 (PR-2), #131 (PR-3), #142 (PR-6), #148 (PR-8) | In-server REPACK (`mcpg.repack`), async-I/O advisor (`mcpg.aio`), online checksums + on-demand logical replication (`mcpg.pg19_runtime`), skip-scan advisor (`mcpg.pg19_skip_scan`) |
| **LangGraph / typed-state integration** | #150 (PR-13) | First sweep of FastMCP `outputSchema` auto-derivation — typed dataclass returns flow straight into LangChain `response_format=…` and LangGraph nodes' Pydantic state models. Contract test + sweep checklist landed for the remaining ~200 tools. |
| **Cache + foreign-data coverage** | #118 (`redis_fdw`), #119 (`pg_prewarm`) | Eight tools each, two new capability buckets (`cache_and_foreign_data`, `cache_warming`), plus the `recommend_*_targets` advisors. |
| **Phase 3 operational scaffold** | #143 (smoke harness), #149 (PR-12 ops playbook), #151 (roadmap), #152 (CI noise), #153 (pgvector v0.8.3), #154 (overlap analyser) | One-command live PG 19 smoke harness, operator-side PG 19 playbook, persistent roadmap + observation-loop convention, `Tests (PG 19)` apt + pgvector fixes, tool-overlap analyser. |

## Strategic notables

- **PG 19 day-1 readiness.** Nine PG 19 modules cover the Beta 1 headline features; each one ships with a `get_*_status` probe so agents can feature-detect at runtime. Phase 2 audit + PO scoring landed as `docs/plans/pg19-readiness.md`; Phase 3 PR sequencing followed it almost verbatim.
- **Outputs are now machine-validated.** Pre-v0.6.3 every MCP tool returned `dict[str, Any]`; consumers had no `outputSchema` to validate against. PR-13 sweeps the PG 19 DDL helpers family as the proof-of-concept; the manifest + contract test now lock in monotonic growth as the remaining ~200 tools are converted. **Direct LangGraph wire-up works for the converted tools today** — `langchain-mcp-adapters` picks up the auto-derived schema and feeds it to `response_format=<Pydantic>`.
- **Observation loop now in the codebase.** `docs/feature-shortlist.md` gained 14 new roadmap entries with date + source-of-observation in the Notes column. The convention is now explicit: every gap surfaced in a PR review or phase retrospective lands as a numbered roadmap row — chat threads no longer orphan gaps.
- **No-deprecation rule held across every PR.** AGE-style Cypher stays alongside SQL/PGQ. pg_repack shell-out stays alongside in-server REPACK. The detach / create / attach partition dance stays alongside MERGE / SPLIT. Every PG ≤ 18 operator keeps every tool they had at v0.6.x.

## Notable shape changes

- **`ValidateCheckConstraintResult.schema` → `.table_schema`** (PR-13). Pydantic's `BaseModel.schema()` shadow forced the rename. The tool itself was added in #144 (same release cycle); no external consumers to migrate.
- **`outputSchema` now populated** for the 5 PG 19 DDL helper tools. Backward-compatible — FastMCP still emits the legacy text content alongside the new `structuredContent`. Other ~200 tools continue with `outputSchema = None` until their sweep PR.

## Hygiene + housekeeping

- **CI noise reduction.** `Tests (PG 19)` step now distinguishes "package not yet published" (workflow `::warning::`, non-fatal) from genuine apt failures (kept fatal). pgvector pinned to v0.8.3 (the first PG 19-compatible release); best-effort try-and-continue wrapper stays as a safety net for future PG 20 / upstream-lag scenarios. Windows event-loop policy added to the smoke harness for Windows-native test clusters.
- **`test_audit_record_json_format` dedupes caplog by message text** — pytest 9.1's handler-attachment change was double-capturing the same record. Fix is forward-compatible across the pytest 9.x line.
- **Dependabot sweep.** msgpack, pglast, upload-artifact v7, download-artifact v8, setup-uv v7 all bumped to current. The bundled-update PR was closed as superseded (pglast handled separately; remaining bumps will repropose cleanly now that the audit test is fixed).

## Phase 3 PR map

| Phase 3 PR | Module | Bucket | PR # | Status |
|---|---|---|---|---|
| PR-1 SQL/PGQ MVP | `mcpg.pgq` | `property_graph_queries` (new) | #127 | ✅ shipped |
| PR-2 In-server REPACK | `mcpg.repack` | `operations_and_health` | #129 | ✅ shipped |
| PR-3 AIO advisor | `mcpg.aio` | `operations_and_health` | #131 | ✅ shipped |
| PR-4 Lock + recovery analytics | `mcpg.pg19_stats` | `operations_and_health` | #141 | ✅ shipped |
| PR-5 Partition MERGE / SPLIT | `mcpg.pg19_partitions` | `timeseries_partitioning` | #145 | ✅ shipped |
| PR-6 Runtime toggles | `mcpg.pg19_runtime` | `operations_and_health` | #142 | ✅ shipped |
| PR-7 WAIT FOR LSN + RYW | `mcpg.wait_for_lsn` | `operations_and_health` | #146 | ✅ shipped |
| PR-8 Skip-scan advisor | `mcpg.pg19_skip_scan` | `advisors` | #148 | ✅ shipped |
| PR-9 DDL helpers | `mcpg.pg19_ddl` | `schema_introspection` + `operations_and_health` | #144 | ✅ shipped |
| PR-10 Small-tools batch | _various_ | _various_ | — | 🟡 carried forward (roadmap 2.5) |
| PR-11 Characterisation tests | _tests/_ | n/a | — | 🟡 carried forward (roadmap 3.4) |
| PR-12 Ops playbook | `docs/plans/` | n/a | #149 | ✅ shipped |
| PR-13 outputSchema sweep (first) | _various_ | n/a | #150 | ✅ shipped — sweep continues (roadmap 8.6) |

## What's carried forward

See [`docs/feature-shortlist.md`](feature-shortlist.md) — every observed gap lives in the roadmap as a numbered row:

- **8.6** outputSchema sweep across the remaining ~200 legacy `dict[str, Any]` tools — each module a small mechanical PR per the contributing-skill checklist
- **8.3 / 8.4 / 8.5** MCP resources, prompts, and `describe_tool` — the next discoverability wave after `outputSchema`
- **8.7 / 8.8** session-scope cost advisor + session-intent handshake — agentic-workflow primitives
- **2.5 / 2.6** PR-10 small-tools batch + EXPLAIN ANALYZE (IO) capture — Phase 3 follow-ups
- **3.3 / 3.4** benchmark harness + PR-11 characterisation tests — defensive coverage
- **14.4 / 14.6** describe_self headline-tools drift + PR-to-roadmap-row linkage — process polish

## GA-day-0 verification (committed)

Per `docs/plans/pg19-readiness.md`: once PG 19 hits GA we re-run **every** Phase 3 tool end-to-end against the real release — not just the version probe; we exercise the advisor heuristics, the DDL paths, and any SQL syntax we compose. Any tool whose generated `ready_to_run_sql` or recommendation doesn't behave as designed on the GA build gets fixed in a same-day patch PR before the README / classifier bump.

## Upgrade notes

- **Drop-in for PG 14-18 users.** No tools removed; no return shapes broken (except `ValidateCheckConstraintResult.schema` → `.table_schema`, which was introduced in the same cycle).
- **PG 19 users.** Every Phase 3 tool feature-detects; `get_*_status` calls report `available=true` and the tool surface lights up. Pair with the `scripts/smoke_test_pg19.sh` harness to exercise every Phase 3 tool against your cluster in one command.
- **LangChain / LangGraph users.** The PG 19 DDL helpers family now exposes `outputSchema` — wire `response_format=<Pydantic model>` against them directly. Other tools are next in the sweep.

## See also

- [`../CHANGELOG.md`](../CHANGELOG.md) — full per-entry detail
- [`plans/pg19-readiness.md`](plans/pg19-readiness.md) — the PG 19 audit + landing plan
- [`plans/pg19-operations-playbook.md`](plans/pg19-operations-playbook.md) — operator-side PG 19 behaviour changes (JIT off, LZ4 TOAST, RADIUS removal, OAuth, …)
- [`feature-shortlist.md`](feature-shortlist.md) — the persistent roadmap
- Tracking issue #120 — PG 19 readiness
