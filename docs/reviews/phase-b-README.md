# Phase B — LLM behaviour observation pipeline

A self-contained harness for measuring how an LLM actually behaves
when it sees MCPg's tool catalogue. The output ties back to specific
findings in `tool-surface-fact-pack.md` (Phase A) and
`tool-overlap-report.md` (Phase A.0) so we can confirm — or refute —
the static signals with measured behaviour before we touch any
source code.

> **Note on `tool-overlap-report.md`.** The Phase A.0 overlap report
> was a working artifact generated during this review and is not
> committed to the repo. The `phase_a_reference` tags below (e.g.
> `tool-overlap-report.md #1 (score 4.99)`) are preserved as
> provenance for the point-in-time (v0.6.2) analysis that produced
> this corpus — they are historical labels, not live links.

## Files

| File | Role |
|---|---|
| `phase-b-prompts.json` | The corpus — 22 prompts across 4 categories, each tagged with the Phase A finding it probes. Edit to add / remove / re-tune probes. |
| `phase-b-observations.jsonl` | Generated. One JSONL row per (prompt × model × run). Append-only — keep across runs to compare provider/model behaviour. |
| `phase-b-report.md` | Generated. Human-readable digest with run metadata, per-category hit rates, per-prompt detail, and a Phase A confirmation matrix. |
| `../../tools/observe_llm_behaviour.py` | The harness. Reads the corpus + the v0.6.2 tool snapshot, calls Anthropic Messages API with the full tool catalogue, writes JSONL. |
| `../../tools/summarise_llm_observations.py` | The reporter. Reads JSONL, writes Markdown. |

## Prompt corpus structure

Each prompt in `phase-b-prompts.json` has:

```json
{
  "id": "od-01",
  "category": "overlap_disambiguation",
  "phase_a_reference": "tool-overlap-report.md #1 (score 4.99)",
  "user_prompt": "I want to look at the individual WAL records ...",
  "expected_tool": "read_pg_wal_records",
  "rationale": "Records vs stats — 'individual' is the signal."
}
```

Categories:

1. **`overlap_disambiguation`** — 8 prompts probing the top pairs from
   the overlap report. Tests whether the LLM picks the right tool when
   two names look similar.
2. **`list_family_picker`** — 4 prompts probing the 39-tool `list_*`
   family. Tests whether the LLM disambiguates among same-prefix tools.
3. **`return_shape`** — 5 prompts asking the LLM to describe what a
   tool returns *without calling it*. Tests whether descriptions
   convey return shape (Phase A flagged 75 tools missing this hint).
4. **`self_introspection`** — 5 prompts asking high-level capability
   questions ("What can mcpg do?"). Tests the gap Phase A flagged:
   no MCP resources, no MCP prompts, no `about` tool.

Each prompt either has an `expected_tool` (the right pick) **or**
`expected_no_tool_call: true` (the right behaviour is to answer in
text without calling a tool). The summariser uses these to compute
hit rate per category and per Phase A finding.

## Running

```bash
export ANTHROPIC_API_KEY=sk-...

# Smoke test with 3 prompts before paying for a full run:
python tools/observe_llm_behaviour.py --limit 3

# Full corpus run, all 22 prompts, Sonnet:
python tools/observe_llm_behaviour.py \
    --model claude-sonnet-4-6 \
    --output docs/reviews/phase-b-observations.jsonl

# One category at a time when iterating:
python tools/observe_llm_behaviour.py --category self_introspection

# Then render the report:
python tools/summarise_llm_observations.py \
    > docs/reviews/phase-b-report.md
```

### Cost expectation per full run

- **Input**: 22 prompts × ~30k tokens (catalogue + prompt + system) ≈ **660k input tokens**
- **Output**: 22 × ~500 tokens ≈ **11k output tokens**
- **At Sonnet 4.x list pricing** ($3/M in, $15/M out): **≈ $2.15** per cold run.
- **With prompt caching** (Anthropic enables for `tools=` automatically on warm calls): typically **~$0.40** on the second+ run.

If you want to evaluate cheaper models first, swap `--model` to
`claude-haiku-4-5-20251001` (~10× cheaper). Halve the cost again with
`--limit 10` while you iterate on the corpus.

### Reproducibility

The harness uses `temperature=0.0` and a stable system prompt. Two
runs on the same model / corpus produce near-identical observations
(Anthropic's tool-use sampling has minor non-determinism even at
T=0, but tool picks are stable).

## Interpreting the report

The summariser's **§4 Phase A confirmation matrix** is the headline
deliverable. It tells you, for each Phase A finding referenced by a
prompt, whether Phase B's behaviour evidence:

- **Confirmed** the static flag (LLM got it wrong)
- **Refuted** it (LLM handled it fine despite the static signal)
- Showed a **partial** pattern (LLM sometimes confused)

That maps directly to what to fix. A "confirmed" finding with a 0%
hit rate is a clear catalogue-improvement candidate; a "refuted"
finding suggests the static flag was overcautious and we can ignore
it. The §5 "Next steps" section is intentionally left empty for the
reviewer to fill in based on the confirmation matrix.

## When to re-run

- After any non-trivial change to `register_tools` (rename, schema
  shift, description rewrite). The contract test (PR #115) will tell
  you the surface changed; this harness tells you whether the change
  improved or regressed LLM behaviour.
- When upgrading the target model (e.g. Sonnet 4.7 → Sonnet 4.8).
- When considering a structural change (consolidating an ORM family,
  adding an `about` tool, adding MCP resources).

The corpus is the contract; the report is the evidence trail.
