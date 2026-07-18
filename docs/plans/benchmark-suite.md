# MCPg benchmark suite — plan

**Status:** planning (this doc). **Approach:** plan-first, then phased PRs
(harness → token accounting → dashboard → agent study → writeup).
**Audience of the *output*:** public — a conclusive, evidence-driven case,
built to survive a skeptical technical reader (the kind who knows Postgres).

## The thesis (what we set out to prove)

> **An LLM agent doing database work is dramatically more token-efficient and
> more reliable through MCPg than through a bare SQL-execution tool — at a
> negligible performance cost, with safety guarantees raw SQL access can't
> offer.**

Every clause of that sentence maps to a measured number:

| Clause | Evidence |
|---|---|
| *dramatically more token-efficient* | 40–70%+ fewer tokens per database task vs a bare `run_select` tool (the headline) |
| *more reliable* | fewer error→retry loops, higher task-completion rate |
| *negligible performance cost* | sub-millisecond, decomposed overhead; ~0 % on heavy queries; cache **beats** native on repeat reads |
| *safety guarantees* | dangerous statements provably rejected |

### The framing rule (non-negotiable)

**We do not claim MCPg makes queries run faster.** It can't — the same SQL
executes on the same PostgreSQL, so the database does identical work. Any
deck headlined "MCPg is faster" is dead on arrival with the target audience.
The performance objective exists to **disarm the obvious objection** ("this
must add latency") by proving the overhead is tiny and predictable — which is
precisely what *earns credibility* for the large token-efficiency claim. Being
scrupulous about the overhead is a feature of the argument, not a weakness.

## Objectives

1. **Performance / overhead.** Quantify MCPg's added latency vs native SQL on
   the same database, across ultralight / light / heavy queries — and
   **decompose** where the overhead goes.
2. **Token efficiency.** Quantify the tokens an LLM agent saves using MCPg's
   purpose-built surface vs a bare SQL-execution tool, **net of** the
   tool-definition context cost.

### Scope: v1 vs later

**v1 is the performance objective + the dashboard.** It's fully deterministic
(no model, no cost, no non-determinism), proves the "negligible overhead" half
of the thesis, and stands on its own as a publishable result. The **token
objective (2) is v2** — it inherits the same dashboard, which evolves to add
the token charts as those results land. This keeps v1 shippable fast and defers
the costed/non-deterministic agent work until the free result is in hand.

## Non-goals / honesty guardrails

- Not a claim that MCPg out-executes Postgres.
- Not a comparison against "no database access" (a rigged win). The token
  baseline is a *bare `run_select` tool*, so we isolate the value of MCPg's
  **tool design**, not merely "having a database."
- No cherry-picking: the full query set, task set, harness, and (for the agent
  study) transcripts are published. "Run it yourself."
- The 252-tool context cost is measured and shown, never hidden.

---

## Objective 1 — Performance / overhead

### Three measurement paths (the middle one is the insight)

| Path | Isolates |
|---|---|
| **Native** — a persistent `psycopg` connection running the SQL directly | DB execution + minimal client. The fair baseline — **not** `psql -c`, which pays process startup per call. |
| **MCPg server-side** — call the tool function in-process | MCPg's own overhead: pglast parse + AST-walk validation, pool checkout, `BEGIN TRANSACTION READ ONLY` / `ROLLBACK`, row→JSON serialization, middleware. |
| **MCPg end-to-end** — through the MCP client + transport (stdio **and** streamable-http) | What an agent actually experiences: adds JSON-RPC encode/decode + transport. |

### Overhead decomposition (diagnostic, not just comparative)

Instrument the server path into timed segments and report a **waterfall**:

```
t_protocol → t_parse → t_pool → t_txn → t_db → t_serialize → t_cache(hit)
```

The killer result: **`t_db` is identical to native**, and we point at exactly
where the added milliseconds live (parse + serialize dominate; all are
fixed-cost, so they vanish as a fraction of heavy queries).

### Query taxonomy — two independent axes

Compute-weight and result-size stress MCPg differently, so vary both:

- **Compute weight:** *ultralight* (`SELECT 1`, PK point-lookup; sub-ms) ·
  *light* (indexed lookup / small GROUP BY; 1–10 ms) · *heavy* (TPC-H
  analytical joins/sorts/aggregates; 100 ms–seconds).
- **Result size:** 1 row · ~100 rows · 10 k–100 k rows. Serialization scales
  with rows, so a *fast* query returning 100 k rows is a different stress than
  a *slow* query returning 1 row.

The heavy tier is the **22 TPC-H queries**; ultralight/light draw from
point-lookups and small aggregates over the TPC-H schema (+ a TPC-C-style
small-transaction set for throughput).

### Metrics & statistical treatment

- Latency **p50 / p95 / p99** (tail matters more than mean), reported cold and
  warm, with the **cache-hit path measured separately** (where MCPg
  legitimately beats native).
- **Throughput under concurrency** (queries/sec at 1/4/16/64 clients) — pool
  and serialization overhead only show up under load.
- N iterations with warm-up discarded; report **medians + variance / CIs**,
  never single-shot means.

### Expected (honest) shape of the result

Overhead ≈ 0 % on heavy queries; possibly 3–10× *relative* on `SELECT 1` but
still **sub-ms absolute** (so: immaterial); cache hits < native on repeats.
We lead with **absolute** numbers to keep the relative ones honest.

---

## Objective 2 — Token efficiency

### Baseline

**MCPg's purpose-built surface** (compact schema, advisors, structured
`outputSchema`, NL→SQL, resources) **vs. a bare `run_select` tool** — same
model, same tasks, same dataset. This isolates tool *design*, not DB access.

