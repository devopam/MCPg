"""Packaging-correctness checks: py.typed presence and wheel include rules."""

from __future__ import annotations

from pathlib import Path


def test_py_typed_marker_present() -> None:
    """PEP 561: the py.typed marker must exist in the package directory."""
    marker = Path(__file__).resolve().parents[2] / "src" / "mcpg" / "py.typed"
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8") == ""
