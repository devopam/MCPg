"""Sync the Cursor plugin manifest version to a release version.

Same role ``packaging/mcpb/sync_version.py`` plays for the .mcpb bundle:
the checked-in ``.cursor-plugin/plugin.json`` carries a copy of
``mcpg.__version__``, so a release bump runs this script (and
``tests/unit/test_cursor_plugin.py`` fails CI if it is skipped).

Named distinctly from the MCPB ``sync_version`` module so pytest
collection cannot bind the wrong ``sync()`` through ``sys.modules``.

Usage::

    python3 packaging/cursor/sync_plugin_version.py X.Y.Z
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def plugin_json_path(repo_root: Path) -> Path:
    """Return the Cursor plugin manifest path under ``repo_root``."""
    return repo_root / ".cursor-plugin" / "plugin.json"


def sync(manifest_path: Path, version: str) -> None:
    """Rewrite the plugin manifest ``version`` in place."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1]:
        raise SystemExit("usage: sync_plugin_version.py <version>")
    path = plugin_json_path(Path(__file__).resolve().parents[2])
    sync(path, sys.argv[1])
    print(f"Cursor plugin pinned to {sys.argv[1]}")
