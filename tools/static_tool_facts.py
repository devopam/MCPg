"""Tool-surface fact-pack generator (Phase A — static observation).

Produces a single Markdown report covering everything we can know
about MCPg's tool surface *without* talking to an LLM:

* Catalog shape — counts by verb prefix, description-length and
  parameter-count distributions, schema depth and required-vs-optional
  split.
* Description quality heuristics — empty / short / name-restating /
  missing-security-caveat / missing-return-hint descriptions.
* Token-budget cost — char-based estimate of how much of an LLM's
  context window the catalogue eats, per-tool and aggregate.
* Self-introspection inventory — what an MCP client sees on
  first connect *beyond* ``tools/list``: server info, MCP
  resources, MCP prompts, the ``get_server_info`` tool's output
  shape.

The report is the input for Phase B (LLM behaviour observation).
Where Phase A flags "this description is just the name in English,"
Phase B should ask the LLM to disambiguate that tool from its
siblings and see whether the LLM can — that's how we tie the static
facts to actual observed behaviour.

Run with:

    python tools/static_tool_facts.py > docs/reviews/tool-surface-fact-pack.md

The analyser is read-only — it never mutates the snapshot or
source tree. The cost approximation (char_count / 4) is a coarse
proxy for tokens; ratios between tools are reliable, absolute
numbers are within ~15% of tiktoken-cl100k for English content.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT_PATH = _REPO_ROOT / "tests" / "contract" / "tool_surface.snapshot.json"
_TOOLS_SOURCE = _REPO_ROOT / "src" / "mcpg" / "tools.py"
_SERVER_SOURCE = _REPO_ROOT / "src" / "mcpg" / "server.py"

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def _approx_tokens(text: str) -> int:
    """Character-count heuristic. Within ~15% of tiktoken for English
    prose; ratios between tools are reliable. Avoids adding a tiktoken
    dev dep.
    """
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def _tool_token_cost(tool: dict[str, Any]) -> dict[str, int]:
    """Per-tool token budget, broken into name / description / schema
    so we can see where the cost lives."""
    name_t = _approx_tokens(tool["name"])
    desc_t = _approx_tokens(tool.get("description") or "")
    schema_t = _approx_tokens(json.dumps(tool.get("inputSchema") or {}))
    return {
        "name": name_t,
        "description": desc_t,
        "schema": schema_t,
        "total": name_t + desc_t + schema_t,
    }


# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


def _verb_prefix(name: str) -> str:
    """First underscore-separated token; the operational verb."""
    return name.split("_", 1)[0] if "_" in name else name


def _schema_param_summary(schema: dict[str, Any]) -> dict[str, Any]:
    """Per-tool input-schema descriptive stats."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    optional = set(props) - required

    # Schema "depth" — how many levels of nested objects appear.
    # A flat shape is depth=1; one level of nesting = depth=2; etc.
    def _depth(node: Any) -> int:
        if not isinstance(node, dict):
            return 0
        if "properties" in node:
            kids = node.get("properties") or {}
            child_max = max((_depth(v) for v in kids.values()), default=0)
            return 1 + child_max
        if "items" in node:
            return _depth(node.get("items"))
        return 0

    depth = _depth(schema)
    return {
        "param_count": len(props),
        "required_count": len(required),
        "optional_count": len(optional),
        "depth": depth,
    }


# ---------------------------------------------------------------------------
# Description quality heuristics
# ---------------------------------------------------------------------------


# Recognises "<verb> the <name fragments>" patterns where the
# description is just the tool name rephrased. e.g. name=list_tables,
# desc="List the tables." → flagged.
def _name_restates_check(name: str, description: str) -> bool:
    """Approximate: does the description add ≥2 distinct content
    words beyond what's in the name?"""
    if not description:
        return False  # empty is its own bucket
    desc_lower = description.lower()
    name_words = {w for w in re.split(r"[_\-]+", name.lower()) if len(w) > 2}
    # Stop-word filter for description side.
    desc_words = {w for w in re.findall(r"[a-z][a-z0-9]+", desc_lower) if len(w) > 2}
    novel = (
        desc_words
        - name_words
        - {
            "the",
            "and",
            "for",
            "with",
            "this",
            "from",
            "into",
            "all",
            "any",
            "via",
            "use",
            "uses",
            "used",
            "you",
            "your",
            "tool",
        }
    )
    return len(novel) < 3