### Two tiers (do both)

- **Tier A — deterministic I/O accounting** (cheap, reproducible, CI-able, no
  LLM). Tokenize the *content* MCPg returns vs the raw-SQL equivalent:
  `get_compact_schema` vs a full `information_schema` dump; an `audit_database`
  structured report vs the raw rows an agent would pull and interpret;
  `analyze_query_plan` typed output vs raw `EXPLAIN` text. Measures **per-call
  compactness** deterministically. This is the backbone.
- **Tier B — agent-loop task runs** (realistic; costs money; non-deterministic).
  Run a fixed agent to task completion, counting total in+out tokens, tool
  calls, turns, and **correctness**, over N trials per task. Captures the big
  saving Tier A can't: **fewer round-trips** (one `analyze_workload` call vs
  many exploratory queries + interpretation loops).

### Task archetypes (mix of big-win and honest-small-win)

1. Schema comprehension — "describe this database."
2. Slow-query diagnosis **and fix** — "why is this slow, and how do I fix it?"
3. Database health audit — "audit this database."
4. PII discovery — "find columns with sensitive data."
5. Plain NL→SQL — "top 5 customers by revenue last quarter." *(Include this
   deliberately: MCPg saves little here. A one-sided result reads as marketing.)*

Tasks run over TPC-H (rich schema) plus the **`mcpg --demo` dataset** for the
planted-finding tasks (the unindexed FK, PII columns, camelCase naming) that
have **known-correct answers** — so "correctness" is objective.

### The break-even accounting (the credibility centerpiece)

MCPg exposes **252 tool definitions**, and every definition costs context
tokens each turn. A "tokens saved" figure that ignores that is dishonest and a
reviewer *will* pounce. So we:

- Measure the upfront tool-schema token cost: full 252 vs a `MCPG_SESSION_INTENT`-
  filtered subset vs the bare `run_select` baseline.
- Report **net** tokens (upfront + per-task) and plot a **break-even curve**:
  after *K* database tasks in a session, the per-task savings overtake the
  upfront cost — and show how session-intent filtering moves *K* left.

Publishing our own strongest counter-argument *as a chart we then defeat* is
what convinces engineers.

### Metrics

Tokens in / out per task; tool-call count; turns-to-completion; correctness
rate; upfront tool-schema cost; **net** savings; break-even *K*.

---

## Datasets

- **TPC-H** (heavy/analytical): scale factor **SF1** (~1 GB) for development,
  **SF10** for the published run. Loader script + a documented generator path
  (`dbgen`, or a DuckDB/`pgbench`-based generator) committed so anyone can
  reproduce.
