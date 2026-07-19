"""Model client for the Tier-B loop — a thin async wrapper over one provider.

Defaults to Anthropic (Claude). Credentials come from the environment
(``ANTHROPIC_API_KEY``), never a flag, so a key is never written to disk or a
result file. Temperature is pinned to 0 for reproducibility. The interface is
deliberately small (:class:`ModelResponse` + ``complete``) so another provider
can be dropped in without touching the agent loop.

``anthropic`` is an optional ``bench`` dependency, imported lazily; its absence
(or a missing key) raises a clear, actionable error at *run* time — Tier-B is
costed and never runs in CI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol, cast

# A fixed, current default; override with --model. Pinned so a published run
# always states the exact model that produced it.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 2048


@dataclass(frozen=True)
class ModelResponse:
    """One model turn: raw content blocks, why it stopped, and token usage."""

    content: list[dict[str, Any]]  # Anthropic content blocks (text / tool_use)
    stop_reason: str
    input_tokens: int
    output_tokens: int


class ModelClient(Protocol):
    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse: ...


class AnthropicClient:
    """``ModelClient`` backed by the Anthropic Messages API (async, temp 0)."""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dep
            raise SystemExit(
                "The Tier-B study needs the anthropic SDK. Install the bench group:\n  uv sync --group bench"
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit(
                "ANTHROPIC_API_KEY is not set. Tier-B calls a real model (it costs money) — "
                "export your key before running it."
            )
        self._client = anthropic.AsyncAnthropic()
        self._model = model
        self._max_tokens = max_tokens

    async def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelResponse:
        # The SDK types messages/tools as its own TypedDicts; we build plain
        # dicts (accepted at runtime). Cast to keep the loop provider-agnostic.
        resp = await self._client.messages.create(
            model=self._model,
            system=system,
            messages=cast("Any", messages),
            tools=cast("Any", tools),
            temperature=0,
            max_tokens=self._max_tokens,
        )
        return ModelResponse(
            content=[block.model_dump() for block in resp.content],
            stop_reason=resp.stop_reason or "end_turn",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
