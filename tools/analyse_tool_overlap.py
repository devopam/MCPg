"""Tool-surface overlap analyser.

Scans the snapshotted MCPg tool catalogue for pairs likely to confuse an
LLM picker — similar names, overlapping descriptions, near-identical
input schemas. The output is a Markdown report ranked by suspicion
score, ready to be reviewed by a human deciding what (if anything) to
consolidate / rename / disambiguate.

Why this and not an LLM call?
- Reproducible (deterministic; no API key required)
- Cheap (runs in seconds against the checked-in snapshot)
- Auditable (a reviewer can verify every flagged pair against a
  visible scoring formula)

Run with:

    python tools/analyse_tool_overlap.py > docs/reviews/tool-overlap-report.md

…then review the report and decide which pairs (if any) deserve a
rename / merge / description tightening PR. The script is a triage
aid, not a verdict.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "tests" / "contract" / "tool_surface.snapshot.json"

# ---------------------------------------------------------------------------
# Scoring knobs — tune to taste; the values below are intentionally
# tolerant so we surface candidate pairs rather than chase a perfect
# threshold. A reviewer triages from the top of the ranked list.
# ---------------------------------------------------------------------------

# Pairs are flagged when *any* of the following hold:
NAME_SIMILARITY_MIN = 0.70  # difflib SequenceMatcher ratio on the names
DESC_JACCARD_MIN = 0.45  # Jaccard over content tokens in descriptions
DESC_OVERLAP_MIN = 0.65  # asymmetric containment — one desc subset of the other
SHARED_VERB_AND_NOUN = True  # name pairs sharing both a verb stem and a noun root


# A trim stop-word list — generic verbs / connectives the descriptions
# all use ("the", "a", "is", "this", …). Kept short on purpose; a
# bigger list would over-trim and hide real overlap.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "via",
        "when",
        "which",
        "with",
        "any",
        "this",
        "these",
        "those",
        "if",
        "you",
        "your",
        "we",
        "our",
        "us",
        "use",
        "uses",
        "used",
        "also",
        "set",
        "sets",
        # mcpg-specific connectives that appear in many descriptions
        "mcpg",
        "postgres",
        "postgresql",
        "tool",
        "tools",
        "table",
        "tables",
        "schema",
        "schemas",
        "query",
        "queries",
        "row",
        "rows",
        "column",
        "columns",
        "database",
        "databases",
    }
)


# Very rough verb / noun root extractor — strip the common -s/-es/-ed/-ing
# suffixes so ``analyse_x``, ``analyses_x``, ``analysed_x`` all land on
# the same stem. Not linguistically correct, just good enough to surface
# pairs.
_SUFFIXES = ("ies", "es", "ed", "ing", "s")


def _stem(word: str) -> str:
    for s in _SUFFIXES:
        if len(word) > len(s) + 2 and word.endswith(s):
            return word[: -len(s)]
    return word


def _tokenise_name(name: str) -> list[str]:
    """Split a tool name into root tokens.

    ``analyze_reranker_lift`` -> ``["analyz", "rerank", "lift"]``.
    """
    parts = re.split(r"[_\-]+", name.lower())
    return [_stem(p) for p in parts if p]


def _tokenise_description(text: str) -> set[str]:
    """Bag-of-stems of a description, with stop-words removed."""
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_]+", text.lower())
    stems = {_stem(w) for w in raw if w not in _STOPWORDS and len(w) > 2}
    return stems


# ---------------------------------------------------------------------------
# Pairwise scoring
# ---------------------------------------------------------------------------


def _name_similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio on the raw names."""
    return SequenceMatcher(None, a, b).ratio()


def _shared_verb_and_noun(a_tokens: list[str], b_tokens: list[str]) -> bool:
    """Heuristic: do the names share both a leading verb stem AND at
    least one trailing noun root?

    Catches families like ``analyze_x_lift`` vs ``analyze_y_lift`` —
    same verb prefix, same trailing noun, different middle = often
    confusing.
    """
    if not a_tokens or not b_tokens:
        return False
    if a_tokens[0] != b_tokens[0]:
        return False
    return bool(set(a_tokens[1:]) & set(b_tokens[1:]))


