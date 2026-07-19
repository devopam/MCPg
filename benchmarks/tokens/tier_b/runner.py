"""Tier-B orchestrator (CLI) — the costed agent-loop token study.

For each task, runs two arms (``baseline`` = a bare ``run_select`` agent;
``mcpg`` = MCPg's purpose-built tools) for N trials against a live MCPg server
over an in-memory MCP session, and writes one structured JSON document with
every trial's tokens / tool-calls / turns / correctness plus the aggregate.

    export ANTHROPIC_API_KEY=sk-...            # Tier-B calls a real model
    # load the demo dataset first (its planted flaws are the known answers):
    #   mcpg --demo            (or the demo loader against $MCPG_TEST_DATABASE_URL)
    uv run python -m benchmarks.tokens.tier_b.runner \
        --database-url "$MCPG_TEST_DATABASE_URL" --trials 5 \
        --model claude-sonnet-5 --output benchmarks/results/tokens-tier-b.json

**Costed and non-deterministic-ish** (temp 0 helps, but tool availability and
model updates move numbers) — it is NOT run in CI. The pure helpers it calls
(tasks graders, schema aggregation) are unit-tested; this orchestration and the
model loop are not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp.shared.memory import create_connected_server_and_client_session

from benchmarks.tokens.tier_b.agent import run_trial
from benchmarks.tokens.tier_b.model import DEFAULT_MODEL, AnthropicClient
from benchmarks.tokens.tier_b.schema import ARM_BASELINE, ARM_MCPG, TierBReport, TrialResult, aggregate
from benchmarks.tokens.tier_b.tasks import default_tasks
from mcpg import __version__
from mcpg.config import load_settings
from mcpg.server import create_server

# The baseline agent gets a lone SQL runner. The MCPg agent gets a focused,
# task-relevant surface (as an operator would expose via session-intent — the
# full-surface upfront cost is quantified separately in Tier-A). Both are
# validated against the server's actual tools at run time.
_BASELINE_TOOLS = {"run_select"}
_DEFAULT_MCPG_TOOLS = [
    "run_select",
    "get_compact_schema",
    "describe_table",
    "list_schemas",
    "analyze_query_plan",
    "recommend_indexes",
    "find_sensitive_columns",
    "audit_database",
]
_READ_TIMEOUT = timedelta(seconds=60)


async def _run(args: argparse.Namespace) -> TierBReport:
    settings = load_settings({"MCPG_DATABASE_URL": args.database_url})
    model = AnthropicClient(args.model)  # raises a clear error if the key/SDK is missing
    tasks = default_tasks()
    trials: list[TrialResult] = []
    async with AsyncExitStack() as stack:
        server = create_server(settings)
        session = await stack.enter_async_context(
            create_connected_server_and_client_session(server, read_timeout_seconds=_READ_TIMEOUT)
        )
        listed = await session.list_tools()
        server_names = {t.name for t in listed.tools}
        mcpg_tools = {n for n in args.mcpg_tools if n in server_names}
        missing = set(args.mcpg_tools) - server_names
        if missing:
            print(f"warning: requested mcpg tools not on the server, skipped: {sorted(missing)}")
        arms = ((ARM_BASELINE, _BASELINE_TOOLS & server_names), (ARM_MCPG, mcpg_tools))
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
                        f"  {task.id:16} {arm:9} #{trial}: "
                        f"tok={result.total_tokens:6} tools={result.tool_calls:2} turns={result.turns:2} {flag}"
                    )

    metadata: dict[str, Any] = {
        "timestamp": args.timestamp,
        "git_sha": args.git_sha,
        "mcpg_version": __version__,
        "model": args.model,
        "trials_per_arm": args.trials,
        "max_turns": args.max_turns,
        "mcpg_tools": sorted(mcpg_tools),
        "host": {"python": platform.python_version(), "os": platform.platform()},
    }
    return TierBReport(metadata=metadata, trials=trials, aggregate=aggregate(trials))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPg Tier-B agent-loop token study (costed — needs a model key).")
    parser.add_argument("--database-url", required=True, help="PostgreSQL DSN (the demo dataset must be loaded).")
    parser.add_argument("--trials", type=int, default=5, help="Trials per (task, arm). Default 5.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id. Default {DEFAULT_MODEL}.")
    parser.add_argument("--max-turns", type=int, default=12, help="Max model turns per trial. Default 12.")
    parser.add_argument("--mcpg-tools", nargs="*", default=_DEFAULT_MCPG_TOOLS, help="Tools the MCPg arm may use.")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the result JSON.")
    parser.add_argument("--git-sha", default="unknown", help="Provenance: the commit under test.")
    parser.add_argument("--timestamp", default="unknown", help="Provenance: ISO-8601 run timestamp.")
    args = parser.parse_args(argv)

    report = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    agg = report.aggregate
    print(
        f"\naggregate: baseline {agg['baseline']['mean_total_tokens']:.0f} tok vs "
        f"MCPg {agg['mcpg']['mean_total_tokens']:.0f} tok  ->  {agg['token_ratio']:.1f}x  "
        f"(correctness: baseline {agg['baseline']['correctness']:.0%} / MCPg {agg['mcpg']['correctness']:.0%}; "
        f"{agg['errored']} errored)"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
