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

Uses ``--output-format stream-json`` (not the single-blob ``json`` format) so
each trial's real tool-call sequence is captured — not just aggregate
token/turn counts — via the per-turn ``assistant`` events' ``tool_use``
content blocks. This is what actually answers "did translate_nl_to_sql fire,"
confirmed working in a live dry run before being wired in here (it also
caught the model calling a tool name — ``list_tables`` — that doesn't exist
in MCPg, and recovering from the error).

Deliberately **not** ``--bare`` mode: it would strip Claude Code's personal
hooks/CLAUDE.md/skills overhead (real, but identical in both arms, so it
doesn't bias the ratio — it just adds shared noise/cost), but it also
requires a plain ``ANTHROPIC_API_KEY`` for the top-level session's own auth
(confirmed: fails "Not logged in" otherwise), which this project authenticates
via OAuth instead. Not worth asking for another key over.

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
        --trials 1 --model claude-sonnet-5 --max-budget-usd 1.00 \
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
from benchmarks.tokens.tier_b.tasks import audit_tasks, real_harness_tasks

_TASK_SETS = {"analytical": real_harness_tasks, "audit": audit_tasks}

_MCP_SERVER_NAME = "mcpg"
# Claude Code's naming convention for a configured MCP server's tools,
# confirmed against this session's own tool list (mcp__<server>__<tool>).
# Wildcard, not a curated tool-name list: an earlier enumerated allowlist bit
# twice in back-to-back runs of this same diagnostic — once for "Bash"
# (missing entirely) and once for "lint_naming_conventions" (a real MCPg
# tool the 9-name _MCPG_TOOLS curation never anticipated) — both times the
# model correctly reached for the tool it needed and got stuck at a
# permission wall it can't clear in headless mode, burning huge token
# budgets or failing outright, not because the approach was wrong. "Bash"
# alongside the wildcard: the mcpg arm must be genuine free choice (mcpg's
# full tool surface *added* to the agent's normal means of double-checking,
# not substituted for it).
_MCPG_ALLOWED_TOOLS = [f"mcp__{_MCP_SERVER_NAME}__*", "Bash"]
_BASELINE_ALLOWED_TOOLS = ["Bash"]

# Both arms get this so the "off" arm isn't handicapped by not knowing how to
# reach the database — the only variable under test is tool availability.
_DB_HINT_TEMPLATE = (
    "\n\n(The database is a local PostgreSQL instance. If you need to connect directly: {database_url} "
    '— reachable via a local psql client, or via `docker exec mcpg-postgres psql -U postgres -d demo -c "<query>"` '
    "if no local client is installed.)"
)

_RESUME_KEYS = (
    "model",
    "trials_per_arm",
    "max_budget_usd",
    "mcpg_system_prompt_hint",
    "task_set",
    "database_url",
    "worktree_dir",
)


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


# C901 rationale: one-off diagnostic benchmark script (never run in CI, per
# its module docstring) driving a real `claude -p` subprocess with
# stdin-piping worked around Windows-specific argv-quoting bugs (see the
# docstring below) and parsing streamed JSON events -- the branching is
# platform-workaround + streamed-event-parsing plumbing for a script that
# only a human runs interactively.
async def _invoke_claude(  # noqa: C901
    prompt: str,
    *,
    model: str,
    max_budget_usd: float,
    mcp_config_path: Path | None,
    allowed_tools: list[str],
    append_system_prompt: str | None = None,
    timeout_seconds: float = 600.0,
) -> tuple[dict[str, Any], list[str]]:
    """Run one headless ``claude -p`` invocation; return ``(result_event, tool_names)``.

    The prompt is sent over **stdin**, not as a CLI argument — confirmed
    necessary on Windows during the smoke test: this project's prompts
    contain backticks and embedded double quotes (the DB-connection hint),
    and passing that through ``asyncio.create_subprocess_exec`` to the
    resolved ``claude.CMD`` shim produced empty/garbled stdout. Piping via
    stdin (the documented default text input for ``--print``) sidesteps
    Windows batch-file argument quoting entirely.

    ``--output-format stream-json`` emits one JSON object per line: several
    ``type: "assistant"`` turn events (whose ``message.content`` blocks
    include any ``tool_use`` calls, in order — this is where ``tool_names``
    comes from) followed by one terminal ``type: "result"`` event carrying
    ``total_cost_usd``/``usage``/``result``/``num_turns`` — the same fields
    the single-blob ``json`` format returns, confirmed identical between the
    two formats. That terminal event is written to stdout **even on a
    non-zero exit** (confirmed: a budget-exhausted run exits 1 with an empty
    stderr and a full result line on stdout), so this always scans stdout
    first and only raises if no ``result`` line is found at all.

    ``timeout_seconds`` bounds ``communicate()``: a hung CLI/network call
    would otherwise block this trial (and every trial after it) forever,
    with nothing checkpointed. On timeout the process is killed and a
    ``TimeoutError`` raised, which the caller in ``_run`` already catches
    and records as a per-trial error, same as any other failure.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise SystemExit("`claude` CLI not found on PATH.")
    cmd = [
        claude_bin,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
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
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=prompt.encode("utf-8")), timeout=timeout_seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"claude -p did not finish within {timeout_seconds:.0f}s; killed the process.") from None
    tool_names: list[str] = []
    result_event: dict[str, Any] | None = None
    for line in stdout.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = cast("dict[str, Any]", json.loads(line))
        except json.JSONDecodeError:
            continue  # non-JSON noise on a line; the events we need are well-formed
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tool_names.append(str(block.get("name")))
        elif event.get("type") == "result":
            result_event = event
    if result_event is None:
        raise RuntimeError(
            f"claude -p exited {proc.returncode} with no result event: "
            f"{stderr.decode(errors='replace')[:1000] or stdout.decode(errors='replace')[-1000:]}"
        )
    return result_event, tool_names


def _result_to_trial(
    task_id: str, arm: str, trial: int, raw: dict[str, Any], tool_names: list[str], passed: bool
) -> TrialResult:
    """Map a `claude -p --output-format stream-json` result event onto the shared TrialResult schema.

    Field names confirmed against real invocations during the smoke test:
    ``usage.input_tokens``/``cache_creation_input_tokens``/``cache_read_input_tokens``
    (all summed into ``tokens_in`` — a cache read is still real prompt
    content the model processed, just discounted, and Tier-B's token_ratio
    has always counted raw tokens moved, not cost), ``usage.output_tokens``,
    ``num_turns``, ``total_cost_usd``, and ``result`` (the final answer
    text). On an error/budget-exhausted run, ``result`` is absent and usage
    fields are 0 — the real per-model breakdown lives in
    ``modelUsage.<model>`` instead, but that path already means the trial
    didn't produce a usable answer.
    """
    usage = raw.get("usage") or {}
    error = None if raw.get("is_error") is False else (raw.get("errors") or raw.get("terminal_reason"))
    return TrialResult(
        task_id=task_id,
        arm=arm,
        trial=trial,
        tokens_in=int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0),
        tokens_out=int(usage.get("output_tokens") or 0),
        turns=int(raw.get("num_turns") or 0),
        tool_calls=len(tool_names),
        passed=passed,
        final_answer=str(raw.get("result") or ""),
        error=str(error) if error else None,
        cost_usd=float(raw.get("total_cost_usd") or 0.0),
        tool_names=tuple(tool_names),
    )


def _checkpoint(args: argparse.Namespace, trials: list[TrialResult], *, complete: bool) -> TierBReport:
    metadata: dict[str, Any] = {
        "kind": "real_harness_comparison",
        "model": args.model,
        "trials_per_arm": args.trials,
        "max_budget_usd": args.max_budget_usd,
        "mcpg_system_prompt_hint": args.mcpg_system_prompt_hint,
        "task_set": args.task_set,
        "database_url": args.database_url,
        "worktree_dir": str(args.worktree_dir),
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
    this_run = {
        "model": args.model,
        "trials_per_arm": args.trials,
        "max_budget_usd": args.max_budget_usd,
        "mcpg_system_prompt_hint": args.mcpg_system_prompt_hint,
        "task_set": args.task_set,
        "database_url": args.database_url,
        "worktree_dir": str(args.worktree_dir),
    }
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

    tasks = _TASK_SETS[args.task_set]()
    trials: list[TrialResult] = list(_load_resumable(args))
    done = {(t.task_id, t.arm, t.trial) for t in trials}

    mcp_config = _build_mcp_config(args.database_url, args.worktree_dir, nl2sql_key)
    fd, mcp_config_path_str = tempfile.mkstemp(prefix="mcpg-real-harness-", suffix=".json")
    mcp_config_path = Path(mcp_config_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mcp_config, f)

        # append_system_prompt is None for baseline unconditionally — the
        # "prefer mcpg" hint has nothing to refer to when mcpg isn't even
        # in the tool list, so applying it there would be meaningless noise.
        arms: tuple[tuple[str, Path | None, list[str], str | None], ...] = (
            (ARM_BASELINE, None, _BASELINE_ALLOWED_TOOLS, None),
            (ARM_MCPG, mcp_config_path, _MCPG_ALLOWED_TOOLS, args.mcpg_system_prompt_hint),
        )
        for task in tasks:
            for trial in range(args.trials):
                for arm, cfg_path, allowed_tools, hint in arms:
                    if (task.id, arm, trial) in done:
                        print(f"  {task.id:20} {arm:9} #{trial}: skipped (already recorded)")
                        continue
                    prompt = task.prompt + _DB_HINT_TEMPLATE.format(database_url=args.database_url)
                    try:
                        raw, tool_names = await _invoke_claude(
                            prompt,
                            model=args.model,
                            max_budget_usd=args.max_budget_usd,
                            mcp_config_path=cfg_path,
                            allowed_tools=allowed_tools,
                            append_system_prompt=hint,
                            timeout_seconds=args.invocation_timeout_seconds,
                        )
                        passed = task.grade(str(raw.get("result") or ""))
                        result = _result_to_trial(task.id, arm, trial, raw, tool_names, passed)
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
                    nl2sql_flag = "nl2sql=YES" if "mcp__mcpg__translate_nl_to_sql" in result.tool_names else ""
                    print(
                        f"  {task.id:20} {arm:9} #{trial}: tok={result.total_tokens:6} turns={result.turns:2} "
                        f"${result.cost_usd:.3f} {flag} {nl2sql_flag} tools={list(result.tool_names)}"
                    )
                    _checkpoint(args, trials, complete=False)
    finally:
        # ASYNC240 rationale: one-off benchmark script, single sequential coroutine
        # (no concurrent tasks to starve); a single unlink() at teardown after
        # trials that each take seconds to minutes is not a meaningful blocking cost.
        mcp_config_path.unlink(missing_ok=True)  # noqa: ASYNC240

    return _checkpoint(args, trials, complete=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="PostgreSQL DSN (the demo dataset must be loaded).")
    parser.add_argument(
        "--worktree-dir", type=Path, required=True, help="Path to the MCPg checkout to run via `uv run --directory`."
    )
    parser.add_argument("--trials", type=int, default=1, help="Trials per (task, arm). Default 1.")
    parser.add_argument(
        "--task-set",
        choices=sorted(_TASK_SETS),
        default="analytical",
        help=(
            "'analytical' (default): the two NL->SQL business-question tasks. "
            "'audit': the three planted-flaw DBA tasks (missing_index/pii_columns/naming_violation) "
            "that the synthetic harness measured costing the mcpg advisor arm 2.66x more, unexplained — "
            "use this to re-diagnose that result with real tool-trace capture."
        ),
    )
    parser.add_argument(
        "--model", default="claude-sonnet-5", help="Model id for both the CLI session and MCPg's nl2sql provider."
    )
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=1.00,
        help=(
            "Hard spend cap per claude -p invocation. Default 1.00 - Claude Code's own system-prompt/"
            "tool-schema overhead alone costs ~$0.10-0.20 in cache-write tokens before the task even "
            "starts, observed during the smoke test, so a tighter cap risks premature budget_exhausted."
        ),
    )
    parser.add_argument(
        "--invocation-timeout-seconds",
        type=float,
        default=600.0,
        help=(
            "Kill and record-as-error a single claude -p invocation that hasn't finished within this "
            "many seconds, rather than letting a hung CLI/network call block this trial (and every "
            "trial after it) forever. Default 600s."
        ),
    )
    parser.add_argument(
        "--mcpg-system-prompt-hint",
        default=None,
        help=(
            "Text appended (via --append-system-prompt) to the mcpg arm only, e.g. the standing "
            "'prefer mcpg over ad-hoc SQL' instruction some real projects inject via a SessionStart "
            "hook — lets this diagnose whether that standing instruction, not the harness or task "
            "design, is what makes translate_nl_to_sql actually get chosen. Omitted for the baseline "
            "arm (mcpg isn't in its tool list, so the hint would have nothing to refer to)."
        ),
    )
    parser.add_argument("--output", type=Path, required=True, help="Path to write the result JSON.")
    args = parser.parse_args(argv)

    report = asyncio.run(_run(args))
    agg = report.aggregate
    trials = report.trials
    baseline_cost = sum(t.cost_usd for t in trials if t.arm == ARM_BASELINE and t.error is None)
    mcpg_cost = sum(t.cost_usd for t in trials if t.arm == ARM_MCPG and t.error is None)
    nl2sql_fired = sum(1 for t in trials if t.arm == ARM_MCPG and "mcp__mcpg__translate_nl_to_sql" in t.tool_names)
    mcpg_trial_count = sum(1 for t in trials if t.arm == ARM_MCPG)
    print(
        f"\naggregate: baseline(no-mcpg) {agg['baseline']['mean_total_tokens']:.0f} tok vs "
        f"mcpg-on {agg['mcpg']['mean_total_tokens']:.0f} tok  ->  {agg['token_ratio']:.2f}x  "
        f"(correctness: baseline {agg['baseline']['correctness']:.0%} / mcpg {agg['mcpg']['correctness']:.0%}; "
        f"{agg['errored']} errored)"
    )
    print(
        f"total spend: baseline ${baseline_cost:.3f}  mcpg ${mcpg_cost:.3f}  "
        f"(all-trials ${baseline_cost + mcpg_cost:.3f})"
    )
    print(f"translate_nl_to_sql fired in {nl2sql_fired}/{mcpg_trial_count} mcpg-arm trial(s)")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
