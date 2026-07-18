# MCPg benchmark suite

Reproducible, evidence-driven benchmarks for MCPg. **v1 = performance /
overhead** (this directory's `perf/`); the token-efficiency study is v2. Full
design and thesis: [`../docs/plans/benchmark-suite.md`](../docs/plans/benchmark-suite.md).

**The framing that keeps this honest:** MCPg does *not* make queries run faster
— the same SQL executes on the same PostgreSQL. The performance benchmark
proves MCPg's overhead is small and predictable (and decomposes exactly where
it goes), so `t_db` is provably identical to native. That "you lose nothing"
result is what earns credibility for the real wins (measured in v2: tokens).

## What runs today (Phase 1)

- `perf/paths.py` — the **native** (persistent psycopg, same txn envelope, no
  MCPg overhead) and **server-side** (in-process `run_select`) measurement paths.
- `perf/queries.py` — the two-axis query set (compute x result-size), heavy tier
  from TPC-H.
- `perf/stats.py` — percentiles + bootstrap median CI + warm-up handling (pure,
  unit-tested).
- `perf/runner.py` — orchestrates paths x queries (cold + warm) → structured JSON.
- `datasets/` — TPC-H schema/index DDL + a DuckDB→`COPY` loader.

The end-to-end transport paths, the overhead-decomposition waterfall (the
`t_db == native` gate), the concurrency sweep, and the HTML dashboard land in
subsequent phases.

## Running it

```bash
# 1. A throwaway PostgreSQL (mirror scripts/benchmark_pg19.sh, or your own).
export MCPG_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bench

# 2. Load TPC-H (SF1 for dev, SF10 for a published run). Needs the bench group:
uv sync --group bench
uv run python -m benchmarks.datasets.load_tpch --database-url "$MCPG_TEST_DATABASE_URL" --scale-factor 1

# 3. Run the benchmark → JSON under benchmarks/results/.
uv run python -m benchmarks.perf.runner \
    --database-url "$MCPG_TEST_DATABASE_URL" \
    --scale-factor 1 --iterations 50 \
    --git-sha "$(git rev-parse HEAD)" --timestamp "$(date -u +%FT%TZ)" \
    --output benchmarks/results/perf-$(date -u +%Y%m%dT%H%M%SZ).json
```

Provenance (git SHA, timestamp, versions, host, scale factor) is embedded in
every result file, so a published number always carries the exact conditions
that produced it.

## Reproducibility

Pinned versions, fixed query literals, seeded bootstrap, warm-up discarded,
cold vs warm reported separately, medians + CIs (never single-shot means). The
committed DDL + loader regenerate the data anywhere; the gigabytes are never
committed.
