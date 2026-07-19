# Published benchmark results

Committed run artifacts — the source of truth the [performance
writeup](../../docs/performance-benchmark.md) is populated from. Each `.json`
embeds its full provenance (MCPg / PostgreSQL / host versions, scale factor,
iteration count, git SHA, timestamp); the matching `.html` is the self-contained
dashboard rendered from it by `benchmarks.dashboard.generate`.

| File | Run |
|---|---|
| `perf-sf1-pg16.json` / `.html` | **Performance** — TPC-H **SF1**, PostgreSQL 16, N=20 warm iterations, all paths (native / server-side / e2e in-memory + stdio) + the concurrency sweep. `t_db == native` gate: 11/11. |
| `tokens-tier-a-sf1.json` | **Tokens, Tier A** — deterministic token accounting on the same SF1 DB (`o200k_base`). Compact schema −76%, query-plan analysis −96% vs the raw-SQL equivalent; full tool surface +48k tokens upfront, break-even ~18 tasks. |

SF1 is the development scale; **SF10 on dedicated hardware is the headline
published scale** (the absolute numbers shift, the shape holds). Regenerate any
run with the commands in [`../README.md`](../README.md) — nothing here is
hand-authored.
