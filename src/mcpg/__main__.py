"""Console entry point for the MCPg server (``mcpg`` / ``python -m mcpg``)."""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcpg import __version__
from mcpg.config import ConfigError, load_settings
from mcpg.server import run


def main() -> int:
    """Load configuration from the environment and run the server.

    Returns:
        A process exit code: 0 on clean shutdown, 1 on a configuration error.
    """
    # SECURITY.md asks bug reporters to include ``mcpg --version`` in
    # their report; this is the surface that gives them an answer.
    if len(sys.argv) > 1 and sys.argv[1] in {"--version", "-V"}:
        print(f"mcpg {__version__}")
        return 0
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"mcpg: configuration error: {exc}", file=sys.stderr)
        return 1
    from mcpg.obs_logging import setup_logging

    setup_logging(settings)
    run(settings)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
