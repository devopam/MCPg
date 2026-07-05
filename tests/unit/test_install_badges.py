"""Consistency tests for the one-click install deeplinks.

The encoded Cursor / VS Code install URLs appear in both README.md and
docs/integrations.md. They're opaque blobs, so drift between the two
copies — or a payload that silently stops decoding to a valid config —
would never be caught by eye. These tests decode the payloads out of
both committed files and pin their shape and equality.
"""

import base64
import json
import re
import urllib.parse
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_CURSOR_RE = re.compile(r"cursor\.com/install-mcp\?name=mcpg&config=([A-Za-z0-9%=+/]+)\)")
_VSCODE_RE = re.compile(r"(?<!insiders\.)vscode\.dev/redirect\?url=([^)\s]+)\)")


def _payloads(pattern: re.Pattern[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for rel in ("README.md", "docs/integrations.md"):
        match = pattern.search((_ROOT / rel).read_text(encoding="utf-8"))
        assert match, f"{rel}: no install link matching {pattern.pattern!r}"
        found[rel] = match.group(1)
    return found


def test_cursor_deeplink_decodes_and_matches_across_files() -> None:
    payloads = _payloads(_CURSOR_RE)
    assert len(set(payloads.values())) == 1, f"Cursor configs drifted between files: {payloads}"
    config = json.loads(base64.b64decode(urllib.parse.unquote(next(iter(payloads.values())))))
    assert config["command"] == "uvx"
    assert config["args"] == ["mcpg"]
    assert "MCPG_DATABASE_URL" in config["env"]


def test_vscode_deeplink_decodes_and_matches_across_files() -> None:
    payloads = _payloads(_VSCODE_RE)
    assert len(set(payloads.values())) == 1, f"VS Code configs drifted between files: {payloads}"
    inner = urllib.parse.unquote(next(iter(payloads.values())))
    assert inner.startswith("vscode:mcp/install?")
    config = json.loads(urllib.parse.unquote(inner.split("?", 1)[1]))
    assert config["name"] == "mcpg"
    assert config["command"] == "uvx"
    assert config["args"] == ["mcpg"]
    # The connection URL must be a masked prompt, never a plain-text value.
    assert config["env"]["MCPG_DATABASE_URL"] == "${input:database_url}"
    (prompt,) = config["inputs"]
    assert prompt["type"] == "promptString"
    assert prompt["password"] is True
