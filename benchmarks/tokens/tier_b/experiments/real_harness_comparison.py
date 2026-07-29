"""Diagnostic: does MCPg save tokens in the *real* harness, not a synthetic one?

Tier-B's main loop (``benchmarks.tokens.tier_b.agent``/``runner``) drives a
from-scratch Python agent loop — a one-line system prompt, calling the raw
Anthropic Messages API directly against MCP tools. It was never actually
testing "Claude Code with MCPg available," which is the real product surface
the token-efficiency question is about, and is why it produced a real,
confirmed discrepancy: ``translate_nl_to_sql`` never fired once across
Tier-B's free-choice runs, but the same model calls it readily in a real
Claude Code project with MCPg configured.

This script closes that gap directly: it drives real ``claude -p`` (headless)
invocations, twice per task — once with the ``mcpg`` MCP server available,
once without (``--strict-mcp-config`` with no ``--mcp-config``, so an ambient
project ``.mcp.json`` can't sneak it back in) — and compares the token/cost
numbers Claude Code itself reports. Both arms get the same one-line hint on
how to reach the database (a raw connection string), so the "off" arm isn't
artificially handicapped by not knowing how to connect — the only variable
under test is whether MCPg's tools are available, not whether the model
knows the DSN.

**Known limitation, disclosed rather than silently ignored**: if
``translate_nl_to_sql`` fires in the "mcpg" arm, its internal LLM call (a
separate, out-of-band HTTP request MCPg's server process makes directly to a
provider) is invisible to Claude Code's own usage/cost accounting — the same
blind spot a real user's own ``/cost`` would have. The numbers this script
reports are Claude Code's own authoritative, real-world spend; a trial where
that tool fires may have a higher *true* total than what's shown. Flagged in
the output, not hidden.

Costed (spawns a real `claude -p` process per arm per trial); never run in
CI; never wired into `benchmarks.tokens.tier_b.runner`.

    export ANTHROPIC_API_KEY=sk-...
    uv run python -m benchmarks.tokens.tier_b.experiments.real_harness_comparison \
        --database-url postgresql://postgres:postgres@localhost:5433/demo \
        --worktree-dir "C:\\Users\\devop\\OneDrive\\Documents\\GitHub\\MCPg-bench-worktree" \
        --trials 1 --model claude-sonnet-5 --max-budget-usd 0.50 \
        --output benchmarks/results/real-harness-comparison.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from benchmarks.tokens.tier_b.schema import ARM_BASELINE, ARM_MCPG, TierBReport, TrialResult, aggregate
from benchmarks.tokens.tier_b.tasks import real_harness_tasks

# Same tool surface as runner.py's _DEFAULT_MCPG_TOOLS, given to the "on" arm
# in full — this is the free-choice comparison the synthetic harness was
# supposed to be (and the discrepancy investigation showed it wasn't).
_MCPG_TOOLS = [
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
_MCP_SERVER_NAME = "mcpg"
# Claude Code's naming convention for a configured MCP server's tools,
# confirmed against this session's own tool list (mcp__<server>__<tool>).
_MCPG_ALLOWED_TOOLS = [f"mcp__{_MCP_SERVER_NAME}__{name}" for name in _MCPG_TOOLS]
_BASELINE_ALLOWED_TOOLS = ["Bash"]

# Both arms get this so the "off" arm isn't handicapped by not knowing how to
# reach the database — the only variable under test is tool availability.
_DB_HINT_TEMPLATE = (
    "\n\n(The database is a local PostgreSQL instance. If you need to connect directly: {database_url} "
    '— reachable via a local psql client, or via `docker exec mcpg-postgres psql -U postgres -d demo -c "<query>"` '
    "if no local client is installed.)"
)

_RESUME_KEYS = ("model", "trials_per_arm", "max_budget_usd")


def _build_mcp_config(database_url: str, worktree_dir: Path, nl2sql_api_key: str) -> dict[str, Any]:
    """The ``--mcp-config`` JSON for the "on" arm — runs the worktree's edited source via ``uv run --directory``.

    Deliberately *not* ``uvx mcpg`` (would pull the published PyPI release,
    missing this branch's nl2sql token-instrumentation fix and any other
    in-flight edits) — mirrors the "always uv run, never a stale global
    install" gotcha noted throughout this project's benchmark work.
    """
    return {
        "mcpServers": {
            _MCP_SERVER_NAME: {
                "command": "uv",
                "args": ["run", "--directory", str(worktree_dir), "mcpg"],
                "env": {
                    "MCPG_DATABASE_URL": database_url,
                    "MCPG_RATE_LIMIT_ENABLED": "false",
                    "MCPG_NL2SQL_PROVIDER": "anthropic",
                    "MCPG_NL2SQL_API_KEY": nl2sql_api_key,
                },
            }
        }
    }


async def _invoke_claude(
    prompt: str,
    *,
    model: str,
    max_budget_usd: float,
    mcp_config_path: Path | None,
    allowed_tools: list[str],
) -> dict[str, Any]:
    """Run one headless ``claude -p`` invocation; return the parsed JSON result."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise SystemExit("`claude` CLI not found on PATH.")
    cmd = [
        claude_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model,
        "--max-budget-usd",
        str(max_budget_usd),
        "--no-session-persistence",
        "--strict-mcp-config",
        "--allowedTools",
        *allowed_tools,
    ]
    if mcp_config_path is not None:
        cmd += ["--mcp-config", str(mcp_config_path)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {stderr.decode(errors='replace')[:2000]}")
    return cast("dict[str, Any]", json.loads(stdout.decode(errors="replace")))


def _result_to_trial(task_id: str, arm: str, trial: int, raw: dict[str, Any], passed: bool) -> TrialResult:
    """Map a `claude -p --output-format json` result onto the shared TrialResult schema.

    Field names are unconfirmed until the smoke-test step verifies them
    against a real invocation — this reads defensively (``.get`` with 0/""
    fallbacks) rather than assuming a shape, and records whatever the raw
    payload's usage block actually contains.
    """
    usage = raw.get("usage") or {}
    return TrialResult(
        task_id=task_id,
        arm=arm,
        trial=trial,
        tokens_in=int(usage.get("input_tokens") or 0),
        tokens_out=int(usage.get("output_tokens") or 0),
        turns=int(raw.get("num_turns") or 0),
        tool_calls=0,  # not extracted from --output-format json; see module docstring limitation
        passed=passed,
        final_answer=str(raw.get("result") or ""),
        error=raw.get("error"),
    )


def _checkpoint(args: argparse.Namespace, trials: list[TrialResult], *, complete: bool) -> TierBReport:
    metadata: dict[str, Any] = {
        "kind": "real_harness_comparison",
        "model": args.model,
        "trials_per_arm": args.trials,
        "max_budget_usd": args.max_budget_usd,
        "host": {"python": platform.python_version(), "os": platform.platform()},
        "complete": complete,
        "known_limitation": (
            "translate_nl_to_sql's internal LLM call, if it fires in the mcpg arm, is not "
            "included in these token/cost numbers — see module docstring."
        ),
    }
    report = TierBReport(metadata=metadata, trials=trials, aggregate=aggregate(trials))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return report


def _load_resumable(args: argparse.Namespace) -> list[TrialResult]:
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
    this_run = {"model": args.model, "trials_per_arm": args.trials, "max_budget_usd": args.max_budget_usd}
    if any(meta.get(k) != this_run[k] for k in _RESUME_KEYS):
        print(f"note: {args.output} exists but its config differs from this run; starting fresh, not resuming.")
        return []
    trials = [TrialResult(**t) for t in data.get("trials", [])]
    if trials:
        print(f"resuming from {args.output}: {len(trials)} trial(s) already recorded, will not be re-run.")
    return trials


async def _run(args: argparse.Namespace) -> TierBReport:
    nl2sql_key = os.environ.get("MCPG_NL2SQL_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not nl2sql_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY (or MCPG_NL2SQL_API_KEY) is not set — translate_nl_to_sql "
            "would fail every trial with 'no provider configured'."
        )
    if not shutil.which("claude"):
        raise SystemExit("`claude` CLI not found on PATH — this experiment drives it via subprocess.")

    tasks = real_harness_tasks()
    trials: list[TrialResult] = list(_load_resumable(args))
    done = {(t.task_id, t.arm, t.trial) for t in trials}

    mcp_config = _build_mcp_config(args.database_url, args.worktree_dir, nl2sql_key)
    fd, mcp_config_path_str = tempfile.mkstemp(prefix="mcpg-real-harness-", suffix=".json")
    mcp_config_path = Path(mcp_config_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mcp_config, f)

        arms: tuple[tuple[str, Path | None, list[str]], ...] = (
            (ARM_BASELINE, None, _BASELINE_ALLOWED_TOOLS),
            (ARM_MCPG, mcp_config_path, _MCPG_ALLOWED_TOOLS),
        )
        for task in tasks:
            for trial in range(args.trials):
                for arm, cfg_path, allowed_tools in arms:
                    if (task.id, arm, trial) in done:
                        print(f"  {task.id:20} {arm:9} #{trial}: skipped (already recorded)")
                        continue
                    prompt = task.prompt + _DB_HINT_TEMPLATE.format(database_url=args.database_url)
                    try:
                        raw = await _invoke_claude(
                            prompt,
                            model=args.model,
                            max_budget_usd=args.max_budget_usd,
                            mcp_config_path=cfg_path,
                            allowed_tools=allowed_tools,
                        )
                        passed = task.grade(str(raw.get("result") or ""))
                        result = _result_to_trial(task.id, arm, trial, raw, passed)
                    except Exception as exc:  # record and continue, same as runner.py's pattern
                        result = TrialResult(
                            task_id=task.id,
                            arm=arm,
                            trial=trial,
                            tokens_in=0,
                            tokens_out=0,
                            turns=0,
                            tool_calls=0,
                            passed=False,
                            final_answer="",
                            error=str(exc),
                        )
                    trials.append(result)
                    flag = "PASS" if result.passed else ("ERR " if result.error else "FAIL")
                    print(f"  {task.id:20} {arm:9} #{trial}: tok={result.total_tokens:6} turns={result.turns:2} {flag}")
                    _checkpoint(args, trials, complete=False)
    finally:
        mcp_config_path.unlink(missing_ok=True)

    return _checkpoint(args, trials, complete=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="PostgreSQL DSN (the demo dataset must be loaded).")
    parser.add_argument(
        "--worktree-dir", type=Path, required=True, help="Path to the MCPg checkout to run via `uv run --directory`."
    )
    parser.add_argument("--trials", type=int, default=1, help="Trials per (task, arm). Default 1.")
    parser.add_argument(
        "--model", default="claude-sonnet-5", help="Model id for both the CLI session and MCPg's nl2sql provider."
    )
    parser.add_argument(
        "--max-budget-usd", type=float, default=0.50, help="Hard spend cap per claude -p invocation. Default 0.50."
    )
    parser.add_argument("--output", type=Path, required=True, help="Path to write the result JSON.")
    args = parser.parse_args(argv)

    report = asyncio.run(_run(args))
    agg = report.aggregate
    print(
        f"\naggregate: baseline(no-mcpg) {agg['baseline']['mean_total_tokens']:.0f} tok vs "
        f"mcpg-on {agg['mcpg']['mean_total_tokens']:.0f} tok  ->  {agg['token_ratio']:.2f}x  "
        f"(correctness: baseline {agg['baseline']['correctness']:.0%} / mcpg {agg['mcpg']['correctness']:.0%}; "
        f"{agg['errored']} errored)"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