# Tools that touch a gated surface should say so in their description
# so the LLM knows when to suggest the operator enable a flag.
_GATED_NAME_PATTERNS = [
    ("write", "MCPG_ACCESS_MODE"),
    ("update", "MCPG_ACCESS_MODE"),
    ("delete", "MCPG_ACCESS_MODE"),
    ("create_", "DDL"),
    ("drop_", "DDL"),
    ("alter_", "DDL"),
    ("reindex", "DDL"),
    ("vacuum", "MCPG_ACCESS_MODE"),
    ("dump", "shell"),
    ("restore", "shell"),
    ("psql", "shell"),
    ("listen", "MCPG_ALLOW_LISTEN"),
]


def _missing_security_caveat(name: str, description: str) -> str | None:
    """Return the security-flag name a tool *should* mention given
    its name, or ``None`` if no caveat is expected (or one is present)."""
    desc_lower = (description or "").lower()
    for needle, flag in _GATED_NAME_PATTERNS:
        if needle not in name:
            continue
        # Loose presence check — variants of how we mention the flag.
        if any(
            token in desc_lower
            for token in (
                flag.lower(),
                "unrestricted",
                "allow_ddl",
                "allow_shell",
                "allow_listen",
                "ddl",
            )
        ):
            return None
        return flag
    return None


_RETURN_HINT_KEYWORDS = (
    "return",
    "returns",
    "yields",
    "emits",
    "produces",
    "list of",
    "list with",
    "object with",
    "dict with",
    "rows",
    "result",
    "output",
    "responds with",
    "writes",
    "drops",
    "creates",
)


def _has_return_hint(description: str) -> bool:
    """Does the description say *anything* about what the tool returns?"""
    if not description:
        return False
    lower = description.lower()
    return any(k in lower for k in _RETURN_HINT_KEYWORDS)


# ---------------------------------------------------------------------------
# Self-introspection inventory (source-code probe)
# ---------------------------------------------------------------------------


def _find_get_server_info_body() -> str:
    """Read the get_server_info tool's body from tools.py so we can
    see what an MCP client gets when it asks 'what is this server'."""
    if not _TOOLS_SOURCE.exists():
        return "(source not found)"
    text = _TOOLS_SOURCE.read_text(encoding="utf-8")
    match = re.search(
        r"async def get_server_info[^\n]*\n((?:    [^\n]*\n|\n)+)",
        text,
    )
    return match.group(0).rstrip() if match else "(get_server_info not found)"


def _list_mcp_resource_decorators() -> list[str]:
    """Find any ``@server.resource(...)`` registrations in the server
    source. MCP servers can expose resources alongside tools; if mcpg
    doesn't expose any, that's a data point."""
    if not _SERVER_SOURCE.exists():
        return []
    text = _SERVER_SOURCE.read_text(encoding="utf-8")
    return re.findall(r"@\w+\.resource\([^)]*\)", text)


def _list_mcp_prompt_decorators() -> list[str]:
    if not _SERVER_SOURCE.exists():
        return []
    text = _SERVER_SOURCE.read_text(encoding="utf-8")
    return re.findall(r"@\w+\.prompt\([^)]*\)", text)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _hist_bins(values: list[int], edges: list[int]) -> list[tuple[str, int]]:
    """Render a histogram for an int distribution against operator-friendly
    bin edges. Returns list of (label, count)."""
    counts: list[int] = [0] * (len(edges) + 1)
    for v in values:
        placed = False
        for i, edge in enumerate(edges):
            if v <= edge:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    labels = []
    last = -1
    for edge in edges:
        labels.append(f"{last + 1}..{edge}")
        last = edge
    labels.append(f"{last + 1}+")
    return list(zip(labels, counts, strict=True))


