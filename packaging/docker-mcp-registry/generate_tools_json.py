#!/usr/bin/env python3
"""Generate the Docker MCP Registry ``tools.json`` bypass file.

The Docker MCP Registry (``github.com/docker/mcp-registry``) normally
verifies a submitted server's tool list by running its container and
calling ``tools/list``. MCPg can't clear that bar in their build sandbox:
``MCPG_DATABASE_URL`` is a hard-required setting (see
``mcpg.config.load_settings``), so the process exits before it can answer
any MCP request unless a live, reachable Postgres is supplied. Their
CONTRIBUTING.md offers an explicit escape hatch for exactly this case: a
hand-authored (or, better, generated) ``tools.json`` listing every tool's
``name`` / ``description`` / ``arguments`` bypasses the live-container
check entirely.

This script derives that file from the one artifact in this repo that
already has the full, guarded tool surface: the contract snapshot at
``tests/contract/tool_surface.snapshot.json`` (regenerated via
``MCPG_REGENERATE_TOOL_SNAPSHOT=1``, per CLAUDE.md). Per this project's
"generated beats hand-maintained" rule, nobody should ever hand-edit the
output — rerun this script whenever the snapshot changes.

The registry's ``tools.json`` shape (confirmed 2026-08-14 against real
entries in docker/mcp-registry, e.g. ``servers/database-server``) is a
flattened argument list, NOT the MCP-native JSON-Schema ``inputSchema``:

    [
      {
        "name": "<tool name>",
        "description": "<tool description>",
        "arguments": [
          {"name": "<param>", "type": "<json type>", "desc": "<param description>"}
        ]
      },
      ...
    ]

Usage:
    python packaging/docker-mcp-registry/generate_tools_json.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SNAPSHOT_PATH = REPO_ROOT / "tests" / "contract" / "tool_surface.snapshot.json"
OUTPUT_PATH = Path(__file__).resolve().parent / "tools.json"

# Fallback when a property has no resolvable type (bare ``{"title": "..."}``
# schemas, seen for a couple of Any-typed passthrough params) — every
# observed real-world tools.json entry uses "string" as the type for
# untyped/loosely-typed arguments, so this matches established convention
# rather than inventing a new one.
_FALLBACK_TYPE = "string"


def _resolve_type(prop_schema: dict[str, Any]) -> str:
    """Best-effort JSON-Schema-property -> flat type-string mapping.

    Handles the shapes seen across MCPg's 254-tool surface plus one not
    currently emitted but legal under JSON Schema 2020-12: a plain ``type``
    key (string or, per spec, a list like ``["string", "null"]`` — the
    first non-null entry wins, same rule as ``anyOf`` below), an ``anyOf``
    (Pydantic's ``Optional[X]`` rendering, e.g.
    ``[{"type": "string"}, {"type": "null"}]``), and schemas missing
    ``type`` entirely (Any-typed params).
    """
    raw_type = prop_schema.get("type")
    if isinstance(raw_type, list):
        for candidate in raw_type:
            if candidate and candidate != "null":
                return str(candidate)
    elif raw_type is not None:
        return str(raw_type)
    if "anyOf" in prop_schema:
        for branch in prop_schema["anyOf"]:
            branch_type = branch.get("type")
            if branch_type and branch_type != "null":
                return str(branch_type)
    return _FALLBACK_TYPE


def _flatten_arguments(input_schema: dict[str, Any]) -> list[dict[str, str]]:
    properties: dict[str, Any] = input_schema.get("properties", {})
    arguments = []
    for name, prop_schema in properties.items():
        desc = prop_schema.get("description") or prop_schema.get("title") or ""
        arguments.append(
            {
                "name": name,
                "type": _resolve_type(prop_schema),
                "desc": desc,
            }
        )
    return arguments


def main() -> None:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    tools = snapshot["tools"]

    entries = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "arguments": _flatten_arguments(tool["inputSchema"]),
        }
        for tool in tools
    ]

    OUTPUT_PATH.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} tool entries to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
