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


# The tool-surface variants, from the full unrestricted surface down to a
# narrow session-intent preset. Each shrinks the upfront context cost and so
# moves the token break-even left — the honest lever an operator actually pulls.
_TOOL_SURFACES: list[tuple[str, dict[str, str]]] = [
    (
        "full (unrestricted)",
        {
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
        },
    ),
    ("read-only (default)", {"MCPG_ACCESS_MODE": "read-only"}),
    ("intent=lookup", {"MCPG_ACCESS_MODE": "read-only", "MCPG_SESSION_INTENT": "lookup"}),
]


async def _surface_json(database_url: str, env: dict[str, str]) -> tuple[int, str]:
    """Build a server in ``env`` and return (tool count, tools/list JSON)."""
    from mcpg.config import load_settings

    server = create_server(load_settings({"MCPG_DATABASE_URL": database_url, **env}))
    tools = await server.list_tools()
    return len(tools), json.dumps([_tool_to_dict(t) for t in tools])


async def tool_context_surfaces(database_url: str) -> tuple[list[tuple[str, int, str]], str]:
    """Measure MCPg's tool-schema context at each surface, plus a bare baseline.

    Returns ``([(surface_name, tool_count, tools_json), ...], bare_json)`` — the
    *upfront* cost the model carries every turn. MCPg is bigger here (the honest
    cost of a rich surface); the per-call savings repay it after the break-even,
    and narrower surfaces (read-only, session-intent) move that break-even left.
    """
    surfaces: list[tuple[str, int, str]] = []
    for name, env in _TOOL_SURFACES:
        count, text = await _surface_json(database_url, env)
        surfaces.append((name, count, text))
    # Bare baseline: a lone run_select tool (taken from the read-only surface).
    _, ro_json = await _surface_json(database_url, {"MCPG_ACCESS_MODE": "read-only"})
    bare = json.dumps([t for t in json.loads(ro_json) if t["name"] == "run_select"])
    return surfaces, bare
