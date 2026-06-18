"""Phase B — LLM behaviour observation harness.

For each prompt in `docs/reviews/phase-b-prompts.json`, this harness:

1. Loads the MCPg tool catalogue from the contract snapshot.
2. Sends it as the `tools=` parameter on an Anthropic Messages API
   call (using native MCP-style tool calling — the same surface a
   real Claude Desktop session sees).
3. Records exactly what the LLM did: which tool it picked, what
   arguments it proposed, what text it generated, how many tokens
   it cost.
4. Appends a JSONL line to the output file (default
   `docs/reviews/phase-b-observations.jsonl`).

Why Anthropic specifically: the conversation has been Claude-centric
and Claude is the primary MCPg target. The harness is structured so
adding OpenAI / Gemini providers is a one-class swap if you ever
want a comparison run, but for our first cut we want Claude's
behaviour because that's what users will see.

Usage::

    export ANTHROPIC_API_KEY=...
    python tools/observe_llm_behaviour.py \\
        --model claude-sonnet-4-6 \\
        --output docs/reviews/phase-b-observations.jsonl

    # Filter to one category if iterating:
    python tools/observe_llm_behaviour.py \\
        --category self_introspection

    # Reduce scope while validating the harness:
    python tools/observe_llm_behaviour.py --limit 3

The harness is deterministic per prompt id (uses temperature=0 +
stable system prompt). Re-running on the same corpus produces the
same observations within Anthropic's tool-use sampling tolerance.

**Cost estimate**: 22 prompts x ~28k tokens of tools + ~300 tokens
of prompt + ~500 tokens of response ≈ ~640k input tokens + ~11k
output tokens per full run. At Sonnet 4.x pricing (~$3/M input,
~$15/M output) that's roughly $2.10 per run before prompt caching
(which Anthropic enables automatically on tools — bringing input
cost down to ~10% on warm runs).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT_PATH = _REPO_ROOT / "tests" / "contract" / "tool_surface.snapshot.json"
_CORPUS_PATH = _REPO_ROOT / "docs" / "reviews" / "phase-b-prompts.json"
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "reviews" / "phase-b-observations.jsonl"

_SYSTEM_PROMPT = (
    "You are an AI agent connected to mcpg — a Model Context Protocol "
    "(MCP) server that exposes PostgreSQL capabilities to LLMs. You "
    "have access to mcpg's full tool catalogue via the `tools=` parameter "
    "on this conversation.\n\n"
    "For each user request, decide whether to call a tool, ask a "
    "clarifying question, or answer from your own knowledge of the "
    "tool catalogue. When the user asks 'what can mcpg do?' or similar "
    "introspection questions, answer in plain prose based on the "
    "available tools — do not just dump the tool list."
)


# ---------------------------------------------------------------------------
# Tool catalogue → Anthropic tool-use format
# ---------------------------------------------------------------------------


def _load_tool_catalogue() -> list[dict[str, Any]]:
    """Map the snapshot record shape into Anthropic's tool-use shape.

    Snapshot has: ``name``, ``description``, ``inputSchema``.
    Anthropic wants: ``name``, ``description``, ``input_schema``.
    """
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["inputSchema"],
        }
        for t in snapshot["tools"]
    ]


# ---------------------------------------------------------------------------
# Anthropic API call
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """One row of evidence for the report."""

    prompt_id: str
    category: str
    phase_a_reference: str
    user_prompt: str
    expected_tool: str | None
    expected_no_tool_call: bool
    rationale: str
    # Captured response:
    picked_tool: str | None
    picked_args: dict[str, Any] | None
    text_response: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    # Derived correctness:
    matched_expected: bool | None
    # Provider plumbing
    provider: str
    model: str
    timestamp: str
    raw_error: str | None = None
    raw_blocks: list[dict[str, Any]] = field(default_factory=list)


async def _call_anthropic(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict[str, Any]],
    max_tokens: int,
    timeout: float,
) -> tuple[dict[str, Any], int]:
    """Single Messages API call. Returns ``(response_json, latency_ms)``."""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "system": system_prompt,
                "tools": tools,
                "messages": [{"role": "user", "content": user_prompt}],
            },
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    response.raise_for_status()
    return response.json(), latency_ms


def _parse_response(body: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, str, list[dict[str, Any]]]:
    """Extract (picked_tool_name, picked_args, text, raw_blocks) from
    the Anthropic Messages response."""
    picked_tool: str | None = None
    picked_args: dict[str, Any] | None = None
    text_parts: list[str] = []
    raw_blocks: list[dict[str, Any]] = []
    for block in body.get("content") or []:
        raw_blocks.append(block)
        if block.get("type") == "tool_use":
            # First tool_use wins for the observation; multi-call chains
            # are interesting but rare on these single-turn probes.
            if picked_tool is None:
                picked_tool = block.get("name")
                picked_args = block.get("input")
        elif block.get("type") == "text":
            text_parts.append(block.get("text", ""))
    return picked_tool, picked_args, "\n\n".join(text_parts).strip(), raw_blocks


def _judge_match(prompt: dict[str, Any], picked_tool: str | None) -> bool | None:
    """Has the LLM produced the expected behaviour?

    * For tool-picker prompts (``expected_tool`` set): must pick that tool.
    * For "no tool call expected" prompts (``expected_no_tool_call`` true):
      must NOT pick any tool.
    * Otherwise: undecidable (return None).
    """
    expected_tool = prompt.get("expected_tool")
    expected_no_call = prompt.get("expected_no_tool_call", False)
    if expected_tool is not None:
        return picked_tool == expected_tool
    if expected_no_call:
        return picked_tool is None
    return None


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


async def _run(
    *,
    api_key: str,
    base_url: str,
    model: str,
    output_path: Path,
    category_filter: str | None,
    limit: int | None,
    max_tokens: int,
    timeout: float,
) -> int:
    corpus = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    prompts = corpus["prompts"]
    if category_filter:
        prompts = [p for p in prompts if p["category"] == category_filter]
    if limit is not None:
        prompts = prompts[:limit]

    tools = _load_tool_catalogue()
    print(f"[harness] {len(prompts)} prompts queued, {len(tools)} tools in catalogue, model={model}", file=sys.stderr)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    successes = 0
    failures = 0
    with output_path.open("a", encoding="utf-8") as f:
        for prompt in prompts:
            try:
                body, latency_ms = await _call_anthropic(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=prompt["user_prompt"],
                    tools=tools,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                obs = Observation(
                    prompt_id=prompt["id"],
                    category=prompt["category"],
                    phase_a_reference=prompt.get("phase_a_reference", ""),
                    user_prompt=prompt["user_prompt"],
                    expected_tool=prompt.get("expected_tool"),
                    expected_no_tool_call=prompt.get("expected_no_tool_call", False),
                    rationale=prompt.get("rationale", ""),
                    picked_tool=None,
                    picked_args=None,
                    text_response="",
                    stop_reason="",
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                    matched_expected=None,
                    provider="anthropic",
                    model=model,
                    timestamp=datetime.now(UTC).isoformat(),
                    raw_error=str(exc),
                )
                failures += 1
            else:
                picked, args, text, blocks = _parse_response(body)
                usage = body.get("usage") or {}
                obs = Observation(
                    prompt_id=prompt["id"],
                    category=prompt["category"],
                    phase_a_reference=prompt.get("phase_a_reference", ""),
                    user_prompt=prompt["user_prompt"],
                    expected_tool=prompt.get("expected_tool"),
                    expected_no_tool_call=prompt.get("expected_no_tool_call", False),
                    rationale=prompt.get("rationale", ""),
                    picked_tool=picked,
                    picked_args=args,
                    text_response=text,
                    stop_reason=body.get("stop_reason", ""),
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    latency_ms=latency_ms,
                    matched_expected=_judge_match(prompt, picked),
                    provider="anthropic",
                    model=model,
                    timestamp=datetime.now(UTC).isoformat(),
                    raw_blocks=blocks,
                )
                successes += 1

            f.write(json.dumps(obs.__dict__, default=str) + "\n")
            f.flush()
            verdict = "?" if obs.matched_expected is None else ("OK" if obs.matched_expected else "MISS")
            print(
                f"[{prompt['id']}] {verdict}  "
                f"picked={picked if successes else 'ERR'}  "
                f"in={obs.input_tokens}  out={obs.output_tokens}  "
                f"lat={obs.latency_ms}ms",
                file=sys.stderr,
            )

    print(f"[harness] done. {successes} success, {failures} failure.", file=sys.stderr)
    return 0 if failures == 0 else 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Anthropic model id.")
    parser.add_argument(
        "--base-url", default="https://api.anthropic.com", help="Override the API base URL (proxy / mock)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="JSONL output path. Appended (not overwritten) so you can run iteratively.",
    )
    parser.add_argument(
        "--category",
        choices=["overlap_disambiguation", "list_family_picker", "return_shape", "self_introspection"],
        help="Run only one category.",
    )
    parser.add_argument("--limit", type=int, help="Run only the first N prompts (after category filter).")
    parser.add_argument("--max-tokens", type=int, default=1024, help="Anthropic max_tokens per response.")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout per call (seconds).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import asyncio

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set in environment.", file=sys.stderr)
        return 2
    return asyncio.run(
        _run(
            api_key=api_key,
            base_url=args.base_url,
            model=args.model,
            output_path=args.output,
            category_filter=args.category,
            limit=args.limit,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