def _description_overlap(a: set[str], b: set[str]) -> tuple[float, float]:
    """Return (jaccard, max_containment).

    Jaccard captures symmetric overlap; max-containment captures the
    "one description is essentially a subset of the other" case which
    plain Jaccard understates when one description is long and the
    other short.
    """
    if not a or not b:
        return 0.0, 0.0
    inter = len(a & b)
    union = len(a | b)
    jaccard = inter / union if union else 0.0
    containment = inter / min(len(a), len(b))
    return jaccard, containment


# ---------------------------------------------------------------------------
# Schema overlap — do two tools accept the same required parameter set?
# A pair that both require ``(schema, table)`` and nothing else is a
# strong family-cluster signal even if the names look distinct.
# ---------------------------------------------------------------------------


def _required_params(schema: dict[str, Any]) -> tuple[str, ...]:
    required = schema.get("required") or []
    return tuple(sorted(required))


def _input_param_set(schema: dict[str, Any]) -> frozenset[str]:
    props = schema.get("properties") or {}
    return frozenset(props.keys())


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _classify_pair(
    name_sim: float, jaccard: float, containment: float, shared_verb_noun: bool, same_required: bool
) -> str:
    """One-line label summarising why the pair was flagged."""
    bits: list[str] = []
    if name_sim >= NAME_SIMILARITY_MIN:
        bits.append(f"name {name_sim:.2f}")
    if jaccard >= DESC_JACCARD_MIN:
        bits.append(f"desc J={jaccard:.2f}")
    if containment >= DESC_OVERLAP_MIN:
        bits.append(f"desc ⊃={containment:.2f}")
    if shared_verb_noun:
        bits.append("verb+noun")
    if same_required:
        bits.append("same req-params")
    return ", ".join(bits) if bits else "—"


def _pair_score(
    name_sim: float, jaccard: float, containment: float, shared_verb_noun: bool, same_required: bool
) -> float:
    """Combined score for ranking. Hand-tuned weights.

    The components are non-redundant: name similarity tells us "the
    names look alike"; description Jaccard / containment tell us
    "the descriptions cover the same ground"; verb+noun tells us
    "the names imply the same intent"; same_required tells us "the
    tools take the same call shape." Adding them gives a rough
    "how confusing is this pair?" score that bunches genuine
    duplicates near the top.
    """
    return (
        2.0 * name_sim
        + 1.5 * jaccard
        + 1.5 * containment
        + (0.5 if shared_verb_noun else 0.0)
        + (0.5 if same_required else 0.0)
    )


