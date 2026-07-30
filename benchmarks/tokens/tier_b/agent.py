"""The Tier-B agent loop — one task, one arm, through a real MCP session.

Drives the model in a tool-use loop against a live MCPg server (over MCP), and
records what the study measures: input/output tokens, turns (model calls),
tool-calls, the full transcript, and whether the final answer is correct.

The two **arms** differ only in which tools the model is *told about*:

- ``mcpg`` — the purpose-built surface (index / sensitive-column / naming
  advisors, compact schema, plan analysis, …).
- ``baseline`` — a lone ``run_select``: the agent must write raw SQL and
  interpret the rows itself.

Same server, same database, same model, same task — so the difference in cost is
attributable to the tool surface. Not unit-tested (needs a model + a live DB);
the pure helpers it leans on (tasks graders, schema aggregation) are.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from benchmarks.tokens.tier_b.schema import TrialResult

if TYPE_CHECKING:
    from mcp import ClientSession

    from benchmarks.tokens.tier_b.model import ModelClient
    from benchmarks.tokens.tier_b.tasks import Task

_SYSTEM = (
    "You are a meticulous PostgreSQL database assistant. Use the available tools to investigate the "
    "database and answer the user's question. Prefer the most direct tool for the job. When you are "
    "confident, reply with a final plain-text answer and no further tool call, stating the specific "
    "table/column names involved."
)


def _anthropic_tools(mcp_tools: list[Any], allowed: set[str]) -> list[dict[str, Any]]:
    """Convert the MCP tool list to Anthropic tool defs, filtered to ``allowed``."""
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
        for t in mcp_tools
        if t.name in allowed
    ]


def _text_of(content: list[dict[str, Any]]) -> str:
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


def _tool_result_text(result: Any) -> str:
    """Serialize an MCP tool result to the text the model sees (structured JSON preferred)."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return json.dumps(structured, default=str)
    blocks = getattr(result, "content", None) or []
    return "\n".join(getattr(b, "text", "") for b in blocks) or "(no output)"


# Tools that make their own internal LLM call, separate from the outer agent
# loop this file drives — that internal call's tokens are otherwise invisible
# to this study (they never pass through model.complete()). translate_nl_to_sql
# is the only one today; it reports usage via TranslationResult.tokens_in/out
# (see src/mcpg/nl2sql.py), surfaced here in the tool result's structuredContent.
_TOOLS_WITH_HIDDEN_LLM_COST = {"translate_nl_to_sql"}


def _hidden_tokens(tool_name: str, result: Any) -> tuple[int, int]:
    """The tokens ``tool_name``'s own internal LLM call spent, or (0, 0)."""
    if tool_name not in _TOOLS_WITH_HIDDEN_LLM_COST:
        return 0, 0
    structured = getattr(result, "structuredContent", None)
    if not isinstance(structured, dict):
        return 0, 0
    return int(structured.get("tokens_in") or 0), int(structured.get("tokens_out") or 0)


async def run_trial(
    task: Task,
    *,
    session: ClientSession,
    model: ModelClient,
    allowed_tools: set[str],
    arm: str,
    trial: int,
    max_turns: int = 12,
) -> TrialResult:
    """Run one (task, arm) trial to completion; return its measured cost + outcome."""
    listed = await session.list_tools()
    tools = _anthropic_tools(listed.tools, allowed_tools)
    messages: list[dict[str, Any]] = [{"role": "user", "content": task.prompt}]
    tokens_in = tokens_out = turns = tool_calls = 0
    hidden_tokens_in = hidden_tokens_out = 0
    final = ""
    try:
        for _ in range(max_turns):
            resp = await model.complete(_SYSTEM, messages, tools)
            turns += 1
            tokens_in += resp.input_tokens
            tokens_out += resp.output_tokens
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
            if resp.stop_reason == "tool_use" and tool_uses:
                results: list[dict[str, Any]] = []
                for tu in tool_uses:
                    tool_calls += 1
                    out = await session.call_tool(tu["name"], tu.get("input") or {})
                    hin, hout = _hidden_tokens(tu["name"], out)
                    hidden_tokens_in += hin
                    hidden_tokens_out += hout
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": _tool_result_text(out)})
                messages.append({"role": "user", "content": results})
            else:
                final = _text_of(resp.content)
                break
        return TrialResult(
            task_id=task.id,
            arm=arm,
            trial=trial,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            turns=turns,
            tool_calls=tool_calls,
            passed=task.grade(final),
            final_answer=final,
            hidden_tokens_in=hidden_tokens_in,
            hidden_tokens_out=hidden_tokens_out,
        )
    except Exception as exc:
        return TrialResult(
            task_id=task.id,
            arm=arm,
            trial=trial,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            turns=turns,
            tool_calls=tool_calls,
            passed=False,
            final_answer=final,
            error=str(exc),
            hidden_tokens_in=hidden_tokens_in,
            hidden_tokens_out=hidden_tokens_out,
        )
