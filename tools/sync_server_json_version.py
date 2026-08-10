"""Sync server.json's version fields to a release version.

Used two ways:

- By the release version bump (docs/release-process.md §4.2), so the
  checked-in file tracks the release instead of drifting — the same
  role ``packaging/mcpb/sync_version.py`` plays for the .mcpb bundle.
- By the ``publish-mcp-registry`` job in publish.yml, which re-runs it
  against the tag-derived version right before ``mcp-publisher
  publish``. That's a safety net, not the primary mechanism: it's what
  stops a forgotten manual bump from making the registry reject the
  release as a duplicate version (the failure mode that motivated
  deriving from the tag in the first place — see git blame on that
  step). ``tests/unit/test_server_json.py`` guards the manual side so
  it's rarely needed in practice.

Patches ``server.json`` in place: the top-level ``version`` and every
``packages[].version``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SERVER_JSON = Path(__file__).resolve().parents[1] / "server.json"


def sync(path: Path, version: str) -> None:
    """Rewrite ``path``'s version pins in place."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    for pkg in data.get("packages", []):
        pkg["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1]:
        raise SystemExit("usage: sync_server_json_version.py <version>")
    sync(_SERVER_JSON, sys.argv[1])
    print(f"server.json pinned to {sys.argv[1]}")