- **TPC-C-style** small-transaction set for ultralight/throughput.
- **`mcpg --demo`** dataset (deterministic, planted findings) for the
  correctness-checkable token tasks.

## Reproducibility

Pinned MCPg + PostgreSQL + model versions; fixed seeds; fixed model at
temperature 0 for Tier B; dedicated DB instance (or explicitly-controlled
concurrency); warm-up discarded; N runs with variance reported. Everything —
queries, tasks, transcripts, raw JSON — committed alongside the results.

## Dashboards (reusable)

The harness writes **structured JSON** result files; a generator renders a
**self-contained, theme-aware HTML dashboard** (no external hosts — same rules
as an Artifact). It ships in **v1** rendering the performance results (latency
percentiles by query class, the overhead **waterfall**, throughput-vs-
concurrency) and **evolves** to add the token charts (savings by task, the
break-even curve) in v2. Re-run the harness → regenerate. Portable, reviewable,
publishable.

*(A Grafana/Prometheus live variant was considered — MCPg already exposes
Prometheus metrics — but rejected for v1: a benchmark result is a snapshot, not
a live feed, so the runnable-infra overhead buys little. The self-contained
committed HTML is the artifact; a live variant can come later if a genuine need
appears.)*

## Repo layout

```
benchmarks/
  README.md                 # how to run everything, reproducibly
  datasets/                 # TPC-H / TPC-C loaders, demo hook, generators
  perf/                     # native vs server-side vs end-to-end + decomposition
  tokens/
    tier_a/                 # deterministic I/O token accounting
    tier_b/                 # agent-loop task runner (fixed model, N trials)
  dashboard/                # JSON → self-contained HTML generator
  results/                  # committed JSON + generated dashboards for published runs
```

## Phasing

**v1 (performance):**
1. **Perf harness + overhead decomposition** on TPC-H (deterministic, no LLM).
2. **Dashboard generator** (JSON → self-contained HTML) rendering the perf
   results.
3. **v1 writeup** — the "negligible overhead" result, publishable on its own.

**v2 (tokens), inheriting the same dashboard:**
4. **Tier-A token accounting** (deterministic).
5. **Tier-B agent study** (fixed model, N trials, published transcripts — the
   costed phase).
6. **Combined writeup** drawing performance + tokens together.

Each phase is independently valuable and lands as its own PR.

## What a skeptic attacks — and our answer

| Attack | Answer baked into the design |
|---|---|
| "MCPg can't be faster than Postgres." | Correct — we never claim it. Perf objective proves *negligible overhead*, and cache wins on repeats. |
| "Your token baseline is rigged." | Baseline is a bare `run_select` tool (real DB access), same model; tasks + transcripts published. |
| "You ignored the 252-tool context cost." | Measured explicitly; net-of-overhead break-even is a headline chart. |
| "Cherry-picked queries/tasks." | Full sets committed; TPC-H is a standard; demo tasks have known answers. |
| "Non-deterministic agent runs." | Tier A is deterministic; Tier B pins model+temp, N trials, reports distribution. |

## Resolved decisions (locked for v1)

- **v1 scope:** performance objective + the HTML dashboard. Token objective is v2.
- **TPC-H scale:** **SF1** for development, **SF10** for the published run —
  SF10 is the ceiling (no SF30).
- **Dashboard:** self-contained **static HTML** (no Grafana/Prometheus in v1 —
  a result is a snapshot, not a live feed). Evolves to add token charts in v2.
- **Concurrency points:** 1 / 4 / 16 / 64 clients.
- **Trials:** ≥ 20 per perf data-point (medians + variance reported).
- **Token baseline (v2):** a bare `run_select` tool, same model — isolates
  MCPg's tool *design*, not merely DB access.
- **Writeup home:** the reproducible harness + JSON + generated HTML under
  `benchmarks/`, plus a narrative writeup on the docs site (surfaced as a
  shareable Artifact).

## Deferred to v2 (decide then)

- The exact Tier-B model(s) to pin at temperature 0 (and whether to run a
  second model to show the token savings generalize across models).
- Tier-B trial count (default ≥ 10 per task) and the final task set.
