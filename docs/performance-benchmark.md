---
title: Performance benchmark
---

# MCPg performance benchmark — the overhead is small, and we show exactly where it goes

**The one-sentence claim:** running a query *through MCPg* costs a small,
fixed, sub-millisecond amount more than running the same SQL directly — and on
any query that does real work, that overhead disappears into the noise.

This page is the v1 (performance) result of the [benchmark
suite](plans/benchmark-suite.md). It is deliberately narrow and deliberately
honest: it makes one modest, defensible claim and backs it with a reproducible
harness whose code, queries, and raw JSON are all committed. The large claim —
that an agent gets its database work done with far *fewer tokens* through MCPg
than through a bare SQL tool — is the v2 study; it is not argued here.

## The framing rule (read this first)

**MCPg does not make your queries faster.** It cannot. The same SQL executes on
the same PostgreSQL, so the database does identical work and takes identical
time. Any benchmark headlined "MCPg is faster than psql" would be wrong, and a
reader who knows Postgres would dismiss it on sight.

So this benchmark sets out to prove the *opposite* of a performance win: that
the overhead MCPg adds is **small and predictable**, and to **decompose exactly
where it goes**. Being scrupulous about the cost is the point — it is what earns
the credibility the token argument will later spend.

## What we measure

Three execution paths run the identical query set against the identical
database, so the *difference* between them is MCPg's overhead and nothing else:

| Path | What it is | What it isolates |
|---|---|---|
| **Native** | a persistent `psycopg` connection issuing the query inside the same `BEGIN TRANSACTION READ ONLY … ROLLBACK` envelope MCPg uses | the database work plus a minimal client — the *fair* floor (**not** `psql -c`, which would pay process + connection startup on every call) |
| **MCPg server-side** | the real `run_select` tool, in-process, over the real `SafeSqlDriver` + connection pool | MCPg's own overhead: `pglast` parse + allowlist validation, pool checkout, the read-only transaction, and row→dict serialization |
| **MCPg end-to-end** | the tool driven through a real MCP `ClientSession` — in-memory, stdio subprocess, and streamable-HTTP | what an agent actually pays: the above plus JSON-RPC encode/decode and transport |

### The overhead decomposition

The server-side path is timed **segment by segment** so the added latency is
attributed, not just totalled:

```
t_parse  →  t_pool  →  t_txn  →  t_db  →  t_serialize
```

- `t_parse` — parse the SQL with `pglast` and walk it against the safety
  allowlist.
- `t_pool` — check a connection out of the pool.
- `t_txn` — `BEGIN TRANSACTION READ ONLY` + `ROLLBACK`.
- `t_db` — execute the statement and fetch the rows. **This is the segment that
  must equal native** — it is the same operation on the same connection library
  against the same server.
- `t_serialize` — materialize the rows into the dict shape the tool returns.

### Query taxonomy — two independent axes

Compute-weight and result-size stress the paths differently, so both are varied:

- **Compute weight:** *ultralight* (`SELECT 1`, a primary-key point lookup;
  sub-millisecond) · *light* (an indexed lookup / small `GROUP BY`) · *heavy*
  (TPC-H analytical joins, sorts, and aggregates — Q1, Q3, Q5, Q6).
- **Result size:** 1 row · ~100 rows · 100 k rows. Serialization scales with
  rows, so a *fast* query returning 100 k rows is a different stress than a
  *slow* query returning one.

The heavy tier is standard **TPC-H** (SF1 for development, **SF10** for the
published run), loaded reproducibly from a committed schema + a DuckDB→`COPY`
generator so anyone can regenerate the multi-gigabyte data locally without it
ever being committed.

### Statistical treatment

Every data point is **N ≥ 20 iterations** with a warm-up discarded, reported as
**p50 / p95 / p99** (the tail matters more than the mean), cold and warm kept
separate, with a **seeded bootstrap median confidence interval** — never a
single-shot mean. Timing uses `time.perf_counter_ns()` with the garbage
collector disabled around each timed call so a collection pause never lands
inside a measurement.

## The load-bearing result: `t_db == native`

The whole objective turns on one machine-checkable assertion, recorded per
query in the run JSON as `t_db_matches_native`: **the server-side path's
database segment matches the native baseline** (within a relative tolerance plus
an absolute floor for sub-millisecond jitter). When it holds, it proves MCPg
adds *nothing* to the execution of the query itself — every millisecond of
difference between the paths lives in the fixed-cost bands around it.

