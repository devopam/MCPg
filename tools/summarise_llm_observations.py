"""Phase B — summarise LLM observation JSONL into a Markdown report.

Reads ``docs/reviews/phase-b-observations.jsonl`` (whatever
``observe_llm_behaviour.py`` produced) and renders a single Markdown
report. The report ties each LLM observation back to its Phase A
finding so the reader can see, finding-by-finding, whether the
static signal was actually born out in behaviour.

Sections:

1. **Run metadata** — provider, model, prompt count, hit rate,
   token totals, total cost estimate, latency stats.
2. **Per-category results** — hit rate, miss list, and a digest of
   the model's text response so we can see whether the misses were
   confusion or genuine alternatives.
3. **Per-prompt details** — full transcript-style entry for each
   prompt: expected, picked, text response, args proposed.
4. **Confirmed Phase A findings** — auto-derived list of which
   static flags Phase B confirmed, refuted, or left undecided.

Run with::

    python tools/summarise_llm_observations.py \\
        > docs/reviews/phase-b-report.md

The summariser is read-only; the JSONL is the source of truth.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_JSONL_PATH = _REPO_ROOT / "docs" / "reviews" / "phase-b-observations.jsonl"

# Anthropic Sonnet 4.x list pricing as of the corpus run (USD per 1M
# tokens). The constants are intentionally inline so the cost number
# in the report is self-documenting; bump when prices change.
_INPUT_USD_PER_M = 3.00
_OUTPUT_USD_PER_M = 15.00


def _load_observations() -> list[dict[str, Any]]:
    if not _JSONL_PATH.exists():
        print(
            f"ERROR: no observations found at {_JSONL_PATH}.\nRun `python tools/observe_llm_behaviour.py` first.",
            file=sys.stderr,
        )
        sys.exit(2)
    return [json.loads(line) for line in _JSONL_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _format_args(args: dict[str, Any] | None) -> str:
    if not args:
        return "_(none)_"
    return "`" + ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items()) + "`"


def _truncate(text: str, limit: int = 280) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main() -> int:
    rows = _load_observations()
    if not rows:
        print("ERROR: observation file is empty.", file=sys.stderr)
        return 2

    # ---- run metadata -------------------------------------------------
    n = len(rows)
    providers = sorted({r["provider"] for r in rows})
    models = sorted({r["model"] for r in rows})
    total_in = sum(int(r.get("input_tokens", 0)) for r in rows)
    total_out = sum(int(r.get("output_tokens", 0)) for r in rows)
    cost_in = total_in / 1_000_000 * _INPUT_USD_PER_M
    cost_out = total_out / 1_000_000 * _OUTPUT_USD_PER_M
    latencies = [int(r.get("latency_ms", 0)) for r in rows if r.get("latency_ms")]
    errors = [r for r in rows if r.get("raw_error")]
    judged = [r for r in rows if r.get("matched_expected") is not None]
    hits = [r for r in judged if r.get("matched_expected") is True]
    misses = [r for r in judged if r.get("matched_expected") is False]

    out: list[str] = []
    out.append("# MCPg tool-surface fact pack — Phase B (LLM behaviour observation)")
    out.append("")
    out.append(
        f"_Generated from `docs/reviews/phase-b-observations.jsonl` "
        f"({n} prompts). Provider(s): {', '.join(providers)}. "
        f"Model(s): {', '.join(models)}. Phase A findings referenced as "
        f"`fact-pack §X` ←→ `tool-overlap-report.md #N`._"
    )
    out.append("")

    out.append("## 1. Run metadata")
    out.append("")
    out.append(f"- Prompts run: **{n}**  (errors: {len(errors)})")
    out.append(f"- Judgable prompts: **{len(judged)}**  (`expected_tool` set or `expected_no_tool_call=true`)")
    if judged:
        pct_hit = 100 * len(hits) / len(judged)
        out.append(f"- Hit rate: **{len(hits)} / {len(judged)}** (**{pct_hit:.0f}%**)")
    out.append(
        f"- Token totals: **{total_in:,} input + {total_out:,} output**  "
        f"≈ **${cost_in + cost_out:.2f}** at "
        f"${_INPUT_USD_PER_M:.2f}/M in + ${_OUTPUT_USD_PER_M:.2f}/M out"
    )
    if latencies:
        out.append(
            f"- Latency — median **{int(statistics.median(latencies))} ms**, "
            f"p90 **{int(statistics.quantiles(latencies, n=10)[-1])} ms**, "
            f"max **{max(latencies)} ms**"
        )
    out.append("")

    # ---- per-category --------------------------------------------------
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    out.append("## 2. Per-category results")
    out.append("")
    out.append("| Category | Prompts | Judged | Hits | Hit rate |")
    out.append("|---|---:|---:|---:|---:|")
    for cat in sorted(by_cat):
        cat_rows = by_cat[cat]
        cat_judged = [r for r in cat_rows if r.get("matched_expected") is not None]
        cat_hits = [r for r in cat_judged if r.get("matched_expected") is True]
        rate = f"{100 * len(cat_hits) / len(cat_judged):.0f}%" if cat_judged else "n/a"
        out.append(f"| `{cat}` | {len(cat_rows)} | {len(cat_judged)} | {len(cat_hits)} | {rate} |")
    out.append("")

    if misses:
        out.append("### 2a. Misses (LLM picked something other than expected)")
        out.append("")
        for r in misses:
            out.append(
                f"- **`{r['prompt_id']}`** _(category: `{r['category']}`)_ — "
                f"expected `{r['expected_tool']}`, got "
                f"`{r['picked_tool'] or '(no tool)'}`"
            )
            out.append(f"  - User prompt: _{_truncate(r['user_prompt'])}_")
            if r.get("text_response"):
                out.append(f"  - Text response (first line): _{_truncate(r['text_response'], 200)}_")
            out.append(f"  - Phase A reference: {r.get('phase_a_reference', '—')}")
        out.append("")

    # ---- per-prompt --------------------------------------------------
    out.append("## 3. Per-prompt details")
    out.append("")
    out.append(
        "_Every observation, ordered by category then prompt id._ "
        "Use this section when reviewing a category's behaviour in depth."
    )
    out.append("")
    for cat in sorted(by_cat):
        out.append(f"### Category — `{cat}`")
        out.append("")
        for r in sorted(by_cat[cat], key=lambda x: x["prompt_id"]):
            verdict = (
                "✅ matched expected"
                if r.get("matched_expected") is True
                else "❌ did not match expected"
                if r.get("matched_expected") is False
                else "— undecidable"
            )
            out.append(f"#### `{r['prompt_id']}` — {verdict}")
            out.append("")
            out.append(f"**Phase A reference:** `{r.get('phase_a_reference', '—')}`  ")
            out.append(f"**User prompt:** _{r['user_prompt']}_  ")
            if r.get("expected_tool"):
                out.append(f"**Expected tool:** `{r['expected_tool']}`  ")
            if r.get("expected_no_tool_call"):
                out.append("**Expected:** no tool call (text-only answer)  ")
            out.append(f"**Picked tool:** `{r.get('picked_tool') or '(no tool)'}`  ")
            if r.get("picked_args"):
                out.append(f"**Picked args:** {_format_args(r['picked_args'])}  ")
            out.append(f"**Stop reason:** `{r.get('stop_reason') or 'n/a'}`")
            out.append("")
            if r.get("text_response"):
                out.append("**Text response:**")
                out.append("")
                out.append("> " + r["text_response"].replace("\n", "\n> "))
                out.append("")
            if r.get("raw_error"):
                out.append(f"**Error:** `{r['raw_error']}`")
                out.append("")
            out.append("---")
            out.append("")

    # ---- Phase A confirmation matrix -----------------------------------
    out.append("## 4. Phase A finding confirmation")
    out.append("")
    out.append(
        "Each Phase A finding mapped against what Phase B actually saw. "
        "A finding is **confirmed** if the relevant probes missed the "
        "expected pick or the LLM hallucinated capabilities; **refuted** "
        "if all probes passed cleanly."
    )
    out.append("")

    # Group by Phase A reference and report hit rate per group.
    by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_ref[r.get("phase_a_reference", "—")].append(r)

    out.append("| Phase A reference | Prompts | Judged | Hits | Hit rate | Verdict |")
    out.append("|---|---:|---:|---:|---:|---|")
    for ref, ref_rows in sorted(by_ref.items()):
        judged_rows = [r for r in ref_rows if r.get("matched_expected") is not None]
        hits_rows = [r for r in judged_rows if r.get("matched_expected") is True]
        rate_str = f"{100 * len(hits_rows) / len(judged_rows):.0f}%" if judged_rows else "n/a"
        if not judged_rows:
            verdict = "_undecidable_"
        elif len(hits_rows) == len(judged_rows):
            verdict = "**refuted** _(LLM handled cleanly)_"
        elif len(hits_rows) == 0:
            verdict = "**confirmed** _(LLM always wrong)_"
        else:
            verdict = "**partial** _(LLM sometimes confused)_"
        out.append(f"| {ref} | {len(ref_rows)} | {len(judged_rows)} | {len(hits_rows)} | {rate_str} | {verdict} |")
    out.append("")

    out.append("---")
    out.append("")
    out.append("## 5. Next steps")
    out.append("")
    out.append(
        "Based on the **confirmed** Phase A findings above, here is "
        "the prioritised list of catalogue improvements the data "
        "supports. The hit rate per finding is the quantitative "
        "argument: lower rate = stronger case for a tactical fix."
    )
    out.append("")
    out.append(
        "_(This section is hand-curated after the run — the summariser reports the data; the reviewer interprets it.)_"
    )
    out.append("")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
