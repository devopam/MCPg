"""Generate docs/demo.md from real tool runs against the demo dataset.

The walkthrough document is *captured, not written*: every output block
in docs/demo.md is the genuine result of running the pivotal MCPg tools
against the ``mcpg_demo`` schema seeded by ``mcpg --demo``. Because the
dataset is deterministic, regenerating the doc produces the same numbers
a user sees on their own machine.

Usage (needs a scratch database; the demo schema is seeded if absent
and left in place afterwards)::

    MCPG_TEST_DATABASE_URL=postgresql://... uv run python tools/generate_demo_walkthrough.py

The integration suite (tests/integration/test_demo_integration.py)
asserts the same underlying invariants — planted index finding, FTS
hits, advisor findings — so the walkthrough can't silently rot even
between regenerations.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcpg.advisors import find_sensitive_columns
from mcpg.config import load_settings
from mcpg.database import Database
from mcpg.demo import DEMO_SCHEMA, seed_demo
from mcpg.graph_projection import generate_graph_projection
from mcpg.indexing import recommend_indexes
from mcpg.naming import lint_naming_conventions
from mcpg.query import analyze_query_plan, run_select
from mcpg.textsearch import full_text_search

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "demo.md"

_SLOW_QUERY = f"SELECT * FROM {DEMO_SCHEMA}.orders WHERE customer_id = 42 ORDER BY order_date DESC"

_REVENUE_QUERY = f"""
SELECT date_trunc('month', order_date)::date AS month,
       count(*) AS orders,
       round(sum(total_cents) / 100.0, 2) AS revenue_usd
