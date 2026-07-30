---
title: Token efficiency
---

# MCPg token efficiency — the compact output pays for the rich surface

**The claim:** MCPg's purpose-built tools return dramatically more compact
output than the raw-SQL equivalent an agent would otherwise pull and interpret
itself — measured per call, deterministically, no LLM involved.

This is the token half of the [benchmark suite](plans/benchmark-suite.md), and
the companion to the [performance writeup](performance-benchmark.md). It
measures **per-call compactness**: tokenize what MCPg's tools *return* vs the
raw-SQL equivalent an agent would otherwise pull. No LLM, no cost, reproducible,
CI-able.

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

MCPg's full surface was **252 tools** at the time of this SF1 run (the committed
JSON is that snapshot; the surface grows as tools are added), and every tool
definition costs context tokens on every turn. A "tokens saved" number that
ignores that is dishonest,
and a reviewer would rightly pounce. So we measure it head-on — a bare
`run_select` tool is **193** tokens, and MCPg's surface is far larger. With a
mean per-call saving of ~2,750 tokens per database task, the surface is repaid
after a break-even number of tasks — **and that number depends on how much
surface you expose**, which is a real operator lever:

| Tool surface | Tools | Upfront tokens | Extra vs bare | Break-even |
|---|---|---|---|---|
| full (unrestricted) | 252 | 63,878 | +63,685 | **~24 tasks** |
| read-only (the default) | 185 | 48,576 | +48,383 | **~18 tasks** |
| `MCPG_SESSION_INTENT=lookup` | 53 | 11,281 | +11,088 | **~5 tasks** |

The dashboard draws the worst case (the full surface) as two cumulative-token
lines: MCPg starts high (its tool surface) but rises slowly; the bare tool
starts near zero but pays the raw cost every task. They cross at ~24 tasks,
after which MCPg is cheaper and the gap widens. Below that, a session of one or
two quick lookups genuinely does *not* amortize the full surface — and we say
so.

But few deployments carry the full surface. **Read-only mode** (the default)
already drops it to ~18 tasks, and **`MCPG_SESSION_INTENT`** filtering — which
narrows the surface to the tools a session actually needs — brings a `lookup`
session to **~5 tasks**. These are not spin: they are the same knobs an operator
sets for safety and prompt-injection resilience, measured here for their token
effect too.

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
| "You ignored the 252-tool context cost." | It is the headline of this page: +63,685 tokens upfront for the full surface, and the break-even against it (per surface) is charted. |
| "Token counts are tokenizer-specific." | True in absolute terms; the *ratio* MCPg-vs-raw is stable across tokenizers, and the encoding is stated. |
| "Tier A isn't a real agent." | Correct — it measures deterministic per-call compactness, not a full agent session end to end. That's the stated scope of this page. |
| "SF1 is small." | The compactness ratios are a property of the representation, not the row count; they hold across scales. |
| "Break-even ~24 is a lot." | For a one-off lookup on the full surface, yes — and we say so. But read-only (the default) is ~18, and a session-intent-filtered `lookup` surface is ~5 tasks; for an agent doing sustained DB work it is quickly cleared. |

## Scope

This page reports the deterministic, per-call token argument only — it does not
claim, measure, or forecast the total tokens an agent spends across a full
session. Paired with the [performance result](performance-benchmark.md)
(negligible overhead, `t_db` identical to native), the evidence-based case is:
**MCPg's compact, structured tool output saves tokens on the calls it's used
for, and the cost of the tool surface is shown, not hidden.**
