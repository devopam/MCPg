"""Tests for the Cursor Marketplace plugin sources.

The plugin is the public Git repo: ``.cursor-plugin/plugin.json`` plus
``packaging/cursor/mcp.json``. Cursor reviews the repository at
https://cursor.com/marketplace/publish. These tests pin the version-sync
script and the install invariants (uvx launch, DSN variable, no secrets).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _ROOT / "packaging" / "cursor"
_SCRIPT = _PLUGIN_DIR / "sync_plugin_version.py"
_SPEC = importlib.util.spec_from_file_location("mcpg_cursor_sync_plugin_version", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_SYNC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SYNC)
plugin_json_path = _SYNC.plugin_json_path
mcp_json_path = _SYNC.mcp_json_path
sync = _SYNC.sync

_MANIFEST = plugin_json_path(_ROOT)
_MCP_JSON = mcp_json_path(_ROOT)


def test_sync_version_patches_the_manifest(tmp_path: Path) -> None:
    scratch = tmp_path / "repo"
    (scratch / ".cursor-plugin").mkdir(parents=True)
    (scratch / "packaging" / "cursor").mkdir(parents=True)
    shutil.copy(_MANIFEST, plugin_json_path(scratch))
    shutil.copy(_MCP_JSON, mcp_json_path(scratch))

    sync(scratch, "9.9.9")

    manifest = json.loads(plugin_json_path(scratch).read_text(encoding="utf-8"))
    assert manifest["version"] == "9.9.9"
    config = json.loads(mcp_json_path(scratch).read_text(encoding="utf-8"))
    assert config["mcpServers"]["mcpg"]["args"] == ["mcpg@9.9.9"]


def test_plugin_json_pins_the_current_release() -> None:
    from mcpg import __version__

    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == __version__, (
        ".cursor-plugin/plugin.json's checked-in version is stale — run "
        f"`python3 packaging/cursor/sync_plugin_version.py {__version__}` "
        "as part of the release version bump (docs/release-process.md §4.2)."
    )


def test_plugin_manifest_invariants() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))

    assert manifest["name"] == "mcpg"
    assert manifest["license"] == "MIT"
    logo = _ROOT / manifest["logo"]
    assert logo.is_file(), manifest["logo"]
    assert ".." not in Path(manifest["logo"]).parts
    assert not Path(manifest["logo"]).is_absolute()

    mcp_path = manifest["mcpServers"]
    assert isinstance(mcp_path, str)
    assert (_ROOT / mcp_path).resolve() == _MCP_JSON.resolve()

    variables = manifest["variables"]
    assert variables["type"] == "object"
    required = set(variables["required"])
    assert required == {"MCPG_DATABASE_URL"}
    props = variables["properties"]
    assert "MCPG_DATABASE_URL" in props
    assert props["MCPG_ACCESS_MODE"]["default"] == "read-only"
    assert set(props["MCPG_ACCESS_MODE"]["enum"]) == {
        "read-only",
        "restricted",
        "unrestricted",
    }


def test_mcp_json_uses_placeholders_not_secrets() -> None:
    config = json.loads(_MCP_JSON.read_text(encoding="utf-8"))
    server = config["mcpServers"]["mcpg"]
    from mcpg import __version__

    assert server["command"] == "uvx"
    assert server["args"] == [f"mcpg@{__version__}"]
    env = server["env"]
    assert env["MCPG_DATABASE_URL"] == "${MCPG_DATABASE_URL}"
    assert env["MCPG_ACCESS_MODE"] == "${MCPG_ACCESS_MODE}"
    blob = _MCP_JSON.read_text(encoding="utf-8").lower()
    assert "postgresql://" not in blob
    assert "password" not in blob
