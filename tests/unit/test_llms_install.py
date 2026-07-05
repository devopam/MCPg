"""Sanity checks for the Cline agent-install guide (llms-install.md).

Cline (and similar agents) read llms-install.md to configure MCPg
autonomously. If the recipe drifts from what MCPg actually accepts, an
agent following it produces a broken config — and, unlike a human
reader, it won't notice. These tests pin the load-bearing facts.
"""

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = (_ROOT / "llms-install.md").read_text(encoding="utf-8")


def test_the_config_block_is_valid_json_and_wires_the_real_command() -> None:
    # Pull the mcpServers JSON block out of the guide and parse it.
    block = re.search(r"```json\n(\{.*?\"mcpServers\".*?\})\n```", _GUIDE, re.DOTALL)
    assert block, "llms-install.md must contain a fenced mcpServers JSON block"
    config = json.loads(block.group(1))

    server = config["mcpServers"]["mcpg"]
    assert server["command"] == "uvx"
    assert server["args"] == ["mcpg"]
    # The one env var MCPg actually requires to start.
    assert "MCPG_DATABASE_URL" in server["env"]


def test_guide_names_the_real_cli_and_demo_flags() -> None:
    # The demo commands it advertises must be the ones the CLI exposes.
    from mcpg import __main__

    source = Path(__main__.__file__).read_text(encoding="utf-8")
    assert '"--demo"' in source and '"--demo-drop"' in source
    assert "mcpg --demo" in _GUIDE
    assert "mcpg --demo-drop" in _GUIDE


def test_guide_keeps_the_safe_default_posture() -> None:
    # It must not instruct the agent to open write/DDL gates by default.
    lowered = _GUIDE.lower()
    assert "read-only" in lowered
    # If it mentions the unrestricted escalation at all, it must be
    # gated behind an explicit user request, never the default recipe.
    if "unrestricted" in lowered:
        assert "explicitly" in lowered
    # Two safety constraints the recipe must keep telling the agent, or
    # an autonomous install silently loses them: never fabricate creds,
    # and remote hosts need TLS. (Strip markdown emphasis so the phrase
    # match doesn't hinge on **bold** markers.)
    plain = lowered.replace("*", "")
    assert "sslmode=require" in plain
    assert "credential" in plain and ("do not invent" in plain or "hard-code" in plain)
