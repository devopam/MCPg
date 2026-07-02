"""Console entry point for the MCPg server (``mcpg`` / ``python -m mcpg``)."""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcpg import __version__
from mcpg.config import ConfigError, load_settings
from mcpg.server import run


def _run_demo_command(command: str, database_url: str) -> int:
    """Seed or drop the demo dataset. Returns a process exit code."""
    from mcpg.demo import SUGGESTED_PROMPTS, DemoError, drop_demo, seed_demo

    try:
        if command == "--demo":
            summary = asyncio.run(seed_demo(database_url))
            print(f"Seeded the {summary.schema!r} schema:")
            for table, count in summary.row_counts.items():
                print(f"  {summary.schema}.{table:<12} {count:>6} rows")
            if summary.vector_column_included:
                print("  (pgvector detected — products.embedding included)")
            else:
                print("  (pgvector not installed — vector demos skipped; everything else works)")
            print("\nPoint your MCP client at this database and try asking:")
            for prompt in SUGGESTED_PROMPTS:
                print(f"  • {prompt}")
            print("\nRemove it any time with: mcpg --demo-drop")
        else:
            drop = asyncio.run(drop_demo(database_url))
            if drop.dropped:
                print(f"Dropped the {drop.schema!r} schema.")
            else:
                print(f"Nothing to do — schema {drop.schema!r} does not exist.")
    except DemoError as exc:
        print(f"mcpg: demo error: {exc}", file=sys.stderr)
        return 1
    return 0


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
    # One-shot demo-dataset commands: seed/drop against the configured
    # database, then exit — the server never starts.
    if len(sys.argv) > 1 and sys.argv[1] in {"--demo", "--demo-drop"}:
        return _run_demo_command(sys.argv[1], settings.database_url)
    from mcpg.obs_logging import setup_logging

    setup_logging(settings)
    run(settings)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