def main() -> int:
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    tools: list[dict[str, Any]] = snapshot["tools"]
    n = len(tools)

    # ---- catalog shape -----------------------------------------------------
    verb_counts: Counter[str] = Counter(_verb_prefix(t["name"]) for t in tools)
    desc_lengths = [len(t.get("description") or "") for t in tools]
    param_summaries = [_schema_param_summary(t.get("inputSchema") or {}) for t in tools]
    param_counts = [p["param_count"] for p in param_summaries]
    required_counts = [p["required_count"] for p in param_summaries]
    depths = [p["depth"] for p in param_summaries]

    # ---- description quality ----------------------------------------------
    empty_desc = [t["name"] for t in tools if not (t.get("description") or "").strip()]
    short_desc = [t["name"] for t in tools if 0 < len(t.get("description") or "") < 50]
    name_restating = [t["name"] for t in tools if _name_restates_check(t["name"], t.get("description") or "")]
    missing_caveat = [(t["name"], _missing_security_caveat(t["name"], t.get("description") or "")) for t in tools]
    missing_caveat = [(n, f) for (n, f) in missing_caveat if f is not None]
    no_return_hint = [t["name"] for t in tools if not _has_return_hint(t.get("description") or "")]

    # ---- token cost -------------------------------------------------------
    per_tool_tokens = []
    for t in tools:
        cost = _tool_token_cost(t)
        per_tool_tokens.append((t["name"], cost))
    per_tool_tokens.sort(key=lambda x: -x[1]["total"])
    total_tokens = sum(c[1]["total"] for c in per_tool_tokens)
    total_name = sum(c[1]["name"] for c in per_tool_tokens)
    total_desc = sum(c[1]["description"] for c in per_tool_tokens)
    total_schema = sum(c[1]["schema"] for c in per_tool_tokens)

    # ---- self-introspection inventory -------------------------------------
    server_info_body = _find_get_server_info_body()
    resource_decorators = _list_mcp_resource_decorators()
    prompt_decorators = _list_mcp_prompt_decorators()

    # ---- render -----------------------------------------------------------
    out: list[str] = []
    out.append("# MCPg tool-surface fact pack (Phase A — static observation)")
    out.append("")
    out.append(
        "_Generated from `tests/contract/tool_surface.snapshot.json` "
        f"({n} tools) and the v0.6.2 source tree. Read-only; "
        "no MCPg code changes. Token counts are char/4 estimates "
        "(±~15% vs tiktoken)._"
    )
    out.append("")
    out.append("---")
    out.append("")

    # --- catalog shape -----------------------------------------------------
    out.append("## 1. Catalog shape")
    out.append("")
    out.append(f"- Total tools registered: **{n}**")
    out.append(f"- Distinct verb prefixes: **{len(verb_counts)}**")
    out.append(
        f"- Description length — median **{statistics.median(desc_lengths)} chars**, "
        f"p90 **{int(statistics.quantiles(desc_lengths, n=10)[-1])}**, "
        f"max **{max(desc_lengths)}**, min **{min(desc_lengths)}**"
    )
    out.append(
        f"- Parameter count — median **{statistics.median(param_counts)}**, "
        f"p90 **{int(statistics.quantiles(param_counts, n=10)[-1])}**, "
        f"max **{max(param_counts)}**"
    )
    out.append(
        f"- Required parameter count — median **{statistics.median(required_counts)}**, "
        f"p90 **{int(statistics.quantiles(required_counts, n=10)[-1])}**, "
        f"max **{max(required_counts)}**"
    )
    out.append(
        f"- Schema depth (0 = no params; 1 = flat) — median **{statistics.median(depths)}**, max **{max(depths)}**"
    )
    out.append("")

    out.append("### 1a. Verb-prefix breakdown")
    out.append("")
    out.append("| Verb prefix | Tool count | % of catalog |")
    out.append("|---|---:|---:|")
    for verb, count in verb_counts.most_common():
        out.append(f"| `{verb}_*` | {count} | {100 * count / n:.1f}% |")
    out.append("")

    out.append("### 1b. Description-length histogram")
    out.append("")
    out.append("| Length (chars) | Tools |")
    out.append("|---|---:|")
    for label, count in _hist_bins(desc_lengths, [0, 50, 100, 200, 400, 800]):
        out.append(f"| {label} | {count} |")
    out.append("")

    out.append("### 1c. Parameter-count histogram")
    out.append("")
    out.append("| Parameters | Tools |")
    out.append("|---|---:|")
    for label, count in _hist_bins(param_counts, [0, 1, 2, 3, 5, 8]):
        out.append(f"| {label} | {count} |")
    out.append("")

    # --- description quality ----------------------------------------------
    out.append("## 2. Description quality heuristics")
    out.append("")
    out.append(f"- **Empty descriptions:** {len(empty_desc)} / {n} ({100 * len(empty_desc) / n:.1f}%)")
    out.append(f"- **Very short (<50 chars, non-empty):** {len(short_desc)} / {n} ({100 * len(short_desc) / n:.1f}%)")
    out.append(
        f"- **Name-restating (description adds <3 novel content words):** "
        f"{len(name_restating)} / {n} ({100 * len(name_restating) / n:.1f}%)"
    )
    out.append(
        f"- **Gated tools missing security caveat in description:** "
        f"{len(missing_caveat)} / {n} ({100 * len(missing_caveat) / n:.1f}%)"
    )
    out.append(
        f"- **No return-shape hint in description:** {len(no_return_hint)} / {n} ({100 * len(no_return_hint) / n:.1f}%)"
    )
    out.append("")

    if empty_desc:
        out.append("### 2a. Tools with empty descriptions")
        out.append("")
        for name in sorted(empty_desc):
            out.append(f"- `{name}`")
        out.append("")

    if short_desc:
        out.append("### 2b. Tools with very short descriptions (<50 chars)")
        out.append("")
        for name in sorted(short_desc):
            tool = next(t for t in tools if t["name"] == name)
            out.append(f"- `{name}` — _{(tool.get('description') or '').strip()}_")
        out.append("")

    if name_restating:
        out.append("### 2c. Tools whose description appears to just restate the name")
        out.append("")
        out.append(
            "_Flagged when the description contains <3 distinct content "
            "words beyond what's already in the tool name. Low-signal "
            "descriptions cost the LLM tokens without adding picking signal._"
        )
        out.append("")
        for name in sorted(name_restating):
            tool = next(t for t in tools if t["name"] == name)
            desc = (tool.get("description") or "").strip()
            out.append(f"- `{name}` — _{desc[:160]}{'…' if len(desc) > 160 else ''}_")
        out.append("")

    if missing_caveat:
        out.append("### 2d. Gated tools without a security caveat")
        out.append("")
        out.append(
            "_Tools whose name implies a gated operation (write / DDL / "
            "shell / listen) but whose description doesn't mention which "
            "MCPG_ACCESS_MODE or MCPG_ALLOW_* flag is required. An LLM "
            "that can't see this from the description can't suggest "
            "'ask the operator to enable allow_ddl' when the call fails._"
        )
        out.append("")
        for name, flag in sorted(missing_caveat):
            out.append(f"- `{name}` — expected flag mention: **{flag}**")
        out.append("")

    out.append("### 2e. Tools with no return-shape hint")
    out.append("")
    out.append(
        f"_{len(no_return_hint)} tools whose description never says what they return "
        "(no 'returns', 'yields', 'list of', 'object with', 'rows', etc.). "
        "An LLM picker often needs to know the shape to decide whether to "
        "call this tool or another that returns a more directly-usable shape._"
    )
    out.append("")
    out.append("Top 20 by alphabetical order (full list elided to keep the report readable):")
    out.append("")
    for name in sorted(no_return_hint)[:20]:
        out.append(f"- `{name}`")
    if len(no_return_hint) > 20:
        out.append(f"- _…and {len(no_return_hint) - 20} more._")
    out.append("")

    # --- token cost -------------------------------------------------------
    out.append("## 3. Token-budget cost")
    out.append("")
    out.append(
        f"Total catalogue cost when surfaced to an LLM (single `tools/list` "
        f"response, approximate): **~{total_tokens:,} tokens**."
    )
    out.append("")
    out.append("Per-component breakdown:")
    out.append("")
    out.append("| Component | Tokens | % of total |")
    out.append("|---|---:|---:|")
    out.append(f"| Names | {total_name:,} | {100 * total_name / total_tokens:.1f}% |")
    out.append(f"| Descriptions | {total_desc:,} | {100 * total_desc / total_tokens:.1f}% |")
    out.append(f"| Input schemas | {total_schema:,} | {100 * total_schema / total_tokens:.1f}% |")
    out.append(f"| **Total** | **{total_tokens:,}** | 100% |")
    out.append("")

    out.append("### 3a. Top 15 most-expensive tools (token budget)")
    out.append("")
    out.append("| Tool | Total | Name | Desc | Schema |")
    out.append("|---|---:|---:|---:|---:|")
    for name, cost in per_tool_tokens[:15]:
        out.append(f"| `{name}` | {cost['total']} | {cost['name']} | {cost['description']} | {cost['schema']} |")
    out.append("")

    out.append("### 3b. Bottom 15 cheapest tools")
    out.append("")
    out.append("| Tool | Total |")
    out.append("|---|---:|")
    for name, cost in per_tool_tokens[-15:]:
        out.append(f"| `{name}` | {cost['total']} |")
    out.append("")

    out.append("### 3c. Context-window context")
    out.append("")
    out.append(
        f"At ~{total_tokens:,} tokens, the full mcpg catalogue is "
        f"{100 * total_tokens / 200_000:.1f}% of a Claude 200k context window, "
        f"{100 * total_tokens / 128_000:.1f}% of a 128k window, and "
        f"{100 * total_tokens / 32_000:.1f}% of a 32k window. The cost is fixed "
        f"per turn — every conversation pays it for every request as long as "
        f"the LLM holds the catalogue in context."
    )
    out.append("")

    # --- self-introspection inventory --------------------------------------
    out.append("## 4. Self-introspection inventory")
    out.append("")
    out.append(
        "What an MCP client (Claude Desktop, Cursor, an automation agent) "
        "sees when it first connects to mcpg, *beyond* the `tools/list` "
        "response covered by sections 1-3."
    )
    out.append("")

    out.append(f"- **MCP resources exposed:** {len(resource_decorators)}")
    if resource_decorators:
        out.append("")
        for d in resource_decorators:
            out.append(f"  - `{d}`")
    out.append("")
    out.append(f"- **MCP prompts exposed:** {len(prompt_decorators)}")
    if prompt_decorators:
        out.append("")
        for d in prompt_decorators:
            out.append(f"  - `{d}`")
    out.append("")

    has_about_tool = any(t["name"] in {"about", "describe_self", "capabilities", "mcpg_about"} for t in tools)
    out.append(
        f"- **Dedicated self-description tool present** "
        f"(`about` / `capabilities` / `describe_self`): "
        f"{'yes' if has_about_tool else 'no'}"
    )
    out.append("")
    out.append("- **`get_server_info` tool body (current implementation):**")
    out.append("")
    out.append("```python")
    out.append(server_info_body)
    out.append("```")
    out.append("")

    # --- crosscutting summary ---------------------------------------------
    out.append("---")
    out.append("")
    out.append("## 5. Cross-cutting observations")
    out.append("")
    out.append("Interpretations the raw stats above point at — not verdicts, just leads worth Phase-B verification.")
    out.append("")
    obs: list[str] = []

    pct_empty = 100 * len(empty_desc) / n
    pct_short = 100 * len(short_desc) / n
    pct_restate = 100 * len(name_restating) / n
    if pct_empty + pct_short + pct_restate > 25:
        obs.append(
            f"Description quality is the biggest static lever: "
            f"{pct_empty + pct_short + pct_restate:.0f}% of tools have descriptions "
            "that are empty, very short, or essentially restate the name. "
            "An LLM picker treats these as low-signal noise."
        )
    pct_no_return = 100 * len(no_return_hint) / n
    if pct_no_return > 30:
        obs.append(
            f"{pct_no_return:.0f}% of tools never describe what they return. "
            "The LLM has to call them speculatively to find out, which wastes "
            "turns when the return shape is wrong for the task."
        )
    if missing_caveat:
        obs.append(
            f"{len(missing_caveat)} gated tools (write / DDL / shell / listen) "
            "don't mention their required MCPG_ACCESS_MODE / MCPG_ALLOW_* flag "
            "in the description. An LLM seeing a permission denial can't tell "
            "the operator which flag to flip."
        )
    if not resource_decorators and not prompt_decorators:
        obs.append(
            "mcpg exposes **zero MCP resources and zero MCP prompts**. "
            "When Claude Chat or Cursor asks 'what is this server / what can "
            "it do' beyond just listing tools, there is no structured "
            "answer to surface. Self-introspection currently relies on the "
            "client reading every tool description and synthesising."
        )
    if not has_about_tool:
        obs.append(
            "No dedicated `about` / `capabilities` / `describe_self` tool. "
            "The closest is `get_server_info`, which mostly returns server "
            "config / version. There is no tool an LLM can call to get "
            "a human-readable answer to 'what does mcpg do?' without "
            "ingesting the full 173-tool catalogue."
        )

    if obs:
        for line in obs:
            out.append(f"- {line}")
    else:
        out.append("_(none — the static signal is clean.)_")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## 6. Hand-off to Phase B")
    out.append("")
    out.append(
        "Phase A surfaces leads; Phase B (LLM behaviour observation) "
        "confirms or refutes them. The Phase-B prompt corpus should "
        "include at least:"
    )
    out.append("")
    out.append(
        "1. **Picker-confusion probes** for the top-scoring overlap pairs "
        "from `tool-overlap-report.md` (Phase A.0). Ask the LLM to "
        "disambiguate them; if it can't, the static flag is real."
    )
    out.append(
        "2. **Permission-denial probes** for the `missing security caveat` "
        "list. Simulate a denial; see whether the LLM can suggest the "
        "right `MCPG_*` flag."
    )
    out.append(
        "3. **Return-shape probes** for the `no return hint` list. Ask "
        "the LLM what it expects the tool to return; compare to reality."
    )
    out.append(
        "4. **Self-introspection probes** — 'What can this MCP server do?' "
        "/ 'List mcpg's capabilities' / 'Can mcpg do X?' Measure whether "
        "the response is accurate, complete, and useful."
    )
    out.append("")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