FROM {DEMO_SCHEMA}.orders
WHERE status <> 'cancelled'
GROUP BY 1 ORDER BY 1 DESC LIMIT 6
""".strip()


def _render(value: Any, *, max_list_items: int = 5) -> str:
    """Render a helper result as pretty JSON, truncating long lists."""

    def _clip(node: Any) -> Any:
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            node = dataclasses.asdict(node)
        if isinstance(node, dict):
            return {k: _clip(v) for k, v in node.items()}
        if isinstance(node, list):
            clipped = [_clip(item) for item in node[:max_list_items]]
            if len(node) > max_list_items:
                clipped.append(f"... ({len(node) - max_list_items} more)")
            return clipped
        return node

    return json.dumps(_clip(value), indent=2, default=str)


async def _build_sections() -> list[str]:
    url = os.environ.get("MCPG_TEST_DATABASE_URL")
    if not url:
        raise SystemExit("Set MCPG_TEST_DATABASE_URL to a scratch database first.")

    settings = load_settings({"MCPG_DATABASE_URL": url})
    database = Database(settings)
    await database.connect()
    driver = database.driver()
    sections: list[str] = []
    try:
        exists = await driver.execute_query("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'mcpg_demo'")
        if not exists:
            print("Demo schema absent — seeding it first...")
            await seed_demo(url)

        # 1. Schema orientation ------------------------------------------------
        from mcpg.composite import summarize_table

        summary = await summarize_table(driver, DEMO_SCHEMA, "orders", sample_rows=3)
        sections.append(
            _section(
                "Get oriented: what does this table look like?",
                "Summarise the orders table — shape, columns, indexes, a few sample rows.",
                f'summarize_table(schema="{DEMO_SCHEMA}", table="orders", sample_rows=3)',
                _render(summary),
            )
        )

        # 2. Ad-hoc analytics --------------------------------------------------
        revenue = await run_select(driver, _REVENUE_QUERY)
        sections.append(
            _section(
                "Ask an analytics question in SQL",
                "What's the monthly order volume and revenue for the last six months (excluding cancellations)?",
                f"run_select(sql=...)\n\n```sql\n{_REVENUE_QUERY}\n```",
                _render(revenue, max_list_items=6),
            )
        )

        # 3. The planted performance problem ----------------------------------
        plan = await analyze_query_plan(driver, _SLOW_QUERY)
        sections.append(
            _section(
                "Diagnose a slow query",
                f"Why is this slow? `{_SLOW_QUERY}`",
                f'analyze_query_plan(sql="{_SLOW_QUERY}")',
                _render(plan),
                postscript=(
                    "The plan shows a **sequential scan over every order** to find one customer's "
                    "rows — `orders.customer_id` is a foreign key with no covering index. That's "
                    "the dataset's planted flaw, and exactly what the next tool catches."
                ),
            )
        )

        # 4. Index advisor. It reads pg_stat_user_tables, which only sees
        # traffic that actually happened — and seeding itself generates
        # thousands of FK-check *index* scans on orders' primary key,
        # drowning the seq-scan signal. Reset the counters (scratch
        # database — the doc requires one), then replay the slow query a
        # few times so the stats reflect the workload, not the seeder.
        await driver.execute_query("SELECT pg_stat_reset()")
        # The reset also zeroes n_live_tup, which the advisor uses as its
        # size floor — re-ANALYZE to repopulate it.
        for table in ("customers", "products", "orders", "order_items", "reviews"):
            await driver.execute_query(f"ANALYZE {DEMO_SCHEMA}.{table}")  # type: ignore[arg-type]
        for _ in range(8):
            await driver.execute_query(_SLOW_QUERY)  # type: ignore[arg-type]
        # Pending per-backend stats flush at transaction end, so a fixed
        # sleep is a race. Poll until the scans are visible — each poll is
        # itself a transaction, which drives the flush.
        for _ in range(20):
            visible = await driver.execute_query(
                "SELECT seq_scan FROM pg_stat_user_tables WHERE schemaname = 'mcpg_demo' AND relname = 'orders'"
            )
            if visible and visible[0].cells["seq_scan"] >= 8:
                break
            await asyncio.sleep(0.5)
        recommendations = await recommend_indexes(driver, min_live_tuples=1000)
        demo_recs = [r for r in recommendations if getattr(r, "schema", DEMO_SCHEMA) == DEMO_SCHEMA]
        sections.append(
            _section(
                "Let the index advisor find it",
                "Recommend indexes for this database and explain the reasoning.",
                "recommend_indexes(min_live_tuples=1000)",
                _render(demo_recs or recommendations),
            )
        )

        # 5. Full-text search --------------------------------------------------
        matches = await full_text_search(driver, DEMO_SCHEMA, "reviews", "review_text", '"battery life"', limit=5)
        sections.append(
            _section(
                "Search customer reviews in natural language",
                "Find reviews that mention battery life.",
                f'full_text_search(schema="{DEMO_SCHEMA}", table="reviews", column="review_text", '
                "search_query='\"battery life\"', limit=5)",
                _render(matches),
            )
        )

        # 6. Governance advisors -----------------------------------------------
        sensitive = await find_sensitive_columns(driver, DEMO_SCHEMA)
        naming = await lint_naming_conventions(driver, DEMO_SCHEMA)
        sections.append(
            _section(
                "Audit for PII and naming drift",
                "Scan the schema for sensitive columns and naming-convention violations.",
                f'find_sensitive_columns(schema="{DEMO_SCHEMA}") + lint_naming_conventions(schema="{DEMO_SCHEMA}")',
                _render(sensitive) + "\n\n" + _render(naming),
                postscript=(
                    "`customers.email` / `customers.phone` are flagged as PII, and the camelCase "
                    '`reviews."reviewSource"` column trips the naming linter — both planted on purpose.'
                ),
            )
        )

        # 7. Graph projection (emit-only) ---------------------------------------
        projection = await generate_graph_projection(driver, DEMO_SCHEMA)
        sections.append(
            _section(
                "Project the schema into a property graph",
                "Model this schema as a graph — customers, products, orders as vertices; foreign keys as edges.",
                f'generate_graph_projection(schema="{DEMO_SCHEMA}")',
                _render(projection),
                postscript=(
                    "The openCypher statements are **generated for review, never executed** — the same "
                    "emit-don't-execute pattern `generate_test_data` and `recommend_redistribute` follow."
                ),
            )
        )
    finally:
        await database.close()
    return sections


def _section(title: str, ask: str, call: str, output: str, *, postscript: str | None = None) -> str:
    parts = [
        f"## {title}",
        f"**You ask:** *{ask}*",
        f"**MCPg runs:** {call}",
        f"```json\n{output}\n```",
    ]
    if postscript:
        parts.append(postscript)
    return "\n\n".join(parts)


_HEADER = f"""\
# The MCPg demo dataset — a guided tour

Seed a small, curated e-commerce dataset into any scratch database and
take MCPg's pivotal tools for a spin against data engineered to show
them off:

```bash
MCPG_DATABASE_URL=postgresql://... mcpg --demo    # seed the {DEMO_SCHEMA} schema
MCPG_DATABASE_URL=postgresql://... mcpg --demo-drop   # remove it again
```

The dataset (~400 customers, 120 products, 3,000 orders, 900 reviews)
is deterministic — same rows every seed — and **curated**: it plants a
missing foreign-key index, PII-shaped columns, a naming violation, and
review prose worth searching, so every tool below has something real to
find. Everything lives in one `{DEMO_SCHEMA}` schema; nothing else in
your database is touched.

> Every output block below is captured from a real run against the
> seeded dataset (regenerate with
> `uv run python tools/generate_demo_walkthrough.py`). Numbers you see
> here are the numbers you'll get.
"""


def main() -> int:
    sections = asyncio.run(_build_sections())
    OUTPUT_PATH.write_text(_HEADER + "\n" + "\n\n---\n\n".join(sections) + "\n")
    print(f"Wrote {OUTPUT_PATH} ({len(sections)} sections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
