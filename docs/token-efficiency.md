---
title: Token efficiency
---

# MCPg token efficiency — the compact output pays for the rich surface

**The claim:** an LLM agent doing database work spends far fewer tokens through
MCPg's purpose-built tools than by pulling raw SQL and interpreting it itself —
enough that MCPg's larger upfront tool surface is repaid after a modest number
of tasks in a session.

This is the token half of the [benchmark suite](plans/benchmark-suite.md), and
the companion to the [performance writeup](performance-benchmark.md). It comes in
two tiers; this page reports **Tier A**, the deterministic one.

- **Tier A (here)** — deterministic per-call token accounting. Tokenize what
  MCPg's tools *return* vs the raw-SQL equivalent an agent would otherwise pull.
  No LLM, no cost, reproducible, CI-able. It measures **per-call compactness**.
- **Tier B (future)** — a fixed model at temperature 0 runs real tasks to
  completion, N trials each, counting total tokens + tool calls + turns +
  correctness. It captures the saving Tier A *cannot*: **fewer round trips** (one
  `analyze_workload` call instead of many exploratory queries + interpretation).
  It costs money and is deferred until it is run properly.

Being explicit about that split is the point: Tier A is a floor on the token
argument — a real, deterministic saving — not the whole of it.

## What Tier A measures

For each scenario, two strings are tokenized: what MCPg's tool returns, and the
raw-SQL equivalent an agent would pull and then have to interpret. Counting uses
`tiktoken` with the `o200k_base` encoding (the current GPT-4o / GPT-5-family
BPE). Exact counts vary a little across model tokenizers, but the **ratio** —
which is the claim — is stable across them, so one well-known reference keeps the
result reproducible.

Everything is measured against a live database, never estimated. The committed
run below is TPC-H **SF1** on PostgreSQL 16; the compactness ratios are a
property of the *representations*, not the data volume, so they hold across
scales.

## The results

From the committed run
([`benchmarks/results/tokens-tier-a-sf1.json`](https://github.com/devopam/MCPg/tree/main/benchmarks/results)):

| Comparison | MCPg | raw SQL | |
|---|---|---|---|
| `get_compact_schema` vs an `information_schema.columns` dump | **574** | 2,375 | **−76 %** (4.1×) |
| `analyze_query_plan` vs raw `EXPLAIN (FORMAT JSON)` | **146** | 3,847 | **−96 %** (26×) |

MCPg's compact schema format (`[table] pk:… \| col:type \| …`) carries the same
facts an agent needs — tables, columns, types, keys, nullability — in a quarter
of the tokens of the raw catalog rows. Its structured plan analysis (the cost,
the row estimate, the node types, the sequential scans) is a *fraction* of the
raw `EXPLAIN` JSON, which is mostly nested bookkeeping an agent must wade through.

## The break-even — the honest centerpiece

MCPg exposes **252 tools**, and every tool definition costs context tokens on
every turn. A "tokens saved" number that ignores that is dishonest, and a
reviewer would rightly pounce. So we measure it head-on:

| | Tokens |
|---|---|
| MCPg's full tool surface (252 tools) | **48,576** |
| a bare `run_select` tool | 193 |
| **upfront extra MCPg carries** | **+48,383** |

That is a real, large upfront cost. It is repaid by the per-call savings: with a
mean saving of ~2,750 tokens per database task, MCPg overtakes the bare-tool
baseline after

> **break-even ≈ 18 database tasks** in a session.

The dashboard draws this as two cumulative-token lines: MCPg starts high (its
tool surface) but rises slowly; the bare tool starts near zero but pays the raw
cost every task. They cross at ~18 tasks, after which MCPg is cheaper and the gap
widens. Below that, a session of one or two quick lookups genuinely does *not*
amortize the surface — and we say so.

Two things move the break-even **left**, and both are honest levers rather than
spin:

- **Read-only mode** exposes a subset of the 252 tools, so the upfront cost is
  smaller in the common deployment.
- **`MCPG_SESSION_INTENT`** filtering narrows the surface to the tools a session
  actually needs. (Quantifying each is a Tier-A follow-up — the harness already
  measures the full surface; the filtered variants slot in the same way.)

## Reproduce it yourself

```bash
export MCPG_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bench
uv sync --group bench   # brings in tiktoken
# load any schema with tables (e.g. the TPC-H loader, or mcpg --demo)

uv run python -m benchmarks.tokens.tier_a.runner \
    --database-url "$MCPG_TEST_DATABASE_URL" --schema public \
    --git-sha "$(git rev-parse HEAD)" --timestamp "$(date -u +%FT%TZ)" \
    --output benchmarks/results/tokens-tier-a.json

# render it into the dashboard (alongside the perf results)
uv run python -m benchmarks.dashboard.generate \
    --input benchmarks/results/perf.json \
    --tokens benchmarks/results/tokens-tier-a.json \
    --output benchmarks/results/dashboard.html
```

## What a skeptic attacks — and the answer

| Attack | Answer |
|---|---|
| "You ignored the 252-tool context cost." | It is the headline of this page: +48,383 tokens upfront, and the break-even against it is charted. |
| "Token counts are tokenizer-specific." | True in absolute terms; the *ratio* MCPg-vs-raw is stable across tokenizers, and the encoding is stated. |
| "Tier A isn't a real agent." | Correct — it is deterministic per-call compactness, a *floor*. The round-trip saving is Tier B, explicitly future and costed. |
| "SF1 is small." | The compactness ratios are a property of the representation, not the row count; they hold across scales. |
| "Break-even 18 is a lot." | For a one-off lookup, yes — MCPg's surface doesn't pay off, and we say so. For an agent doing sustained DB work it is quickly cleared, and read-only / session-intent filtering move it left. |

## Scope, and what's next

This is the deterministic floor of the token argument. **Tier B** — the
agent-loop study with a fixed model, published transcripts, and correctness on
the planted-finding demo tasks — is the costed phase that captures the larger,
round-trip saving; it is deferred until it can be run rigorously. Paired with the
[performance result](performance-benchmark.md) (negligible overhead, `t_db`
identical to native), Tier A already makes the evidence-based case: **MCPg's
compact, structured surface saves the tokens that matter, and the cost of the
surface is shown, not hidden.**
