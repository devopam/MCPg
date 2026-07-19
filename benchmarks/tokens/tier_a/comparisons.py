"""The Tier-A comparisons — MCPg's compact output vs the raw-SQL equivalent.

Each comparison produces two strings: what MCPg's purpose-built tool returns,
and the raw-SQL equivalent an agent would otherwise pull and interpret itself.
The runner tokenizes both. Everything is measured against a live database (or,
for the tool-context cost, the live tool registry) — never estimated.

Two flavours:

* **Per-call savings** (``schema``, ``query-plan``) — MCPg returns a compact,
  structured view; the raw equivalent is the underlying catalog / ``EXPLAIN``
  output. MCPg is smaller, and by how much is the point.
* **Upfront cost** (``tool-context``) — the honest other side of the ledger:
  exposing MCPg's full tool surface costs more context up front than a single
  bare ``run_select`` tool. Reported so the break-even can be computed, never
  hidden.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from mcpg import introspection, query
from mcpg.config import Settings
from mcpg.server import create_server
from mcpg.sql import SqlDriver

# A self-contained analytical query over the catalog — a real plan (scan + join
# + aggregate + sort) on *any* PostgreSQL, so the query-plan comparison does not
# depend on a particular dataset being loaded.
_PLAN_SQL = (
    "SELECT t.table_schema, count(DISTINCT t.table_name) AS tables, count(c.column_name) AS columns "
    "FROM information_schema.tables t "
    "JOIN information_schema.columns c "
    "  ON c.table_schema = t.table_schema AND c.table_name = t.table_name "
    "GROUP BY t.table_schema ORDER BY columns DESC"
)


async def compact_schema_vs_information_schema(driver: SqlDriver, schema: str) -> tuple[str, str]:
    """MCPg ``get_compact_schema`` vs a raw ``information_schema.columns`` dump."""
    mcpg_text = await introspection.get_compact_schema(driver, schema)
    rows = await query.run_select(
        driver,
        "SELECT table_name, column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' "
        "ORDER BY table_name, ordinal_position",
        max_rows=100_000,
    )
    raw_text = json.dumps(rows.rows, default=str)
    return mcpg_text, raw_text


async def analyze_plan_vs_raw_explain(driver: SqlDriver) -> tuple[str, str]:
    """MCPg ``analyze_query_plan`` (structured) vs raw ``EXPLAIN (FORMAT JSON)``."""
    analysis = await query.analyze_query_plan(driver, _PLAN_SQL)
    mcpg_text = json.dumps(asdict(analysis), default=str)
    raw = await query.explain_query(driver, _PLAN_SQL)
    raw_text = json.dumps(raw.plan, default=str)
    return mcpg_text, raw_text


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    """Serialize a FastMCP Tool to the JSON shape sent in a tools/list response."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.inputSchema,
    }


async def tool_context_full_vs_bare(settings: Settings) -> tuple[str, str]:
    """MCPg's **full** tool-schema context vs a lone ``run_select`` tool.

    This is the *upfront* cost — the tool definitions the model carries every
    turn. MCPg is bigger here (that is the honest cost of a rich surface); the
    per-call savings above are what pay it back after a handful of tasks.
    """
    server = create_server(settings)
    tools = await server.list_tools()
    full = json.dumps([_tool_to_dict(t) for t in tools])
    bare = json.dumps([_tool_to_dict(t) for t in tools if t.name == "run_select"])
    return full, bare
