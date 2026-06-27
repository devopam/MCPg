"""Tests for the roadmap-linkage helper (roadmap 14.6)."""

from __future__ import annotations

import sys
from pathlib import Path

# tools/ isn't a package; add it to the path for the import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from roadmap_linkage import (
    IN_PROGRESS,
    OPEN,
    SHIPPED,
    parse_roadmap,
    row_exists,
    row_status,
)

_SAMPLE = """\
## 8. AI / agent-specific

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 8.6 | 🟡 **In progress.** outputSchema sweep. | L | Medium-High | notes |
| 8.7 | ✅ **Shipped (bundle).** Session-scope cost advisor. | M | High | notes |
| 8.8 | An open item with no marker yet. | M | High | notes |

## 15. WarehousePG

| 15.2 | ✅ **Shipped (Bundle A).** list_distribution_policies. | M | High | n |
"""


def test_parse_extracts_all_numeric_rows() -> None:
    rows = parse_roadmap(_SAMPLE)
    assert set(rows) == {"8.6", "8.7", "8.8", "15.2"}


def test_status_inference_from_marker() -> None:
    rows = parse_roadmap(_SAMPLE)
    assert rows["8.6"].status == IN_PROGRESS
    assert rows["8.7"].status == SHIPPED
    assert rows["8.8"].status == OPEN
    assert rows["15.2"].status == SHIPPED


def test_header_separator_rows_are_ignored() -> None:
    # "| # | Item | ..." and "|---|---|" must not become rows.
    rows = parse_roadmap(_SAMPLE)
    assert "#" not in rows
    assert all(rid[0].isdigit() for rid in rows)


def test_summary_strips_marker() -> None:
    rows = parse_roadmap(_SAMPLE)
    assert "🟡" not in rows["8.6"].summary
    assert "✅" not in rows["8.7"].summary


def test_row_exists_and_status_helpers() -> None:
    rows = parse_roadmap(_SAMPLE)
    assert row_exists("8.6", rows) is True
    assert row_exists("9.9", rows) is False
    assert row_status("8.7", rows) == SHIPPED
    assert row_status("nope", rows) is None


def test_parses_the_real_roadmap_without_error() -> None:
    """Smoke test against the live doc — every supported row id parses
    and at least the well-known shipped rows are present."""
    rows = parse_roadmap()
    assert "8.6" in rows
    assert "2.1" in rows
    # A few rows we know shipped earlier in the project.
    assert rows["2.1"].status == SHIPPED
