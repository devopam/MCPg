"""Token counting for the Tier-A accounting.

Wraps ``tiktoken`` with a cached encoder. We count with a single reference
encoding (``o200k_base``, the current GPT-4o / GPT-5 family BPE) — exact counts
vary a little across model tokenizers, but the **ratio** between MCPg's compact
output and the raw-SQL equivalent (which is the claim) is stable across them, so
one well-known reference is enough and keeps the result reproducible.

``tiktoken`` is a dev-only ``bench`` dependency; import errors point the operator
at ``uv sync --group bench``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tiktoken

DEFAULT_ENCODING = "o200k_base"


@lru_cache(maxsize=4)
def _encoder(encoding: str) -> tiktoken.Encoding:
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - dev-only dependency
        raise SystemExit(
            "tiktoken is required for the token accounting. Install the bench group:\n  uv sync --group bench"
        ) from exc
    return tiktoken.get_encoding(encoding)


def count_tokens(text: str, encoding: str = DEFAULT_ENCODING) -> int:
    """Return the number of tokens ``text`` encodes to under ``encoding``."""
    return len(_encoder(encoding).encode(text))
