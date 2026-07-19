"""Tier-A token-accounting orchestrator (CLI).

Runs each comparison against a live database (and the live tool registry),
tokenizes both sides with :mod:`benchmarks.tokens.tokenize`, and writes one
structured JSON document. Deterministic and CI-able — no LLM, no cost.

    uv run python -m benchmarks.tokens.tier_a.runner \
        --database-url "$MCPG_TEST_DATABASE_URL" --schema public \
        --output benchmarks/results/tokens-tier-a.json

Operator tool — needs a live PostgreSQL; the pure helpers (tokenize, schema
``derive``) are unit-tested.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Any

from benchmarks.tokens.tier_a import comparisons as cmp
from benchmarks.tokens.tier_a.schema import TokenComparison, TokenReport, break_even, derive
from benchmarks.tokens.tokenize import DEFAULT_ENCODING, count_tokens
from mcpg import __version__
from mcpg.config import load_settings
from mcpg.database import Database


async def _run(args: argparse.Namespace) -> TokenReport:
    settings = load_settings({"MCPG_DATABASE_URL": args.database_url})
    database = Database(settings)
    await database.connect()
    results: list[TokenComparison] = []
    try:
        driver = database.driver()

        mcpg, raw = await cmp.compact_schema_vs_information_schema(driver, args.schema)
        results.append(
            derive(
                f"compact_schema[{args.schema}] vs information_schema",
                "schema",
                count_tokens(mcpg),
                count_tokens(raw),
                {"tool": "get_compact_schema", "raw": "information_schema.columns dump"},
            )
        )

        mcpg, raw = await cmp.analyze_plan_vs_raw_explain(driver)
        results.append(
            derive(
                "analyze_query_plan vs raw EXPLAIN",
                "query-plan",
                count_tokens(mcpg),
                count_tokens(raw),
                {"tool": "analyze_query_plan", "raw": "EXPLAIN (FORMAT JSON)"},
            )
        )
    finally:
        await database.close()

    # Upfront tool-schema context cost (no DB needed for the counting itself).
    full, bare = await cmp.tool_context_full_vs_bare(settings)
    results.append(
        derive(
            "full tool surface vs bare run_select (upfront)",
            "tool-context",
            count_tokens(full),
            count_tokens(bare),
            {"note": "upfront cost, not a per-call saving; MCPg is larger here by design"},
        )
    )

    metadata: dict[str, Any] = {
        "timestamp": args.timestamp,
        "git_sha": args.git_sha,
        "mcpg_version": __version__,
        "encoding": DEFAULT_ENCODING,
        "schema": args.schema,
        "break_even": break_even(results),
        "host": {"python": platform.python_version(), "os": platform.platform(), "machine": platform.machine()},
    }
    return TokenReport(metadata=metadata, comparisons=results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPg Tier-A token-efficiency accounting (deterministic).")
    parser.add_argument("--database-url", required=True, help="PostgreSQL DSN (a schema with tables loaded).")
    parser.add_argument("--schema", default="public", help="Schema to compare (default: public).")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the result JSON.")
    parser.add_argument("--git-sha", default="unknown", help="Provenance: the commit under test.")
    parser.add_argument("--timestamp", default="unknown", help="Provenance: ISO-8601 run timestamp.")
    args = parser.parse_args(argv)

    report = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    for c in report.comparisons:
        if c.category == "tool-context":
            # Upfront cost, not a per-call saving — present it as such.
            print(f"  {c.name:52} mcpg={c.mcpg_tokens:6} raw={c.raw_tokens:6}  upfront cost (MCPg is larger)")
        else:
            print(f"  {c.name:52} mcpg={c.mcpg_tokens:6} raw={c.raw_tokens:6}  savings={c.savings_pct:+.0f}%")
    be = report.metadata["break_even"]
    print(
        f"  break-even: MCPg's upfront tool surface (+{be['upfront_extra_tokens']} tok) is repaid after "
        f"~{be['break_even_tasks']} tasks (mean saving {be['mean_per_call_saving_tokens']:.0f} tok/task)"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
