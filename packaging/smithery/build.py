"""Derive a Smithery-publishable bundle from the canonical MCPB bundle.

Smithery's `mcp publish` accepts an ``.mcpb`` but its parser only knows
the older MCPB runtimes (``python`` / ``node`` / ``binary``) — it does
**not** recognise the ``uv`` server type our canonical Claude Desktop
bundle uses. Rather than maintain a second hand-written manifest (which
would drift), this transforms the one source of truth
(``packaging/mcpb``) into a Smithery-compatible variant:

- ``server.type: "uv"`` → ``"python"`` (a runtime Smithery understands;
  MCPg *is* a Python package, so the label is honest).
- ``server.mcp_config`` → launch via ``uvx mcpg`` directly. This is the
  right local-launch form for Smithery's registry (no bundle unpack
  needed), and because ``uvx mcpg`` always resolves the latest published
  release, the Smithery listing tracks PyPI without a per-release
  re-publish being strictly required.

Everything else — name, version, ``user_config`` (which Smithery turns
into the connection config schema), icon, privacy policies — is carried
across unchanged. The version is expected to already be synced into
``packaging/mcpb`` (via ``sync_version.py``) before this runs.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CANONICAL = _HERE.parent / "mcpb"


def smithery_manifest(canonical: dict) -> dict:
    """Transform the canonical MCPB manifest into a Smithery-compatible one.

    Pure and deterministic — the whole point is that this is testable
    without a network or the packing toolchain.
    """
    manifest = json.loads(json.dumps(canonical))  # deep copy
    server = manifest["server"]
    server["type"] = "python"
    server["mcp_config"] = {
        "command": "uvx",
        "args": ["mcpg"],
        "env": {
            "MCPG_DATABASE_URL": "${user_config.database_url}",
            "MCPG_ACCESS_MODE": "${user_config.access_mode}",
        },
    }
    return manifest


def build(out_dir: Path) -> Path:
    """Assemble a ready-to-pack Smithery bundle directory.

    Copies the canonical bundle's payload (server sources, icon,
    pyproject) and writes the transformed manifest over it. Returns the
    output directory so the caller can ``mcpb pack`` it.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(_CANONICAL, out_dir, ignore=shutil.ignore_patterns("__pycache__"))

    canonical = json.loads((_CANONICAL / "manifest.json").read_text(encoding="utf-8"))
    (out_dir / "manifest.json").write_text(
        json.dumps(smithery_manifest(canonical), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_dir


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else _HERE / "build"
    result = build(target)
    version = json.loads((result / "manifest.json").read_text(encoding="utf-8"))["version"]
    print(f"Built Smithery bundle dir at {result} (mcpg {version})")
