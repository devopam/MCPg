"""Roadmap-row linkage helper (roadmap 14.6).

Parses ``docs/feature-shortlist.md`` into a map of row id → status so a
PR's cited roadmap row can be validated at review/merge time. Before
this, the PR↔roadmap linkage was implicit prose ("advances 8.6"); now
the contract-review step can mechanically confirm the cited row exists
and report whether it's already marked shipped.

Row ids are the leading cell of each table row: ``N.M`` (e.g. ``8.6``,
``15.2``) or a bare section number used as a row id (rare). Status is
inferred from the ✅ / 🟡 / (none) marker at the start of the Item cell:

* ``shipped``     — the Item cell starts with ✅
* ``in_progress`` — the Item cell starts with 🟡
* ``open``        — neither marker

CLI:

    python tools/roadmap_linkage.py list           # print every id + status
    python tools/roadmap_linkage.py check 8.6       # exit 0 if the row exists
    python tools/roadmap_linkage.py check 8.6 --open  # exit 0 only if NOT shipped

The ``check`` subcommand is what a CI lane or the PR-review SOP calls to
confirm a PR cites a real, still-open row.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_ROADMAP = Path(__file__).resolve().parent.parent / "docs" / "feature-shortlist.md"

# A table row: "| 8.6 | <item> | ... |". Capture the id cell and the
# item cell (the second column) for status inference.
_ROW = re.compile(r"^\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*(.*?)\s*\|")

SHIPPED = "shipped"
IN_PROGRESS = "in_progress"
OPEN = "open"


@dataclass(frozen=True)
class RoadmapRow:
    """One parsed roadmap table row."""

    row_id: str
    status: str  # shipped / in_progress / open
    summary: str  # first ~80 chars of the item cell, marker stripped


def _status_of(item_cell: str) -> str:
    head = item_cell.lstrip()
    if head.startswith("✅"):
        return SHIPPED
    if head.startswith("🟡"):
        return IN_PROGRESS
    return OPEN


def parse_roadmap(text: str | None = None) -> dict[str, RoadmapRow]:
    """Parse the roadmap markdown into ``{row_id: RoadmapRow}``.

    Pass ``text`` to parse a string directly (tests); otherwise reads
    ``docs/feature-shortlist.md``. Later rows with the same id win — in
    practice ids are unique, but a defensive last-wins keeps the map
    total.
    """
    if text is None:
        text = _ROADMAP.read_text(encoding="utf-8")
    rows: dict[str, RoadmapRow] = {}
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        row_id, item = m.group(1), m.group(2)
        # Skip the header separator row "| # | Item | ..." — its id cell
        # is "#", which won't match the numeric pattern, so nothing to do.
        # Strip the leading marker for a clean summary.
        summary = item
        for marker in ("✅", "🟡"):
            summary = summary.replace(marker, "", 1)
        summary = summary.strip().strip("*").strip()
        rows[row_id] = RoadmapRow(row_id=row_id, status=_status_of(item), summary=summary[:80])
    return rows


def row_exists(row_id: str, rows: dict[str, RoadmapRow] | None = None) -> bool:
    rows = rows if rows is not None else parse_roadmap()
    return row_id in rows


def row_status(row_id: str, rows: dict[str, RoadmapRow] | None = None) -> str | None:
    rows = rows if rows is not None else parse_roadmap()
    r = rows.get(row_id)
    return r.status if r else None


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Validate PR ↔ roadmap-row linkage.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="print every roadmap row id + status")
    c = sub.add_parser("check", help="check a row id exists")
    c.add_argument("row_id")
    c.add_argument(
        "--open",
        action="store_true",
        help="require the row to NOT be shipped (i.e. this PR is the one shipping it)",
    )
    args = p.parse_args(argv)
    rows = parse_roadmap()

    if args.cmd == "list":
        for rid in sorted(rows, key=lambda s: [int(x) for x in s.split(".")]):
            r = rows[rid]
            print(f"{rid:8} {r.status:12} {r.summary}")
        return 0

    # check
    if not row_exists(args.row_id, rows):
        print(f"error: roadmap row {args.row_id!r} not found in docs/feature-shortlist.md", file=sys.stderr)
        return 1
    status = row_status(args.row_id, rows)
    print(f"{args.row_id}: {status}")
    if args.open and status == SHIPPED:
        print(f"error: roadmap row {args.row_id!r} is already marked shipped", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
