"""Structured result schema for a Tier-A token-accounting run.

Frozen dataclasses serialized with ``dataclasses.asdict`` (repo convention).
One JSON file per run; provenance is passed in by the caller.
"""

from __future__ import annotations

import math
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


def _break_even_tasks(upfront_extra: int, mean_saving: float) -> int | None:
    """``ceil(extra / saving)`` tasks, or None when it can't be amortized."""
    if mean_saving <= 0:
        return None
    if upfront_extra <= 0:
        return 0
    return math.ceil(upfront_extra / mean_saving)


def break_even(comparisons: list[TokenComparison]) -> dict[str, Any]:
    """The break-even accounting — the credibility centerpiece.

    MCPg's tool surface costs more context *up front* (the ``tool-context``
    comparisons — one per surface: full / read-only / session-intent), and its
    compact tool output saves tokens *per call* (the rest). After *K* database
    tasks the per-call savings overtake the upfront cost; a narrower surface
    lowers the upfront cost and so moves *K* left.

    Reports the mean per-call saving, a per-surface breakdown, and — as the
    headline — the **widest** surface's break-even (the worst case). All fields
    are always present; ``break_even_tasks`` is ``None`` when there is no upfront
    row or no per-call savings to amortize it, so it serializes cleanly.
    """
    per_call = [c for c in comparisons if c.category != "tool-context"]
    savings = [c.raw_tokens - c.mcpg_tokens for c in per_call]
    mean_saving = sum(savings) / len(savings) if savings else 0.0

    surfaces: list[dict[str, Any]] = []
    for c in (c for c in comparisons if c.category == "tool-context"):
        extra = c.mcpg_tokens - c.raw_tokens
        surfaces.append(
            {
                "name": c.detail.get("surface", c.name),
                "tool_count": c.detail.get("tools"),
                "mcpg_tokens": c.mcpg_tokens,
                "upfront_extra_tokens": extra,
                "break_even_tasks": _break_even_tasks(extra, mean_saving),
            }
        )
    # Headline = the widest surface (largest upfront cost — the worst case).
    headline = max(surfaces, key=lambda s: s["upfront_extra_tokens"]) if surfaces else None
    return {
        "mean_per_call_saving_tokens": mean_saving,
        "surfaces": surfaces,
        "upfront_extra_tokens": headline["upfront_extra_tokens"] if headline else 0,
        "break_even_tasks": headline["break_even_tasks"] if headline else None,
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
