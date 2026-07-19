"""Result schema + pure aggregation for a Tier-B agent-loop run.

One :class:`TrialResult` per (task, arm, trial). :func:`aggregate` reduces the
trials to per-(task, arm) means + per-arm totals + the headline arm comparison
(MCPg vs a bare ``run_select`` agent). Pure and unit-tested; the runner and the
model loop are not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_VERSION = 1

ARM_MCPG = "mcpg"
ARM_BASELINE = "baseline"


@dataclass(frozen=True)
class TrialResult:
    """One (task, arm) trial's measured cost + outcome."""

    task_id: str
    arm: str  # ARM_MCPG | ARM_BASELINE
    trial: int
    tokens_in: int
    tokens_out: int
    turns: int
    tool_calls: int
    passed: bool
    final_answer: str
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass(frozen=True)
class TierBReport:
    """A full Tier-B run — the top-level JSON document."""

    metadata: dict[str, Any]
    trials: list[TrialResult]
    aggregate: dict[str, Any]
    schema_version: int = SCHEMA_VERSION
    kind: str = "tokens_tier_b"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def aggregate(trials: list[TrialResult]) -> dict[str, Any]:
    """Reduce trials to per-(task, arm) means, per-arm totals, and the arm comparison.

    The headline is ``token_ratio`` = baseline mean total tokens / MCPg mean
    total tokens across all completed trials — how many times more tokens the
    bare-SQL agent spends. ``correctness`` is the fraction of trials whose final
    answer matched the known-correct one. Only non-errored trials feed the means;
    ``errored`` is reported so silent drops can't inflate the story.
    """
    ok = [t for t in trials if t.error is None]

    def arm_stats(arm: str) -> dict[str, Any]:
        rows = [t for t in ok if t.arm == arm]
        return {
            "trials": len(rows),
            "mean_total_tokens": _mean([t.total_tokens for t in rows]),
            "mean_tool_calls": _mean([t.tool_calls for t in rows]),
            "mean_turns": _mean([t.turns for t in rows]),
            "correctness": _mean([1.0 if t.passed else 0.0 for t in rows]),
        }

    mcpg, baseline = arm_stats(ARM_MCPG), arm_stats(ARM_BASELINE)
    mcpg_tok = mcpg["mean_total_tokens"]
    token_ratio = baseline["mean_total_tokens"] / mcpg_tok if mcpg_tok else 0.0

    per_task: list[dict[str, Any]] = []
    for task_id in dict.fromkeys(t.task_id for t in ok):
        entry: dict[str, Any] = {"task_id": task_id}
        for arm in (ARM_MCPG, ARM_BASELINE):
            rows = [t for t in ok if t.task_id == task_id and t.arm == arm]
            entry[arm] = {
                "mean_total_tokens": _mean([t.total_tokens for t in rows]),
                "mean_tool_calls": _mean([t.tool_calls for t in rows]),
                "correctness": _mean([1.0 if t.passed else 0.0 for t in rows]),
            }
        per_task.append(entry)

    return {
        "mcpg": mcpg,
        "baseline": baseline,
        "token_ratio": token_ratio,
        "errored": len(trials) - len(ok),
        "per_task": per_task,
    }
