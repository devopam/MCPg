# MCPg v0.6.4 — release notes

**Released:** 2026-06-27
**Tool surface:** **243** tools across 19 capability buckets
**Tests:** 2499 pass (PG 14 / 15 / 16 / 17 / 18 / 19)
**CI:** PG 14-19 + an experimental WarehousePG lane

This is a **patch-level bump (0.6.2 → 0.6.4)**. The intervening 0.6.3
cut was staged but never published; everything it would have carried
plus the work since rolls into 0.6.4. The no-deprecation rule held
across every PR — PG ≤ 18 paths stay advertised by per-tool status
probes, and no consumer-visible return shape was removed or renamed.

> Why 0.6.4 (skipping 0.6.3): the 0.6.3 release PR sat open while ~30
> more PRs landed on `main`; rather than re-base a stale cut, the next
> published version is 0.6.4 with the complete rolled changelog. The
> minor bump stays reserved for a stability commitment closer to 1.0.

| Theme | Highlights |
|-------|------------|
| **PG 19 readiness** | SQL/PGQ, in-server REPACK, async-I/O advisor, lock+recovery analytics, runtime toggles, partition MERGE/SPLIT, skip-scan advisor, WAIT FOR LSN + RYW, DDL helpers, characterisation tests (3.4) |
| **WarehousePG (§15)** | MPP detection probe + read introspection (distribution policies, segment health, AO tables, resource groups) + MPP advisors (`analyze_mpp_query_plan`, `recommend_redistribute`) + experimental CI lane |
| **Logical replication writes (2.1)** | `create_publication` / `drop_publication` / `create_subscription` / `drop_subscription` with DSN-redacting result repr |
| **Agent ergonomics** | `generate_test_row_for` (8.1), `analyze_session_cost` (8.7), session-intent surface filter (8.8), `recommend_headline_tools` (14.4) |
| **Config & sizing advisors (§16)** | `audit_sequences` (overflow), `audit_settings` (postgresql.conf sweep), `recommend_postgres_conf` (pgtune calculator) |
| **Vector** | `recommend_hnsw_ef_search` — multi-query recall@k / latency sweep with index verification (9.1) |
| **LangChain / LangGraph integration** | `outputSchema` sweep (8.6) — **192** tools now emit a typed JSON output schema on the wire |
| **Process** | Roadmap-row linkage tooling (14.6) — PR template + `tools/roadmap_linkage.py` validator |

## Headline: structured outputs (`outputSchema`) across the surface

The 8.6 sweep converted every tool that returns a single helper
dataclass verbatim from a `dict[str, Any]` return to its typed
frozen-dataclass return, so FastMCP auto-derives a JSON `outputSchema`
that LangChain / LangGraph clients validate against. The
structured-output manifest floor climbed from **5 → 192** across seven
batches. List-returning tools wrap into a `{"result": [...]}`
envelope; the ~17 remaining `dict`-typed tools are code-emitting /
result-restructuring handlers (ORM generators, `describe_self`,
`prepare_migration`) that legitimately can't auto-derive a clean
schema and are documented opaque exceptions.

Backward-compatible: FastMCP keeps emitting the legacy text `content`
array alongside the new `structuredContent`, so clients reading only
`content` see no change.

## Upgrade

```bash
pip install --upgrade mcpg   # once published to PyPI
```

No configuration changes required. Every new capability is additive
and gated behind the same `MCPG_ACCESS_MODE` / `MCPG_ALLOW_DDL`
controls as before. The new `MCPG_SESSION_INTENT` env var (8.8) is
opt-in — unset means the full tool surface, exactly as today.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.4]` for the complete
itemised list (Added / Changed / Fixed), including the Phase 3 PG 19
readiness block previously staged for 0.6.3.
