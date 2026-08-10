"""Tests for server.json — the MCP registry manifest.

Published automatically by the ``publish-mcp-registry`` job in
publish.yml, which re-derives ``version`` from the release tag via
``tools/sync_server_json_version.py`` right before ``mcp-publisher
publish`` (same pattern as the .mcpb bundle's sync_version.py — see
test_mcpb_bundle.py). That CI step is a safety net; these tests are
the primary guard, pinning the sync script and catching the checked-in
file drifting between releases the way
``test_bundle_pyproject_pins_the_current_release`` does for the bundle.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SERVER_JSON = _ROOT / "server.json"

sys.path.insert(0, str(_ROOT / "tools"))
from sync_server_json_version import sync  # noqa: E402


def test_sync_patches_version_and_every_package(tmp_path: Path) -> None:
    scratch = tmp_path / "server.json"
    scratch.write_text(_SERVER_JSON.read_text(encoding="utf-8"), encoding="utf-8")

    sync(scratch, "9.9.9")

    data = json.loads(scratch.read_text(encoding="utf-8"))
    assert data["version"] == "9.9.9"
    assert data["packages"] and all(pkg["version"] == "9.9.9" for pkg in data["packages"])


def test_server_json_pins_the_current_release() -> None:
    """The checked-in version tracks mcpg's own version between releases.

    The publish workflow re-syncs from the tag at release time, but
    keeping the checked-in copy current means the repo never shows a
    stale version to anyone reading server.json directly, and a
    forgotten manual bump fails CI instead of silently shipping.
    """
    from mcpg import __version__

    data = json.loads(_SERVER_JSON.read_text(encoding="utf-8"))
    assert data["version"] == __version__, (
        "server.json's checked-in version is stale — run "
        f"`python3 tools/sync_server_json_version.py {__version__}` "
        "as part of the release version bump (docs/release-process.md §4.2)."
    )
    assert all(pkg["version"] == __version__ for pkg in data["packages"])


def test_server_json_has_website_url() -> None:
    data = json.loads(_SERVER_JSON.read_text(encoding="utf-8"))
    assert data["websiteUrl"] == "https://devopam.github.io/MCPg/"
