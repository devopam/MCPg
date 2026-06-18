"""Tool-surface contract test.

Snapshots the MCPg tool catalogue (every tool's name, description and
JSON-Schema input shape) and compares it against a checked-in JSON
snapshot. Any unintended change — a tool rename, a parameter
deletion, a description that drops a security caveat — fails this
test before it lands.

**Intentional changes** to the tool surface are part of normal
development; this test isn't here to block them. When you've made a
deliberate change (added a tool, added a parameter, rewrote a
description), regenerate the snapshot:

.. code-block:: bash

    MCPG_REGENERATE_TOOL_SNAPSHOT=1 \\
        uv run pytest tests/contract/test_tool_surface_snapshot.py

…and commit the resulting ``tool_surface.snapshot.json`` diff along
with the source-code change. The diff is the review surface — a
reviewer can see "this PR added 3 tools" or "this PR widened the
input schema on ``run_write`` to accept a new parameter" without
having to re-read every register-tools helper.

**Why this lives in tests/contract/ rather than tests/unit/**:
the goal is a stable check on the public API surface, not a unit
test of a function. Treating it as a contract test keeps the
intent clear and lets us add other contract tests (e.g. the
JSON-RPC error shapes the MCP server exposes) alongside this one
without polluting the unit-test directory.

The snapshot is produced with every gate flag ON
(``access_mode=unrestricted``, ``allow_ddl=true``, ``allow_shell=true``,
``allow_listen=true``) so every tool that can ever be registered
shows up. Operators running with stricter flags don't lose
coverage — the contract is "this is the maximal surface; the
real surface is a subset of it."
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from mcpg.config import load_settings
from mcpg.tools import register_tools

_SNAPSHOT_PATH = Path(__file__).parent / "tool_surface.snapshot.json"

# Database URL used during snapshot generation — never actually
# connected to; ``register_tools`` only reads ``Settings`` fields
# to decide which tools to expose. A loopback URL avoids the TLS
# enforcement check at ``load_settings`` startup.
_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"


def _build_maximal_server() -> FastMCP:
    """Construct a FastMCP server with every flag enabled.

    Mirrors what an operator would see if they ran MCPg with the
    most permissive configuration — every conditionally-registered
    tool (DDL, shell, listen) is exposed. This is the maximal
    contract surface.
    """
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
        }
    )
    server: FastMCP = FastMCP("mcpg-snapshot")
    register_tools(server, settings)
    return server


def _canonical_tool_record(tool: Any) -> dict[str, Any]:
    """Reduce one MCP ``Tool`` to a stable, diff-friendly dict.

    Drops anything that can move between framework versions
    without indicating a real surface change (e.g. internal handler
    references). The kept fields are exactly what an MCP client
    sees over the wire.
    """
    return {
        "name": tool.name,
        "description": (tool.description or "").strip(),
        "inputSchema": tool.inputSchema,
    }


async def _capture_tool_surface() -> dict[str, Any]:
    """Return a canonical, sorted snapshot of the maximal tool surface."""
    server = _build_maximal_server()
    tools = await server.list_tools()
    records = sorted((_canonical_tool_record(t) for t in tools), key=lambda r: r["name"])
    return {
        "_meta": {
            "tool_count": len(records),
            "schema_version": 1,
        },
        "tools": records,
    }


def _format_canonical(snapshot: dict[str, Any]) -> str:
    """Stable serialisation — sorted keys, 2-space indent, trailing newline."""
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


async def test_tool_surface_matches_snapshot() -> None:
    """The set of tools MCPg exposes — names, descriptions, schemas —
    must match the checked-in snapshot exactly.

    Set ``MCPG_REGENERATE_TOOL_SNAPSHOT=1`` to write a fresh
    snapshot instead of asserting against the existing one. This is
    the "intentional change" escape hatch; the regenerated file
    must be committed along with the source change.
    """
    captured = await _capture_tool_surface()
    captured_text = _format_canonical(captured)

    if os.environ.get("MCPG_REGENERATE_TOOL_SNAPSHOT") == "1":
        _SNAPSHOT_PATH.write_text(captured_text, encoding="utf-8")
        pytest.skip(
            f"Regenerated {_SNAPSHOT_PATH.name} ({captured['_meta']['tool_count']} tools). "
            "Commit the diff alongside the source change."
        )

    if not _SNAPSHOT_PATH.exists():
        pytest.fail(
            f"{_SNAPSHOT_PATH} is missing. "
            "Generate it with: "
            "MCPG_REGENERATE_TOOL_SNAPSHOT=1 uv run pytest "
            "tests/contract/test_tool_surface_snapshot.py"
        )

    expected_text = _SNAPSHOT_PATH.read_text(encoding="utf-8")
    if captured_text == expected_text:
        return

    # On mismatch: surface a focused diagnosis. Diff the parsed
    # records by tool name so the failure message identifies the
    # set delta first, then lets the operator drill into a per-tool
    # diff via the regenerate command.
    expected = json.loads(expected_text)
    expected_names = {t["name"] for t in expected["tools"]}
    captured_names = {t["name"] for t in captured["tools"]}
    added = sorted(captured_names - expected_names)
    removed = sorted(expected_names - captured_names)

    expected_by_name = {t["name"]: t for t in expected["tools"]}
    captured_by_name = {t["name"]: t for t in captured["tools"]}
    changed = sorted(
        name for name in expected_names & captured_names if expected_by_name[name] != captured_by_name[name]
    )

    lines = ["mcpg tool surface drifted from snapshot."]
    if added:
        lines.append(f"  added ({len(added)}): {', '.join(added)}")
    if removed:
        lines.append(f"  removed ({len(removed)}): {', '.join(removed)}")
    if changed:
        lines.append(f"  changed ({len(changed)}): {', '.join(changed)}")
    lines.append("")
    lines.append("If this is intentional, regenerate the snapshot:")
    lines.append("  MCPG_REGENERATE_TOOL_SNAPSHOT=1 uv run pytest tests/contract/test_tool_surface_snapshot.py")
    lines.append("…and commit the resulting tool_surface.snapshot.json diff.")
    pytest.fail("\n".join(lines))
