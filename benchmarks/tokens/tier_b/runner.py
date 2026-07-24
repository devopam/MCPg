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
    "translate_nl_to_sql",
]
_READ_TIMEOUT = timedelta(seconds=60)

# Metadata keys that must match an on-disk checkpoint before it's safe to
# resume from — anything else and "skip trials already present" would quietly
# mix results from two different configurations into one aggregate.
_RESUME_KEYS = ("model", "trials_per_arm", "max_turns", "mcpg_tools")


def _checkpoint(
    args: argparse.Namespace, trials: list[TrialResult], resolved_mcpg_tools: set[str], *, complete: bool
) -> TierBReport:
    """Write the run so far to ``args.output``, overwriting the prior checkpoint.

    Each trial is a real, costed model conversation — losing one to an
    unexplained process interruption (seen repeatedly running the perf harness
    in this environment) means paying for it again for nothing. Checkpointing
    after every trial means an interruption loses at most the one in flight,
    and a rerun against the same --output can skip everything already paid for
    (see :func:`_load_resumable`).
    """
    metadata: dict[str, Any] = {
        "timestamp": args.timestamp,
        "git_sha": args.git_sha,
        "mcpg_version": __version__,
        "model": args.model,
        "trials_per_arm": args.trials,
        "max_turns": args.max_turns,
        "mcpg_tools": sorted(resolved_mcpg_tools),
        "host": {"python": platform.python_version(), "os": platform.platform()},
        "complete": complete,
    }
    report = TierBReport(metadata=metadata, trials=trials, aggregate=aggregate(trials))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return report


def _load_resumable(args: argparse.Namespace) -> list[TrialResult]:
    """Load already-paid-for trials from ``args.output``, if it's safe to resume.

    Safe means: the file exists, isn't already ``complete``, and its recorded
    config (model / trials-per-arm / max-turns / tool set) matches this
    invocation's — otherwise silently reusing it would mix incompatible runs
    into one aggregate. Any mismatch or unreadable file means starting clean;
    this never raises, since a corrupt/foreign file just isn't resumable, not
    a fatal error.
    """
    if not args.output.exists():
        return []
    try:
        data = json.loads(args.output.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    meta = data.get("metadata", {})
    if meta.get("complete"):
        print(f"note: {args.output} already holds a complete run; overwriting with a fresh one.")
        return []
    this_run = {
        "model": args.model,
        "trials_per_arm": args.trials,
        "max_turns": args.max_turns,
        "mcpg_tools": sorted(args.mcpg_tools),
    }
    if any(meta.get(k) != this_run[k] for k in _RESUME_KEYS):
        print(f"note: {args.output} exists but its config differs from this run; starting fresh, not resuming.")
        return []
    trials = [TrialResult(**t) for t in data.get("trials", [])]
    if trials:
        print(f"resuming from {args.output}: {len(trials)} trial(s) already recorded, will not be re-run.")
    return trials


async def _run(args: argparse.Namespace) -> TierBReport:
    settings = load_settings({"MCPG_DATABASE_URL": args.database_url})
    model = AnthropicClient(args.model)  # raises a clear error if the key/SDK is missing
    tasks = default_tasks()
    trials: list[TrialResult] = list(_load_resumable(args))
    done = {(t.task_id, t.arm, t.trial) for t in trials}
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
                    if (task.id, arm, trial) in done:
                        print(f"  {task.id:16} {arm:9} #{trial}: skipped (already recorded)")
                        continue
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
                    # Each trial just spent real money on a real model call — persist it
                    # immediately so an interruption never loses more than the one in flight.
                    _checkpoint(args, trials, mcpg_tools, complete=False)

    return _checkpoint(args, trials, mcpg_tools, complete=True)


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
