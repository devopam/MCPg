"""Console entry point for the MCPg server (``mcpg`` / ``python -m mcpg``)."""

from __future__ import annotations

import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcpg.config import ConfigError, load_settings
from mcpg.server import run


def main() -> int:
    """Load configuration from the environment and run the server.

    Returns:
        A process exit code: 0 on clean shutdown, 1 on a configuration error.
    """
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"mcpg: configuration error: {exc}", file=sys.stderr)
        return 1
    run(settings)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
