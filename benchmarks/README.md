# MCPg benchmark suite

Reproducible, evidence-driven benchmarks for MCPg. **v1 = performance /
overhead** (this directory's `perf/`); the token-efficiency study is v2. Full
design and thesis: [`../docs/plans/benchmark-suite.md`](../docs/plans/benchmark-suite.md).

**The framing that keeps this honest:** MCPg does *not* make queries run faster
— the same SQL executes on the same PostgreSQL. The performance benchmark
proves MCPg's overhead is small and predictable (and decomposes exactly where
it goes), so `t_db` is provably identical to native. That "you lose nothing"
result is what earns credibility for the real wins (measured in v2: tokens).

## What runs today

- `perf/paths.py` — the **native** (persistent psycopg, same txn envelope, no
  MCPg overhead) and **server-side** (in-process `run_select`) measurement paths.
- `perf/decompose.py` — the **overhead-decomposition waterfall**: times the
  server path segment-by-segment (`t_parse → t_pool → t_txn → t_db →
  t_serialize`) and records the load-bearing **`t_db == native`** assertion.
- `perf/e2e.py` — the **end-to-end paths** through the real MCP protocol
  (in-memory, stdio subprocess, streamable HTTP) — the `t_protocol` band. Opt-in
  via `--e2e` / `--e2e-http-url`.
- `perf/concurrency.py` — the **throughput sweep** at 1/4/16/64 concurrent
  clients (native + server-side), where pool + serialization overhead surface.
  Opt-in via `--concurrency`.
- `perf/queries.py` — the two-axis query set (compute x result-size), heavy tier
  from TPC-H.
- `perf/stats.py` — percentiles + bootstrap median CI + warm-up handling (pure,
  unit-tested).
- `perf/runner.py` — orchestrates paths x queries (cold + warm) → structured JSON.
- `dashboard/generate.py` — renders a run's JSON into a **self-contained,
  theme-aware HTML dashboard** (latency percentiles, the overhead-decomposition
  waterfall, throughput-vs-concurrency, the `t_db == native` gate). No external
  hosts; re-run the harness, regenerate.
- `datasets/` — TPC-H schema/index DDL + a DuckDB→`COPY` loader.

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
    --output benchmarks/results/perf.json

# 4. Render the JSON into a self-contained HTML dashboard.
uv run python -m benchmarks.dashboard.generate \
    --input benchmarks/results/perf.json \
    --output benchmarks/results/perf.html
```

### Also measuring the end-to-end MCP paths (`--e2e`)

`--e2e` adds the in-memory and stdio-subprocess paths (they self-configure from
`--database-url`). For the HTTP path, start a server first and point the runner
at it:

```bash
# In one shell: an operator-started streamable-HTTP server on the same DB.
MCPG_DATABASE_URL="$MCPG_TEST_DATABASE_URL" mcpg --transport streamable-http

# In another: run with the e2e paths, including HTTP.
uv run python -m benchmarks.perf.runner \
    --database-url "$MCPG_TEST_DATABASE_URL" --scale-factor 1 --iterations 50 \
    --e2e --e2e-http-url http://127.0.0.1:8000/mcp \
    --output benchmarks/results/perf-e2e.json
```

Provenance (git SHA, timestamp, versions, host, scale factor) is embedded in
every result file, so a published number always carries the exact conditions
that produced it.

## Reproducibility

Pinned versions, fixed query literals, seeded bootstrap, warm-up discarded,
cold vs warm reported separately, medians + CIs (never single-shot means). The
committed DDL + loader regenerate the data anywhere; the gigabytes are never
committed.
