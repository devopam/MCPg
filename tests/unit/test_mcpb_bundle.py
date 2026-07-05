"""Tests for the MCPB desktop-extension bundle sources.

The bundle in packaging/mcpb/ is packed and attached to every GitHub
release by the publish workflow. These tests pin the two things that
would break silently: the version-sync script the workflow runs, and
the manifest invariants the Claude Desktop install experience relies
on (sensitive connection URL, read-only default, uv server type).
"""

import json
import shutil
import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parents[2] / "packaging" / "mcpb"

sys.path.insert(0, str(_BUNDLE_DIR))
from sync_version import sync  # noqa: E402


def test_sync_version_patches_every_pin(tmp_path: Path) -> None:
    scratch = tmp_path / "mcpb"
    shutil.copytree(_BUNDLE_DIR, scratch)

    sync(scratch, "9.9.9")

    manifest = json.loads((scratch / "manifest.json").read_text())
    assert manifest["version"] == "9.9.9"
    pyproject = (scratch / "pyproject.toml").read_text()
    assert 'version = "9.9.9"' in pyproject
    assert '"mcpg==9.9.9"' in pyproject
    # No stale pin of any other version survives.
    assert pyproject.count("mcpg==") == 1


def test_manifest_invariants() -> None:
    manifest = json.loads((_BUNDLE_DIR / "manifest.json").read_text())

    # uv server type is what keeps the bundle tiny and cross-platform.
    assert manifest["server"]["type"] == "uv"
    entry_point = manifest["server"]["entry_point"]
    assert (_BUNDLE_DIR / entry_point).is_file()

    # The connection URL must be keychain-stored and mandatory; the
    # access mode must default to MCPg's safe posture.
    db = manifest["user_config"]["database_url"]
    assert db["sensitive"] is True
    assert db["required"] is True
    assert manifest["user_config"]["access_mode"]["default"] == "read-only"

    # The env plumbing the server actually reads.
    env = manifest["server"]["mcp_config"]["env"]
    assert env["MCPG_DATABASE_URL"] == "${user_config.database_url}"
    assert env["MCPG_ACCESS_MODE"] == "${user_config.access_mode}"

    # 252 tools are not enumerated by hand — the host derives them.
    assert manifest["tools_generated"] is True
    assert manifest["prompts_generated"] is True

    # Connector-directory requirements: a bundled icon and an HTTPS
    # privacy_policies array ("missing or incomplete privacy policies
    # result in immediate rejection", per the submission docs).
    assert (_BUNDLE_DIR / manifest["icon"]).is_file()
    policies = manifest["privacy_policies"]
    assert policies and all(url.startswith("https://") for url in policies)


def test_bundle_pyproject_pins_the_current_release() -> None:
    """The checked-in pin tracks mcpg's own version between releases.

    The publish workflow re-syncs from the tag at release time, but
    keeping the checked-in files current means a bundle packed from a
    source checkout is never silently one release behind.
    """
    from mcpg import __version__

    pyproject = (_BUNDLE_DIR / "pyproject.toml").read_text()
    assert f"mcpg=={__version__}" in pyproject
    manifest = json.loads((_BUNDLE_DIR / "manifest.json").read_text())
    assert manifest["version"] == __version__
