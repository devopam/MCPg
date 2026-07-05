"""Tests for the Smithery bundle transform (packaging/smithery/build.py).

The Smithery listing is derived from the canonical MCPB bundle by a
pure transform. These pin the transform's contract so the derived
bundle can't silently stop being Smithery-publishable (wrong runtime
type) or lose the config schema Smithery builds from ``user_config``.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "packaging" / "smithery"))
from build import build, smithery_manifest  # noqa: E402

_CANONICAL = json.loads((_ROOT / "packaging" / "mcpb" / "manifest.json").read_text(encoding="utf-8"))


def test_transform_uses_a_runtime_smithery_recognises() -> None:
    # Smithery's bundle parser only accepts python/node/binary — never
    # the canonical bundle's "uv" type. This is the whole reason the
    # transform exists.
    assert _CANONICAL["server"]["type"] == "uv"  # guards the premise
    out = smithery_manifest(_CANONICAL)
    assert out["server"]["type"] == "python"


def test_transform_launches_via_uvx_mcpg() -> None:
    out = smithery_manifest(_CANONICAL)
    cfg = out["server"]["mcp_config"]
    assert cfg["command"] == "uvx"
    assert cfg["args"] == ["mcpg"]
    # The connection URL + access mode must still flow from user_config.
    assert cfg["env"]["MCPG_DATABASE_URL"] == "${user_config.database_url}"
    assert cfg["env"]["MCPG_ACCESS_MODE"] == "${user_config.access_mode}"


def test_transform_preserves_the_listing_metadata() -> None:
    out = smithery_manifest(_CANONICAL)
    # Version, config schema source, icon, and privacy policy must carry
    # across unchanged — Smithery builds the config schema from
    # user_config and the directory-style metadata from the rest.
    assert out["version"] == _CANONICAL["version"]
    assert out["user_config"] == _CANONICAL["user_config"]
    assert out["icon"] == _CANONICAL["icon"]
    assert out["privacy_policies"] == _CANONICAL["privacy_policies"]


def test_transform_does_not_mutate_the_canonical_manifest() -> None:
    before = json.dumps(_CANONICAL, sort_keys=True)
    smithery_manifest(_CANONICAL)
    assert json.dumps(_CANONICAL, sort_keys=True) == before


def test_build_assembles_a_complete_bundle_dir(tmp_path: Path) -> None:
    out = build(tmp_path / "smithery")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["server"]["type"] == "python"
    # Payload the packer needs must be present.
    assert (out / manifest["icon"]).is_file()
    assert (out / manifest["server"]["entry_point"]).is_file()
    assert (out / "pyproject.toml").is_file()
