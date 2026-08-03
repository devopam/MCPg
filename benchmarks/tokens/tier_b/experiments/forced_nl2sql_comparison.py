"""Diagnostic: does MCPg's translate_nl_to_sql tool actually get used?

Both the default Haiku and the real target Sonnet model, given the full
9-tool MCPg arm, chose to explore the schema and write their own SQL via
run_select rather than ever calling translate_nl_to_sql — on both of the
analytical tasks that tool exists for. That makes the normal "mcpg" arm's
comparison for those two tasks silent on the one capability being asked
about: does going through MCPg's dedicated NL->SQL translator help or hurt,
against not using it at all?

This script forces the answer by restricting each arm to exactly one tool
for the two analytical tasks — baseline gets only run_select (unchanged from
the main study), MCPg gets only translate_nl_to_sql (nothing else, so it
cannot answer without going through it). That is deliberately not how the
committed benchmarks.tokens.tier_b.runner works for the rest of the suite —
this is a targeted, reusable comparison, kept separate so it never
accidentally becomes the default methodology.

Costed (calls a real model); never run in CI; never merged into runner.py.

    export ANTHROPIC_API_KEY=sk-...
    uv run python -m benchmarks.tokens.tier_b.experiments.forced_nl2sql_comparison \
        --database-url postgresql://postgres:postgres@localhost:5433/demo \
        --trials 3 --model claude-sonnet-5 \
        --output benchmarks/results/forced-nl2sql-comparison.json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

if sys.platform == "win32":
    # Matches src/mcpg/__main__.py: async psycopg needs the selector loop,
    # not Windows' default proactor loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcp.shared.memory import create_connected_server_and_client_session

from benchmarks.tokens.tier_b.agent import run_trial
from benchmarks.tokens.tier_b.model import DEFAULT_MODEL, AnthropicClient
from benchmarks.tokens.tier_b.schema import ARM_BASELINE, ARM_MCPG, TrialResult, aggregate
from benchmarks.tokens.tier_b.tasks import default_tasks
from mcpg.config import load_settings
from mcpg.server import create_server

_TASK_IDS = {"top_revenue_category", "top_customer_lifetime_spend"}
_READ_TIMEOUT = timedelta(seconds=60)


async def _run(args: argparse.Namespace) -> list[TrialResult]:
    # translate_nl_to_sql's internal LLM call needs its OWN provider config —
    # separate from the outer agent's ANTHROPIC_API_KEY that AnthropicClient
    # (model.py) reads. Missing this the first time this script ran made
    # every translate_nl_to_sql call fail with "no provider configured",
    # producing a false "baseline wins" result that was actually just the
    # tool erroring out, not losing a fair comparison. Reuse the same
    # Anthropic key from the process environment rather than hardcoding it.
    nl2sql_key = os.environ.get("MCPG_NL2SQL_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not nl2sql_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY (or MCPG_NL2SQL_API_KEY) is not set — translate_nl_to_sql "
            "would fail every trial with 'no provider configured', as it did last run."
        )
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": args.database_url,
            "MCPG_NL2SQL_PROVIDER": "anthropic",
            "MCPG_NL2SQL_API_KEY": nl2sql_key,
        }
    )
    model = AnthropicClient(args.model)
    tasks = [t for t in default_tasks() if t.id in _TASK_IDS]
    trials: list[TrialResult] = []
    async with AsyncExitStack() as stack:
        server = create_server(settings)
        session = await stack.enter_async_context(
            create_connected_server_and_client_session(server, read_timeout_seconds=_READ_TIMEOUT)
        )
        # The forced, single-tool arms this script exists for — NOT
        # runner.py's _DEFAULT_MCPG_TOOLS / _BASELINE_TOOLS.
        arms = ((ARM_BASELINE, {"run_select"}), (ARM_MCPG, {"translate_nl_to_sql"}))
        for task in tasks:
            for trial in range(args.trials):
                for arm, allowed in arms:
                    result = await run_trial(
                        task,
                        session=session,
                        model=model,
                        allowed_tools=allowed,
                        arm=arm,
                        trial=trial,
                        max_turns=args.max_turns,
                    )
                    trials.append(result)
                    flag = "PASS" if result.passed else ("ERR " if result.error else "FAIL")
                    print(
                        f"  {task.id:28} {arm:9} #{trial}: tok={result.total_tokens:6} "
                        f"(outer={result.tokens_in + result.tokens_out:6} "
                        f"hidden={result.hidden_tokens_in + result.hidden_tokens_out:5}) "
                        f"tools={result.tool_calls:2} turns={result.turns:2} {flag}"
                    )
    return trials


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="PostgreSQL DSN (the demo dataset must be loaded).")
    parser.add_argument("--trials", type=int, default=3, help="Trials per (task, arm). Default 3.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id. Default {DEFAULT_MODEL}.")
    parser.add_argument("--max-turns", type=int, default=10, help="Max model turns per trial. Default 10.")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the result JSON.")
    args = parser.parse_args(argv)

    trials = asyncio.run(_run(args))
    agg = aggregate(trials)
    print(
        f"\naggregate: baseline {agg['baseline']['mean_total_tokens']:.0f} tok vs "
        f"MCPg {agg['mcpg']['mean_total_tokens']:.0f} tok  ->  {agg['token_ratio']:.2f}x  "
        f"(correctness: baseline {agg['baseline']['correctness']:.0%} / MCPg {agg['mcpg']['correctness']:.0%}; "
        f"{agg['errored']} errored)"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"trials": [dataclasses.asdict(t) for t in trials], "aggregate": agg}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
