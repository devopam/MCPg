---
title: Performance benchmark
---

# MCPg performance benchmark — the overhead is small, and we show exactly where it goes

**The one-sentence claim:** running a query *through MCPg* costs about a
millisecond of fixed overhead more than running the same SQL directly —
dominated by the read-only-transaction envelope, plus serialization that scales
with the number of rows returned — and on any query that does real work, that
overhead disappears into the noise. The database execution itself
(`t_db`) is **identical** to native.

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

- On a **heavy** query, `t_db` fills almost the entire bar — the overhead bands
  are slivers. The overhead is a *negligible fraction* of the total, because it
  is fixed-cost and the query is expensive.
- On an **ultralight** query (`SELECT 1`), the same fixed bands are a *large
  fraction* of a tiny total. This is the honest flip side: in **relative** terms
  the overhead on a trivial query can be several-fold; in **absolute** terms it
  is still about a millisecond, so it is immaterial to anything an agent actually
  does. We lead with the absolute numbers to keep the relative ones honest.

Two things the measured decomposition makes plain, and both are worth being
straight about:

- **The read-only transaction envelope dominates the fixed overhead** — not
  parsing. On `SELECT 1` the `BEGIN TRANSACTION READ ONLY` + `ROLLBACK` round
  trips (`t_txn`) are roughly **two-thirds** of the added cost and several times
  the query itself; parse (`t_parse`) is next, and pool checkout (`t_pool`) is
  negligible. That envelope is not waste — it is the read-only guarantee that
  makes running agent-supplied SQL safe. You are paying ~0.5 ms for the property
  that a `SELECT` *cannot* have written anything.
- **Serialization scales with rows.** For small results `t_serialize` is
  nothing; for a 100 k-row fetch it is the largest band (tens of milliseconds),
  because every row is materialized into the tool's dict shape. Agents rarely
  pull 100 k rows through a single call — but where they do, this is the cost,
  and it is real.

Everything except serialization is fixed-cost — it does not grow with the query
— which is why it vanishes as a share of any query that does real work.

## Throughput under concurrency

Single-client latency hides the costs that only appear under load — the bounded
connection pool and per-call work competing for CPU. The sweep drives native and
server-side at **1 / 4 / 16 / 64 concurrent clients** on **ultralight point
lookups** (the only class where per-call overhead is the signal rather than the
database's own execution or serialization volume), each path given an equal
connection budget so the comparison measures genuine overhead rather than
artificial pool starvation.

Here the fixed per-call cost shows its teeth honestly: on a sub-millisecond
point lookup, the server-side path sustains **roughly half** the native
throughput, because ~0.5–1 ms of fixed overhead on top of a ~0.3 ms query is a
large *relative* tax. This is the same fact as the latency numbers, seen from
the throughput side — and it is the *worst case*, a query so trivial the
overhead is most of the total. On any query that does real work the two paths
converge.

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
    --scale-factor 1 --iterations 20 --e2e --concurrency \
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
| "You're hiding the cost on trivial queries." | The opposite — the normalized decomposition shows the overhead is a *large relative share* of `SELECT 1` (about half its throughput under load). It is still ~1 ms absolute, and we say so. |
| "You're hand-waving 'negligible' — where does it go?" | Itemized: the read-only transaction envelope dominates (~⅔), then parse; serialization scales with rows. Nothing is hidden in a rounded-down mean. |
| "Cherry-picked queries." | The heavy tier is standard TPC-H; the full two-axis query set, the harness, and the raw JSON are committed. Run it yourself. |
| "Single-shot numbers are noise." | N ≥ 20 with warm-up discarded, p50/p95/p99, seeded bootstrap CIs, GC pinned out of the timed region. |

## Results

The numbers below are a real run — every figure is verified against a live
measurement, never asserted from memory. The committed run JSON and generated
dashboard ([`benchmarks/results/`](https://github.com/devopam/MCPg/tree/main/benchmarks/results))
are the source of truth. This run is **TPC-H SF1 on PostgreSQL 16**, N = 20
warm iterations; SF10 on dedicated hardware is the eventual *headline* scale
(numbers shift, the shape does not), and the "run it yourself" harness above
reproduces both.

**`t_db == native`: 11 / 11 queries pass.** Across queries spanning 0.3 ms to
2 s, the server-side database segment matches the native baseline — deltas from
about −0.05 s to +0.02 s on the multi-second queries (i.e. measurement noise),
sub-millisecond on the rest. MCPg adds nothing to the execution itself.

**Warm p50 latency, native vs MCPg server-side** — on anything doing real work,
the overhead is a rounding error:

| Query | Native | MCPg server-side | Overhead |
|---|---|---|---|
| `SELECT 1` | 375 µs | 794 µs | +0.42 ms |
| `orders_status_counts` (90 ms GROUP BY) | 91.6 ms | 95.2 ms | **+3.6 ms (+4 %)** |
| TPC-H Q3 | 399 ms | 401 ms | **+2 ms (+0.5 %)** |
| TPC-H Q6 | 424 ms | 431 ms | +7.7 ms (+1.8 %) |
| TPC-H Q1 | 2087 ms | 1999 ms | −88 ms (−4 %, noise) |
| 100 k-row fetch | 199 ms | 237 ms | +38 ms (serialization) |

The `SELECT 1` overhead is +0.42 ms — a large *percentage* of a 0.4 ms query,
an irrelevance in absolute terms. The one query with a visible cost is the
100 k-row fetch, where materializing every row adds ~40 ms; that is the honest
price of pulling a large result through a tool call (agents paginate instead).

**Where that fixed overhead goes** (decomposition of `SELECT 1`): the read-only
transaction envelope `t_txn` is **≈ two-thirds** of it (~0.57 ms), parse is
next (~0.1 ms), pool checkout is ~0.02 ms, serialization ~0.002 ms. The
dominant cost is the safety guarantee, not overhead you'd want to remove.

**Throughput under concurrency** (ultralight point lookups, 1→64 clients):
native sustains ~3500 q/s, server-side ~1600 q/s — the server path holds
**~half** native throughput on a sub-millisecond query, the worst case for a
fixed per-call cost, and the two converge on any heavier query.

**The end-to-end path** an agent actually drives (through the MCP protocol +
server middleware — audit logging, rate limiting, tracing) adds a few
milliseconds per call on top of server-side; and shipping a 100 k-row result
through JSON-RPC is expensive (seconds), which is a further argument for
paginating rather than bulk-fetching through a tool.

## Scope, and what's next

This is v1 — the performance half of the thesis, and it stands on its own. It
does **not** measure the token efficiency that is MCPg's actual headline
advantage; that is the [v2 study](plans/benchmark-suite.md#objective-2--token-efficiency)
(deterministic per-call token accounting, then an agent-loop task study with
published transcripts, net of the tool-schema context cost). The performance
result exists to clear the obvious objection — *"surely this adds latency"* —
so the token argument is heard on its merits.