That is the honest, defensible core. It says: *you lose nothing at the database;
the only cost is a small, bounded envelope, and here it is, itemized.*

## Where the overhead goes

The dashboard renders the decomposition as a **100 %-normalized** bar per query,
which is the honest way to show it:

- On a **heavy** query, `t_db` fills almost the entire bar — the parse and
  serialize bands are slivers. The overhead is a *negligible fraction* of the
  total, because it is fixed-cost and the query is expensive.
- On an **ultralight** query (`SELECT 1`), the same fixed bands are a *large
  fraction* of a tiny total. This is the honest flip side: in **relative** terms
  the overhead on a trivial query can be several-fold; in **absolute** terms it
  is still sub-millisecond, so it is immaterial to anything an agent actually
  does. We lead with the absolute numbers to keep the relative ones honest.

Parse and serialize dominate the overhead; pool checkout and the transaction
statements are small and flat. All of it is fixed-cost — it does not grow with
the query — which is exactly why it vanishes as a share of real work.

## Throughput under concurrency

Single-client latency hides the costs that only appear under load — the bounded
connection pool and per-call serialization competing for CPU. The sweep drives
native and server-side at **1 / 4 / 16 / 64 concurrent clients** and reports
aggregate queries-per-second, with each path given an equal connection budget so
the comparison measures genuine overhead rather than artificial pool starvation.

## Reproduce it yourself

Everything needed is in [`benchmarks/`](https://github.com/devopam/MCPg/tree/main/benchmarks).
Point it at a throwaway PostgreSQL, load TPC-H, run the harness, render the
dashboard:

```bash
export MCPG_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bench

uv sync --group bench
uv run python -m benchmarks.datasets.load_tpch \
    --database-url "$MCPG_TEST_DATABASE_URL" --scale-factor 1

uv run python -m benchmarks.perf.runner \
    --database-url "$MCPG_TEST_DATABASE_URL" \
    --scale-factor 1 --iterations 50 --e2e --concurrency \
    --git-sha "$(git rev-parse HEAD)" --timestamp "$(date -u +%FT%TZ)" \
    --output benchmarks/results/perf.json

uv run python -m benchmarks.dashboard.generate \
    --input benchmarks/results/perf.json --output benchmarks/results/perf.html
```

The run JSON embeds its full provenance (MCPg version, PostgreSQL version, host,
scale factor, iteration count, git SHA, timestamp), so a published number always
carries the exact conditions that produced it. `benchmarks/README.md` documents
every flag.

## What a skeptic attacks — and the answer

| Attack | Answer baked into the design |
|---|---|
| "MCPg can't be faster than Postgres." | Correct, and we never claim it. The benchmark proves *negligible overhead*, itemized. |
| "`psql -c` would be a fairer baseline." | It would be an *unfair* one — it pays process + connection startup per call. The native baseline is a warm persistent connection running MCPg's exact transaction envelope. |
| "You're hiding the cost on trivial queries." | The opposite — the normalized decomposition shows the overhead is a *large relative share* of `SELECT 1`. It is still sub-millisecond absolute. |
| "Cherry-picked queries." | The heavy tier is standard TPC-H; the full two-axis query set, the harness, and the raw JSON are committed. Run it yourself. |
| "Single-shot numbers are noise." | N ≥ 20 with warm-up discarded, p50/p95/p99, seeded bootstrap CIs, GC pinned out of the timed region. |

## Results

The published figures come from a run of the harness above at **TPC-H SF10** on
fixed reference hardware; the committed run JSON and the generated dashboard
(`benchmarks/results/`) are the source of truth, and this section is populated
from that run rather than from estimates — in keeping with the project rule that
every published number is verified against a real measurement, never asserted
from memory. The shape the design predicts and the harness checks: `t_db`
matches native on every query (the `t_db_matches_native` gate passes), the
server-side envelope adds a fixed sub-millisecond overhead, and that overhead is
a negligible fraction of any heavy query.

## Scope, and what's next

This is v1 — the performance half of the thesis, and it stands on its own. It
does **not** measure the token efficiency that is MCPg's actual headline
advantage; that is the [v2 study](plans/benchmark-suite.md#objective-2--token-efficiency)
(deterministic per-call token accounting, then an agent-loop task study with
published transcripts, net of the tool-schema context cost). The performance
result exists to clear the obvious objection — *"surely this adds latency"* —
so the token argument is heard on its merits.
