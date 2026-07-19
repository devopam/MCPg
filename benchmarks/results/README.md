# Published benchmark results

Committed run artifacts — the source of truth the [performance
writeup](../../docs/performance-benchmark.md) is populated from. Each `.json`
embeds its full provenance (MCPg / PostgreSQL / host versions, scale factor,
iteration count, git SHA, timestamp); the matching `.html` is the self-contained
dashboard rendered from it by `benchmarks.dashboard.generate`.

| File | Run |
|---|---|
| `perf-sf1-pg16.json` / `.html` | TPC-H **SF1**, PostgreSQL 16, N=20 warm iterations, all paths (native / server-side / e2e in-memory + stdio) + the concurrency sweep. `t_db == native` gate: 11/11. |

SF1 is the development scale; **SF10 on dedicated hardware is the headline
published scale** (the absolute numbers shift, the shape holds). Regenerate any
run with the commands in [`../README.md`](../README.md) — nothing here is
hand-authored.
