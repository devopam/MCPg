"""Structured result schema for a Tier-A token-accounting run.

Frozen dataclasses serialized with ``dataclasses.asdict`` (repo convention).
One JSON file per run; provenance is passed in by the caller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TokenComparison:
    """One MCPg-output-vs-raw-SQL-equivalent token comparison.

    ``mcpg_tokens`` is the token count of what MCPg's purpose-built tool returns;
    ``raw_tokens`` the count of the raw-SQL equivalent an agent would otherwise
    pull and interpret. ``savings_pct`` / ``ratio`` are derived (see
    :func:`derive`).
    """

    name: str
    category: str  # "schema" | "query-plan" | "tool-context" | ...
    mcpg_tokens: int
    raw_tokens: int
    savings_pct: float
    ratio: float
    detail: dict[str, Any] = field(default_factory=dict)


def derive(
    name: str, category: str, mcpg_tokens: int, raw_tokens: int, detail: dict[str, Any] | None = None
) -> TokenComparison:
    """Build a :class:`TokenComparison`, computing savings % and ratio.

    ``savings_pct`` = how much smaller MCPg's output is than the raw equivalent
    (``0`` when the raw side is empty); ``ratio`` = raw / mcpg (how many times
    larger the raw output is; ``0`` when MCPg's output is empty).
    """
    savings = 100.0 * (raw_tokens - mcpg_tokens) / raw_tokens if raw_tokens else 0.0
    ratio = raw_tokens / mcpg_tokens if mcpg_tokens else 0.0
    return TokenComparison(
        name=name,
        category=category,
        mcpg_tokens=mcpg_tokens,
        raw_tokens=raw_tokens,
        savings_pct=savings,
        ratio=ratio,
        detail=detail or {},
    )


def break_even(comparisons: list[TokenComparison]) -> dict[str, Any]:
    """The break-even accounting — the credibility centerpiece.

    MCPg's rich tool surface costs more context *up front* (the ``tool-context``
    comparison), and its compact tool output saves tokens *per call* (the rest).
    After *K* database tasks in a session the per-call savings overtake the
    upfront cost. We report the upfront extra, the mean per-call saving, and
    ``K = ceil(extra / saving)``.

    Returns an all-present dict with ``break_even_tasks = None`` when there is no
    upfront cost row or no per-call savings to amortize it (so it serializes
    cleanly either way).
    """
    upfront = next((c for c in comparisons if c.category == "tool-context"), None)
    per_call = [c for c in comparisons if c.category != "tool-context"]
    upfront_extra = (upfront.mcpg_tokens - upfront.raw_tokens) if upfront else 0
    savings = [c.raw_tokens - c.mcpg_tokens for c in per_call]
    mean_saving = sum(savings) / len(savings) if savings else 0.0
    if upfront is None or mean_saving <= 0:
        k: int | None = None
    else:
        k = -(-upfront_extra // int(mean_saving)) if upfront_extra > 0 else 0  # ceil division
    return {
        "upfront_extra_tokens": upfront_extra,
        "mean_per_call_saving_tokens": mean_saving,
        "break_even_tasks": k,
    }


@dataclass(frozen=True)
class TokenReport:
    """A full Tier-A run — the top-level JSON document."""

    metadata: dict[str, Any]
    comparisons: list[TokenComparison]
    schema_version: int = SCHEMA_VERSION
    kind: str = "tokens_tier_a"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