def main() -> int:
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    tools: list[dict[str, Any]] = snapshot["tools"]

    # Pre-compute the per-tool features.
    features = []
    for tool in tools:
        name = tool["name"]
        desc = tool.get("description") or ""
        schema = tool.get("inputSchema") or {}
        features.append(
            {
                "name": name,
                "desc": desc,
                "name_tokens": _tokenise_name(name),
                "desc_tokens": _tokenise_description(desc),
                "required": _required_params(schema),
                "param_set": _input_param_set(schema),
            }
        )

    # Pairwise scan. O(n^2) is fine at 173 tools.
    flagged: list[dict[str, Any]] = []
    for i in range(len(features)):
        for j in range(i + 1, len(features)):
            a, b = features[i], features[j]
            name_sim = _name_similarity(a["name"], b["name"])
            jaccard, containment = _description_overlap(a["desc_tokens"], b["desc_tokens"])
            shared_vn = _shared_verb_and_noun(a["name_tokens"], b["name_tokens"])
            same_required = bool(a["required"]) and a["required"] == b["required"]

            flagged_now = (
                name_sim >= NAME_SIMILARITY_MIN
                or jaccard >= DESC_JACCARD_MIN
                or containment >= DESC_OVERLAP_MIN
                or (SHARED_VERB_AND_NOUN and shared_vn)
            )
            if not flagged_now:
                continue

            flagged.append(
                {
                    "a": a["name"],
                    "b": b["name"],
                    "name_sim": name_sim,
                    "jaccard": jaccard,
                    "containment": containment,
                    "shared_vn": shared_vn,
                    "same_required": same_required,
                    "score": _pair_score(name_sim, jaccard, containment, shared_vn, same_required),
                    "a_desc": a["desc"],
                    "b_desc": b["desc"],
                    "required": a["required"] if same_required else None,
                }
            )

    flagged.sort(key=lambda x: -x["score"])

    # Per-verb-stem family map — for the operator who wants to see
    # "all tools whose first token is `analyze_`" in one place.
    families: dict[str, list[str]] = defaultdict(list)
    for f in features:
        if f["name_tokens"]:
            families[f["name_tokens"][0]].append(f["name"])
    families = {k: sorted(v) for k, v in families.items() if len(v) >= 3}

    # ----- render ----------------------------------------------------------

    out: list[str] = []
    out.append("# MCPg tool-surface overlap report")
    out.append("")
    out.append(
        f"_Generated from `tests/contract/tool_surface.snapshot.json` ({snapshot['_meta']['tool_count']} tools)._"
    )
    out.append("")
    out.append("## What this is")
    out.append("")
    out.append(
        "A triage aid. Pairs below are surfaced by a hand-tuned similarity "
        "scoring formula; **none of them are automatically wrong**. A reviewer "
        "decides per pair whether to consolidate, rename, or tighten "
        "descriptions for clarity. Many flagged pairs will be legitimate "
        "siblings (e.g. a read variant and a write variant of the same "
        "operation) and should stay distinct."
    )
    out.append("")
    out.append("## Scoring formula")
    out.append("")
    out.append(
        "Each pair gets a score combining: SequenceMatcher ratio on names "
        f"(weight 2.0, flag ≥ {NAME_SIMILARITY_MIN}), Jaccard of description "
        f"stems (weight 1.5, flag ≥ {DESC_JACCARD_MIN}), max-containment of "
        f"description stems (weight 1.5, flag ≥ {DESC_OVERLAP_MIN}), shared "
        "leading verb + trailing noun (weight 0.5), identical required-param "
        "tuple (weight 0.5). Higher = more likely to confuse an LLM picker."
    )
    out.append("")
    out.append(f"**{len(flagged)} flagged pairs** ranked by score.")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Top flagged pairs")
    out.append("")

    for idx, pair in enumerate(flagged, 1):
        out.append(f"### {idx}. `{pair['a']}` ↔ `{pair['b']}`  (score {pair['score']:.2f})")
        out.append("")
        classification = _classify_pair(
            pair["name_sim"],
            pair["jaccard"],
            pair["containment"],
            pair["shared_vn"],
            pair["same_required"],
        )
        out.append(f"_{classification}_")
        out.append("")
        out.append(f"- **{pair['a']}** — {(pair['a_desc'] or '_(no description)_').strip()[:240]}")
        out.append(f"- **{pair['b']}** — {(pair['b_desc'] or '_(no description)_').strip()[:240]}")
        if pair["same_required"] and pair["required"]:
            out.append(f"- _Same required params:_ `{list(pair['required'])}`")
        out.append("")

    # ----- families --------------------------------------------------------

    out.append("---")
    out.append("")
    out.append("## Verb-prefix families (3+ tools)")
    out.append("")
    out.append(
        "Cluster of tools sharing a leading verb stem. Useful for spotting "
        "places where the catalogue has grown organically into a series and "
        "where a single dispatching tool with a `mode=` argument might be "
        "cleaner — or, conversely, where a family is already coherent and "
        "needs no change."
    )
    out.append("")
    for verb in sorted(families):
        tools_in_family = families[verb]
        out.append(f"### `{verb}_*`  ({len(tools_in_family)} tools)")
        out.append("")
        for t in tools_in_family:
            out.append(f"- `{t}`")
        out.append("")

    # ----- name-token frequency -------------------------------------------

    out.append("---")
    out.append("")
    out.append("## Name-token frequency")
    out.append("")
    out.append(
        "Most common stems across all tool names. High counts hint at "
        "namespace congestion — if a stem appears in 15 names, every new "
        "tool that uses it inherits the confusion."
    )
    out.append("")
    counter: Counter[str] = Counter()
    for f in features:
        counter.update(f["name_tokens"])
    out.append("| Stem | Count | Example tools |")
    out.append("|---|---:|---|")
    for stem, count in counter.most_common(30):
        examples = [f["name"] for f in features if stem in f["name_tokens"]][:3]
        out.append(f"| `{stem}` | {count} | {', '.join(f'`{e}`' for e in examples)} |")
    out.append("")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
