"""Docker MCP Registry ``tools.json`` drift guard.

``packaging/docker-mcp-registry/tools.json`` is the bypass file submitted
alongside ``server.yaml`` to github.com/docker/mcp-registry — MCPg requires
a live ``MCPG_DATABASE_URL`` to start (see ``mcpg.config.load_settings``),
so their build sandbox can't run the container to auto-discover tools the
way it does for DB-less servers. Per their CONTRIBUTING.md, a checked-in
``tools.json`` (name / description / arguments per tool) bypasses that
live-container verification.

It's generated from ``tests/contract/tool_surface.snapshot.json`` — the
same contract snapshot ``test_doc_tables.py`` and ``about.py`` treat as the
tool-surface source of truth — by
``packaging/docker-mcp-registry/generate_tools_json.py``. This test asserts
the checked-in file still matches what the generator produces, so a tool
added, removed, or reshaped without regenerating can't silently drift the
registry submission out of sync with the real server.

When you add/remove a tool or change its schema, regenerate::

    python packaging/docker-mcp-registry/generate_tools_json.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

_REPO = Path(__file__).resolve().parents[2]
_GENERATOR_PATH = _REPO / "packaging" / "docker-mcp-registry" / "generate_tools_json.py"
_TOOLS_JSON_PATH = _REPO / "packaging" / "docker-mcp-registry" / "tools.json"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_tools_json", _GENERATOR_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tools_json_matches_generator_output() -> None:
    generator = _load_generator()
    snapshot = json.loads(generator.SNAPSHOT_PATH.read_text(encoding="utf-8"))
    expected = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "arguments": generator._flatten_arguments(tool["inputSchema"]),
        }
        for tool in snapshot["tools"]
    ]

    checked_in = json.loads(_TOOLS_JSON_PATH.read_text(encoding="utf-8"))

    assert checked_in == expected, (
        "packaging/docker-mcp-registry/tools.json is out of sync with "
        "tests/contract/tool_surface.snapshot.json — regenerate via "
        "`python packaging/docker-mcp-registry/generate_tools_json.py`"
    )


def test_tools_json_covers_every_registered_tool() -> None:
    """Guards against a silently-truncated or stale bypass file."""
    snapshot = json.loads((_REPO / "tests" / "contract" / "tool_surface.snapshot.json").read_text(encoding="utf-8"))
    checked_in = json.loads(_TOOLS_JSON_PATH.read_text(encoding="utf-8"))

    assert len(checked_in) == snapshot["_meta"]["tool_count"]
    assert {t["name"] for t in checked_in} == {t["name"] for t in snapshot["tools"]}
