"""Sync the MCPB bundle's pinned versions to a release version.

Called by the publish workflow with the tag-derived version
(``python3 packaging/mcpb/sync_version.py 1.2.3``) right before
``mcpb pack``, so the checked-in bundle files — which carry the
*previous* release's pin — can never ship stale. Same pattern as the
``server.json`` sync step in publish.yml, extracted to a real file so
it can be unit-tested instead of living inline in workflow YAML.

Patches, in the directory this script lives in:

- ``manifest.json``  → ``version``
- ``pyproject.toml`` → ``[project] version`` and the ``mcpg==X.Y.Z`` pin
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def sync(bundle_dir: Path, version: str) -> None:
    """Rewrite the bundle's version pins in place."""
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    pyproject_path = bundle_dir / "pyproject.toml"
    text = pyproject_path.read_text()
    text = re.sub(r'version = "[^"]+"', f'version = "{version}"', text)
    text = re.sub(r"mcpg==[0-9][^\"',\s]*", f"mcpg=={version}", text)
    pyproject_path.write_text(text)


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1]:
        raise SystemExit("usage: sync_version.py <version>")
    sync(Path(__file__).resolve().parent, sys.argv[1])
    print(f"mcpb bundle pinned to {sys.argv[1]}")
