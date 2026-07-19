"""Render a performance-run JSON into a self-contained HTML dashboard.

Reads the structured JSON a ``benchmarks.perf.runner`` run writes and produces
one **self-contained, theme-aware** HTML file — no external hosts (same rule as
an Artifact): all CSS inline, charts as inline SVG, no CDN scripts. Re-run the
harness, regenerate; the file is portable, reviewable, publishable.

    uv run python -m benchmarks.dashboard.generate \
        --input benchmarks/results/perf.json \
        --output benchmarks/results/perf.html

Design follows the project's data-viz method: categorical colours in fixed slot
order (native = blue, server-side = green, e2e = magenta/yellow/aqua), thin
marks with rounded data-ends, a legend for every multi-series chart, recessive
gridlines, and a table view alongside every chart so identity is never
colour-alone. The whole document swaps light/dark from the viewer's theme.

``render_html`` is pure (JSON in, HTML string out) and unit-tested; only the
thin CLI wrapper touches the filesystem.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

# Categorical slot order (validated reference palette). native=blue is the
# "irreducible DB work" colour; the same blue marks t_db in the waterfall.
_PATH_COLORS: dict[str, str] = {
    "native": "var(--series-1)",
    "server_side": "var(--series-2)",
    "e2e_inmemory": "var(--series-3)",
    "e2e_stdio": "var(--series-4)",
    "e2e_http": "var(--series-5)",
}
_PATH_LABELS: dict[str, str] = {
    "native": "Native (psycopg)",
    "server_side": "MCPg server-side",
    "e2e_inmemory": "MCPg e2e (in-memory)",
    "e2e_stdio": "MCPg e2e (stdio)",
    "e2e_http": "MCPg e2e (HTTP)",
}
# Waterfall segments, in execution (stack) order, coloured with the validated
# slot-1..5 sequence so adjacent bands stay CVD-distinct; the 2px surface gaps
# between segments are the secondary-encoding relief the palette asks for. The
# story is carried by segment *size* (t_db dominates) + the assertion badge, not
# by any one segment's hue. Each chart carries its own legend, so reusing the
# categorical ramp across panels is unambiguous.
_SEGMENTS: list[tuple[str, str, str]] = [
    ("t_parse", "parse + validate", "var(--series-1)"),
    ("t_pool", "pool checkout", "var(--series-2)"),
    ("t_txn", "txn (begin/rollback)", "var(--series-3)"),
    ("t_db", "DB execute + fetch", "var(--series-4)"),
    ("t_serialize", "serialize", "var(--series-5)"),
]


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "0"
    if value >= 100:
        return f"{value:,.0f} ms"
    if value >= 1:
        return f"{value:.2f} ms"
    return f"{value * 1000:.0f} µs"


def _fmt_rps(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}/s"


def _baseline_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warm, single-client baseline rows — the latency comparison the report leads with.

    A baseline row is warm, concurrency 1, and carries no throughput. The
    concurrency sweep also emits a concurrency==1 row (its level-1 point) with
    throughput set and no decomposition; the ``throughput_rps is None`` guard
    keeps it from shadowing the real single-client baseline for ultralight
    queries.
    """
    return [
        r
        for r in results
        if r.get("temperature") == "warm" and r.get("concurrency") == 1 and r.get("throughput_rps") is None
    ]


def _ordered_paths(rows: list[dict[str, Any]]) -> list[str]:
    seen = [r["path"] for r in rows]
    return [p for p in _PATH_COLORS if p in seen]


def _nice_max(value: float) -> float:
    """Round an axis maximum up to a clean bound (fine-grained mantissa steps)."""
    if value <= 0:
        return 1.0
    import math

    exp = math.floor(math.log10(value))
    base = 10.0**exp
    for mult in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= mult * base:
            return mult * base
    return 10 * base


# --- SVG chart builders (pure) --------------------------------------------


def _svg_open(width: int, height: int, label: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{_esc(label)}" preserveAspectRatio="xMidYMid meet" '
        f'style="max-width:100%;height:auto;font:12px system-ui,-apple-system,sans-serif">'
    )


def _legend(items: list[tuple[str, str]]) -> str:
    chips = "".join(
        f'<span class="chip"><span class="sw" style="background:{color}"></span>{_esc(label)}</span>'
        for label, color in items
    )
    return f'<div class="legend">{chips}</div>'


def _grouped_bar_chart(rows: list[dict[str, Any]], paths: list[str]) -> str:
    """Horizontal grouped bars: warm p50 latency (ms) per query, one bar per path."""
    queries = list(dict.fromkeys(r["query_id"] for r in rows))
    by_key = {(r["path"], r["query_id"]): r for r in rows}
    values = {
        (p, q): (by_key[(p, q)]["latency_ms"]["p50"] if (p, q) in by_key else None) for p in paths for q in queries
    }
    vmax = _nice_max(max((v for v in values.values() if v is not None), default=1.0))

    left, right, top = 150, 70, 8
    plot_w = 760 - left - right
    band = max(28.0, len(paths) * 12.0 + 12)
    bar_h = min(18.0, (band - 10) / max(1, len(paths)) - 2)
    height = int(top + band * len(queries) + 36)

    parts = [_svg_open(760, height, "Warm p50 latency by query and path")]
    # Gridlines + x ticks.
    for i in range(5):
        gx = left + plot_w * i / 4
        val = vmax * i / 4
        parts.append(
            f'<line x1="{gx:.1f}" y1="{top}" x2="{gx:.1f}" y2="{top + band * len(queries):.1f}" class="grid"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{top + band * len(queries) + 16:.1f}" text-anchor="middle" class="tick">'
            f"{_esc(_fmt_ms(val))}</text>"
        )
    for qi, q in enumerate(queries):
        gy = top + band * qi
        parts.append(
            f'<text x="{left - 10}" y="{gy + band / 2:.1f}" text-anchor="end" '
            f'dominant-baseline="middle" class="cat">{_esc(q)}</text>'
        )
        for pi, p in enumerate(paths):
            v = values[(p, q)]
            if v is None:
                continue
            w = plot_w * v / vmax
            y = gy + 6 + pi * (bar_h + 2)
            parts.append(
                f'<rect x="{left}" y="{y:.1f}" width="{max(0.5, w):.1f}" height="{bar_h:.1f}" rx="3" '
                f'fill="{_PATH_COLORS[p]}"><title>{_esc(_PATH_LABELS[p])} · {_esc(q)}: '
                f"{_esc(_fmt_ms(v))}</title></rect>"
            )
    parts.append("</svg>")
    legend = _legend([(_PATH_LABELS[p], _PATH_COLORS[p]) for p in paths])
    return legend + "".join(parts)


def _waterfall_chart(rows: list[dict[str, Any]]) -> str:
    """100%-normalized overhead decomposition per query (server-side path).

    Each bar is one query's server-side latency split by segment, as a share of
    its own total, with the absolute total labelled at the right. Normalizing
    keeps every query readable on one axis (a shared *absolute* axis buries the
    sub-ms queries under the heavy ones) and makes the honest story visible: the
    overhead bands are a large share of a tiny query but a negligible share of a
    heavy one — where ``t_db`` fills almost the whole bar.
    """
    server = [r for r in rows if r["path"] == "server_side" and r.get("decomposition_ns")]
    data: list[tuple[str, dict[str, float], float]] = []
    for r in server:
        dec = r["decomposition_ns"]
        seg = {key: (dec.get(key) or 0.0) / 1e6 for key, _, _ in _SEGMENTS}  # ns -> ms
        total = sum(seg.values())
        if total > 0:
            data.append((r["query_id"], seg, total))
    if not data:
        return ""

    left, right, top = 150, 96, 8
    plot_w = 760 - left - right
    band = 30.0
    bar_h = 18.0
    height = int(top + band * len(data) + 36)
    parts = [_svg_open(760, height, "Overhead decomposition (normalized)")]
    for i in range(5):
        gx = left + plot_w * i / 4
        parts.append(f'<line x1="{gx:.1f}" y1="{top}" x2="{gx:.1f}" y2="{top + band * len(data):.1f}" class="grid"/>')
        parts.append(
            f'<text x="{gx:.1f}" y="{top + band * len(data) + 16:.1f}" text-anchor="middle" class="tick">'
            f"{i * 25}%</text>"
        )
    for qi, (q, seg, total) in enumerate(data):
        gy = top + band * qi
        parts.append(
            f'<text x="{left - 10}" y="{gy + band / 2:.1f}" text-anchor="end" '
            f'dominant-baseline="middle" class="cat">{_esc(q)}</text>'
        )
        x = float(left)
        for key, label, color in _SEGMENTS:
            share = seg[key] / total
            if share <= 0:
                continue
            w = plot_w * share
            pct = share * 100
            # 2px surface gap between touching segments (the relief the palette asks for).
            parts.append(
                f'<rect x="{x:.1f}" y="{gy + 6:.1f}" width="{max(0.5, w - 2):.1f}" height="{bar_h:.1f}" '
                f'fill="{color}"><title>{_esc(q)} · {_esc(label)}: {_esc(_fmt_ms(seg[key]))} '
                f"({pct:.0f}%)</title></rect>"
            )
            x += w
        # Absolute total at the right so magnitude is never lost to normalization.
        parts.append(
            f'<text x="{left + plot_w + 8}" y="{gy + band / 2:.1f}" dominant-baseline="middle" '
            f'class="cat" style="fill:var(--ink-2)">{_esc(_fmt_ms(total))}</text>'
        )
    parts.append("</svg>")
    legend = _legend([(label, color) for _, label, color in _SEGMENTS])
    return legend + "".join(parts)


def _throughput_chart(results: list[dict[str, Any]]) -> str:
    """Lines: throughput (queries/sec) vs concurrency, per path. Empty if no sweep."""
    conc = [r for r in results if r.get("throughput_rps") is not None and r.get("concurrency", 1) >= 1]
    if not conc:
        return ""
    levels = sorted({r["concurrency"] for r in conc})
    paths = _ordered_paths(conc)
    # Average rps across queries at each (path, level) so the line reads as
    # aggregate throughput, not one query's.
    agg: dict[tuple[str, int], list[float]] = {}
    for r in conc:
        agg.setdefault((r["path"], r["concurrency"]), []).append(r["throughput_rps"])
    series = {(p, lv): (sum(vs) / len(vs) if (vs := agg.get((p, lv))) else None) for p in paths for lv in levels}
    vmax = _nice_max(max((v for v in series.values() if v is not None), default=1.0))

    left, right, top, bottom = 70, 30, 12, 34
    plot_w, plot_h = 760 - left - right, 240 - top - bottom
    parts = [_svg_open(760, 240, "Throughput vs concurrency")]
    for i in range(5):
        gy = top + plot_h * i / 4
        parts.append(f'<line x1="{left}" y1="{gy:.1f}" x2="{left + plot_w}" y2="{gy:.1f}" class="grid"/>')
        parts.append(
            f'<text x="{left - 8}" y="{gy + 4:.1f}" text-anchor="end" class="tick">'
            f"{_esc(_fmt_rps(vmax * (4 - i) / 4))}</text>"
        )

    def px(idx: int) -> float:
        return left + (plot_w * idx / (len(levels) - 1) if len(levels) > 1 else plot_w / 2)

    def py(val: float) -> float:
        return top + plot_h * (1 - val / vmax)

    for li, lv in enumerate(levels):
        parts.append(
            f'<text x="{px(li):.1f}" y="{top + plot_h + 20:.1f}" text-anchor="middle" class="tick">{lv}</text>'
        )
    parts.append(
        f'<text x="{left + plot_w / 2:.1f}" y="238" text-anchor="middle" class="axis-title">concurrent clients</text>'
    )
    for p in paths:
        marks: list[tuple[float, float, int, float]] = []
        for li, lv in enumerate(levels):
            val = series.get((p, lv))
            if val is None:
                continue
            marks.append((px(li), py(val), lv, val))
        if not marks:
            continue
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in marks)
        parts.append(
            f'<path d="{d}" fill="none" stroke="{_PATH_COLORS[p]}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for x, y, lv, val in marks:
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{_PATH_COLORS[p]}" stroke="var(--surface-1)" '
                f'stroke-width="2"><title>{_esc(_PATH_LABELS[p])} @ {lv}: {_esc(_fmt_rps(val))}</title></circle>'
            )
    parts.append("</svg>")
    legend = _legend([(_PATH_LABELS[p], _PATH_COLORS[p]) for p in paths])
    return legend + "".join(parts)


# --- tables + document ----------------------------------------------------


def _latency_table(rows: list[dict[str, Any]], paths: list[str]) -> str:
    queries = list(dict.fromkeys(r["query_id"] for r in rows))
    by_key = {(r["path"], r["query_id"]): r for r in rows}
    head = "".join(f"<th>{_esc(_PATH_LABELS[p])} p50 / p95</th>" for p in paths)
    body = []
    for q in queries:
        cells = [f'<th scope="row">{_esc(q)}</th>']
        for p in paths:
            r = by_key.get((p, q))
            if r is None:
                cells.append("<td>—</td>")
            else:
                lat = r["latency_ms"]
                cells.append(f"<td>{_esc(_fmt_ms(lat['p50']))} / {_esc(_fmt_ms(lat['p95']))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<table class="data"><thead><tr><th scope="col">query</th>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def _assertion_summary(assertions: list[dict[str, Any]]) -> tuple[str, str]:
    gate = [a for a in assertions if a.get("name") == "t_db_matches_native"]
    if not gate:
        return "—", "muted"
    passed = sum(1 for a in gate if a.get("passed"))
    verdict = "good" if passed == len(gate) else ("warning" if passed else "critical")
    return f"{passed} / {len(gate)}", verdict


def _assertion_table(assertions: list[dict[str, Any]]) -> str:
    gate = [a for a in assertions if a.get("name") == "t_db_matches_native"]
    if not gate:
        return ""
    body = []
    for a in gate:
        d = a.get("detail", {})
        ok = a.get("passed")
        icon = "✓" if ok else "✗"
        cls = "good" if ok else "critical"
        body.append(
            f'<tr><th scope="row">{_esc(a.get("query_id"))}</th>'
            f"<td>{_esc(_fmt_ms(d.get('native_t_db_ms')))}</td>"
            f"<td>{_esc(_fmt_ms(d.get('server_t_db_ms')))}</td>"
            f"<td>{_esc(_fmt_ms(d.get('delta_ms')))}</td>"
            f'<td><span class="badge {cls}">{icon} {"match" if ok else "differs"}</span></td></tr>'
        )
    return (
        '<table class="data"><thead><tr><th scope="col">query</th><th scope="col">native t_db</th>'
        '<th scope="col">server t_db</th><th scope="col">Δ</th><th scope="col">verdict</th></tr></thead>'
        "<tbody>" + "".join(body) + "</tbody></table>"
    )


def _stat_tiles(meta: dict[str, Any]) -> str:
    pg = meta.get("postgres", {}) or {}
    host = meta.get("host", {}) or {}
    tiles = [
        ("MCPg", meta.get("mcpg_version", "—")),
        ("PostgreSQL", (pg.get("version_string", "—") or "—").split(" ")[0] if pg else "—"),
        ("TPC-H scale", f"SF{meta.get('scale_factor', '—')}"),
        ("iterations", meta.get("iterations", "—")),
        ("host", host.get("machine", "—")),
        ("commit", (meta.get("git_sha", "—") or "—")[:10]),
    ]
    return (
        '<div class="tiles">'
        + "".join(
            f'<div class="tile"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div></div>' for k, v in tiles
        )
        + "</div>"
    )


# --- token efficiency (Tier-A) --------------------------------------------

# baseline (bare run_select) = blue; MCPg = green — "the win".
_TOK_BASELINE = "var(--series-1)"
_TOK_MCPG = "var(--series-2)"


def _fmt_tok(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def _break_even_chart(per_call: list[dict[str, Any]], upfront_full: int, upfront_bare: int) -> str:
    """Cumulative-tokens line chart: MCPg vs a bare run_select tool over tasks.

    MCPg starts high (its full tool surface) but rises slower (compact output);
    the bare baseline starts low but pays the raw-SQL cost every task. They cross
    at the break-even, after which MCPg is cheaper and the gap widens.
    """
    if not per_call:
        return ""
    mcpg_per = sum(float(c["mcpg_tokens"]) for c in per_call) / len(per_call)
    raw_per = sum(float(c["raw_tokens"]) for c in per_call) / len(per_call)
    if raw_per <= mcpg_per:
        return ""
    k = (upfront_full - upfront_bare) / (raw_per - mcpg_per)
    n_max = max(4.0, k * 2)

    def mcpg_at(n: float) -> float:
        return upfront_full + mcpg_per * n

    def base_at(n: float) -> float:
        return upfront_bare + raw_per * n

    vmax = _nice_max(max(mcpg_at(n_max), base_at(n_max)))
    left, right, top, bottom = 64, 120, 12, 34
    plot_w, plot_h = 760 - left - right, 260 - top - bottom
    parts = [_svg_open(760, 260, "Token break-even: MCPg vs a bare run_select tool")]
    for i in range(5):
        gy = top + plot_h * i / 4
        parts.append(f'<line x1="{left}" y1="{gy:.1f}" x2="{left + plot_w}" y2="{gy:.1f}" class="grid"/>')
        parts.append(
            f'<text x="{left - 8}" y="{gy + 4:.1f}" text-anchor="end" class="tick">'
            f"{_esc(_fmt_tok(vmax * (4 - i) / 4))}</text>"
        )

    def px(n: float) -> float:
        return left + plot_w * n / n_max

    def py(v: float) -> float:
        return top + plot_h * (1 - v / vmax)

    for i in range(5):
        n = n_max * i / 4
        parts.append(
            f'<text x="{px(n):.1f}" y="{top + plot_h + 20:.1f}" text-anchor="middle" class="tick">{n:.0f}</text>'
        )
    parts.append(
        f'<text x="{left + plot_w / 2:.1f}" y="258" text-anchor="middle" class="axis-title">'
        "database tasks in a session</text>"
    )
    # The two cumulative lines.
    for _label, color, fn in (("bare run_select", _TOK_BASELINE, base_at), ("MCPg", _TOK_MCPG, mcpg_at)):
        d = f"M{px(0):.1f},{py(fn(0)):.1f} L{px(n_max):.1f},{py(fn(n_max)):.1f}"
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round"/>')
    # Mark the crossing.
    cx, cy = px(k), py(mcpg_at(k))
    parts.append(f'<line x1="{cx:.1f}" y1="{top}" x2="{cx:.1f}" y2="{top + plot_h:.1f}" class="grid"/>')
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="var(--ink)"/>')
    parts.append(
        f'<text x="{cx + 6:.1f}" y="{top + 14:.1f}" class="cat" style="fill:var(--ink)">'
        f"break-even ~ {k:.0f} tasks</text>"
    )
    parts.append("</svg>")
    legend = _legend([("MCPg", _TOK_MCPG), ("bare run_select tool", _TOK_BASELINE)])
    return legend + "".join(parts)


def _token_table(comparisons: list[dict[str, Any]]) -> str:
    body = []
    for c in comparisons:
        tc = c["category"] == "tool-context"
        verdict = (
            f'<span class="badge critical">+{_esc(_fmt_tok(c["mcpg_tokens"] - c["raw_tokens"]))} upfront</span>'
            if tc
            else f'<span class="badge good">-{c["savings_pct"]:.0f}%</span>'
        )
        body.append(
            f'<tr><th scope="row">{_esc(c["name"])}</th>'
            f"<td>{_esc(_fmt_tok(c['mcpg_tokens']))}</td>"
            f"<td>{_esc(_fmt_tok(c['raw_tokens']))}</td>"
            f"<td>{verdict}</td></tr>"
        )
    return (
        '<table class="data"><thead><tr><th scope="col">comparison</th><th scope="col">MCPg</th>'
        '<th scope="col">raw SQL</th><th scope="col"></th></tr></thead><tbody>' + "".join(body) + "</tbody></table>"
    )


def _token_section(token_report: dict[str, Any]) -> str:
    comparisons = token_report.get("comparisons", []) or []
    if not comparisons:
        return ""
    meta = token_report.get("metadata", {}) or {}
    be = meta.get("break_even", {}) or {}
    per_call = [c for c in comparisons if c["category"] != "tool-context"]
    upfront = next((c for c in comparisons if c["category"] == "tool-context"), None)
    k = be.get("break_even_tasks")
    tiles = []
    for c in per_call:
        tiles.append((c["category"], f"-{c['savings_pct']:.0f}%"))
    if upfront:
        tiles.append(("tool surface upfront", f"+{_fmt_tok(upfront['mcpg_tokens'] - upfront['raw_tokens'])} tok"))
    if k is not None:
        tiles.append(("break-even", f"~{k} tasks"))
    tiles_html = (
        '<div class="tiles">'
        + "".join(
            f'<div class="tile"><div class="k">{_esc(kk)}</div><div class="v">{_esc(vv)}</div></div>'
            for kk, vv in tiles
        )
        + "</div>"
    )
    chart = _break_even_chart(per_call, upfront["mcpg_tokens"], upfront["raw_tokens"]) if upfront else ""
    return f"""<section><h2>Token efficiency <span class="tag">v2 · Tier A</span></h2>
      <p class="note">MCPg's compact, structured tool output vs the raw-SQL equivalent an agent would otherwise
      pull and interpret — counted with <code>{_esc(meta.get("encoding", "o200k_base"))}</code>. The rich tool
      surface costs more context up front (shown, not hidden); the per-call savings repay it after the break-even.</p>
      {tiles_html}
      {chart}
      {_token_table(comparisons)}</section>"""


def render_html(run: dict[str, Any], token_report: dict[str, Any] | None = None) -> str:
    """Render a PerfRun dict into a complete, self-contained HTML document.

    When ``token_report`` (a Tier-A tokens JSON) is given, a token-efficiency
    section is appended — the same dashboard, evolved for v2.
    """
    meta = run.get("metadata", {}) or {}
    results = run.get("results", []) or []
    assertions = run.get("assertions", []) or []
    rows = _baseline_rows(results)
    paths = _ordered_paths(rows)

    verdict_str, verdict_cls = _assertion_summary(assertions)
    waterfall = _waterfall_chart(rows)
    throughput = _throughput_chart(results)

    sections = [
        f"""<section class="hero">
      <div class="verdict {verdict_cls}">
        <div class="big">{_esc(verdict_str)}</div>
        <div class="cap">queries where <code>t_db</code> matches native</div>
      </div>
      <p class="lede">MCPg does not make queries faster — the same SQL runs on the same PostgreSQL.
      This dashboard shows the added latency is small and predictable, and <strong>decomposes exactly
      where it goes</strong>: the DB-execution segment (<code>t_db</code>) is identical to native;
      the fixed-cost overhead lives in parse + serialize.</p>
      {_stat_tiles(meta)}
    </section>""",
    ]
    if paths:
        sections.append(
            f"""<section><h2>Warm latency (p50) by query</h2>
      <p class="note">Lower is better. Native vs MCPg on identical SQL — the bars sit on top of each other
      because the database does identical work.</p>
      {_grouped_bar_chart(rows, paths)}
      {_latency_table(rows, paths)}</section>"""
        )
    if waterfall:
        sections.append(
            f"""<section><h2>Overhead decomposition</h2>
      <p class="note">Each bar is one query's server-side latency split by segment, as a share of its own total
      (absolute total at right). <code>t_db</code> is the actual database work and equals native; the other bands
      are MCPg's fixed-cost overhead — a visible share of a sub-millisecond query, a negligible sliver of a heavy
      one, where <code>t_db</code> fills almost the whole bar.</p>
      {waterfall}</section>"""
        )
    if throughput:
        sections.append(
            f"""<section><h2>Throughput under concurrency</h2>
      <p class="note">Aggregate queries/sec as concurrent clients rise. Pool + serialization costs surface here.</p>
      {throughput}</section>"""
        )
    atable = _assertion_table(assertions)
    if atable:
        sections.append(f"""<section><h2>The <code>t_db == native</code> gate</h2>{atable}</section>""")

    if token_report:
        token_section = _token_section(token_report)
        if token_section:
            sections.append(token_section)

    body = "\n".join(sections)
    return _DOCUMENT.format(
        title="MCPg performance benchmark",
        timestamp=_esc(meta.get("timestamp", "—")),
        body=body,
    )


_DOCUMENT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --surface-1:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --series-1:#2a78d6; --series-2:#008300; --series-3:#e87ba4; --series-4:#eda100;
  --series-5:#1baf7a; --series-6:#eb6834; --series-7:#4a3aa7; --series-8:#e34948;
  --good:#0ca30c; --warning:#fab219; --critical:#d03b3b;
  color-scheme:light;
}}
@media (prefers-color-scheme:dark) {{
  :root:where(:not([data-theme="light"])) {{
    --surface-1:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --series-1:#3987e5; --series-2:#008300; --series-3:#d55181; --series-4:#c98500;
    --series-5:#199e70; --series-6:#d95926; --series-7:#9085e9; --series-8:#e66767;
    color-scheme:dark;
  }}
}}
:root[data-theme="dark"] {{
  --surface-1:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --series-1:#3987e5; --series-2:#008300; --series-3:#d55181; --series-4:#c98500;
  --series-5:#199e70; --series-6:#d95926; --series-7:#9085e9; --series-8:#e66767;
  color-scheme:dark;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--plane); color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:900px; margin:0 auto; padding:32px 20px 64px; }}
header {{ display:flex; align-items:baseline; justify-content:space-between; gap:16px; flex-wrap:wrap; }}
h1 {{ font-size:22px; margin:0; }}
.sub {{ color:var(--muted); font-size:13px; }}
.toggle {{ margin-left:auto; background:var(--surface-1); color:var(--ink-2); border:1px solid var(--border);
  border-radius:8px; padding:6px 12px; cursor:pointer; font:inherit; font-size:13px; }}
section {{ background:var(--surface-1); border:1px solid var(--border); border-radius:14px;
  padding:20px 22px; margin-top:20px; overflow-x:auto; }}
h2 {{ font-size:16px; margin:0 0 4px; }}
.tag {{ font-size:11px; font-weight:600; color:var(--muted); border:1px solid var(--border);
  border-radius:20px; padding:1px 8px; margin-left:6px; vertical-align:middle; }}
.note, .lede {{ color:var(--ink-2); font-size:13.5px; margin:4px 0 14px; }}
.hero {{ display:grid; gap:16px; }}
.verdict {{ display:flex; align-items:center; gap:16px; }}
.verdict .big {{ font-size:40px; font-weight:700; line-height:1; }}
.verdict.good .big {{ color:var(--good); }} .verdict.warning .big {{ color:var(--warning); }}
.verdict.critical .big {{ color:var(--critical); }} .verdict.muted .big {{ color:var(--muted); }}
.verdict .cap {{ color:var(--ink-2); font-size:13px; }}
code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.92em;
  background:var(--plane); padding:1px 5px; border-radius:5px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; }}
.tile {{ background:var(--plane); border:1px solid var(--border); border-radius:10px; padding:10px 12px; }}
.tile .k {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }}
.tile .v {{ font-size:15px; font-weight:600; margin-top:2px; word-break:break-word; }}
.legend {{ display:flex; flex-wrap:wrap; gap:14px; margin:2px 0 12px; font-size:12.5px; color:var(--ink-2); }}
.chip {{ display:inline-flex; align-items:center; gap:6px; }}
.sw {{ width:11px; height:11px; border-radius:3px; display:inline-block; }}
svg text.tick {{ fill:var(--muted); }} svg text.cat {{ fill:var(--ink-2); font-size:11.5px; }}
svg text.axis-title {{ fill:var(--muted); }} svg line.grid {{ stroke:var(--grid); stroke-width:1; }}
table.data {{ border-collapse:collapse; width:100%; margin-top:14px; font-size:13px;
  font-variant-numeric:tabular-nums; }}
table.data th, table.data td {{ text-align:right; padding:6px 10px; border-bottom:1px solid var(--border); }}
table.data thead th {{ color:var(--muted); font-weight:600; font-size:11.5px; }}
table.data th[scope=row] {{ text-align:left; color:var(--ink-2); font-weight:600; }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:20px; font-size:12px; font-weight:600; }}
.badge.good {{ color:var(--good); background:color-mix(in srgb,var(--good) 14%,transparent); }}
.badge.critical {{ color:var(--critical); background:color-mix(in srgb,var(--critical) 14%,transparent); }}
footer {{ color:var(--muted); font-size:12px; margin-top:24px; text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div><h1>MCPg performance benchmark</h1><div class="sub">run {timestamp}</div></div>
  <button class="toggle" onclick="toggleTheme()">theme</button>
</header>
{body}
<footer>Generated from the run JSON by <code>benchmarks.dashboard.generate</code>
— self-contained, reproducible.</footer>
</div>
<script>
function toggleTheme() {{
  var r = document.documentElement;
  r.setAttribute('data-theme', r.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
}}
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a perf-run JSON into a self-contained HTML dashboard.")
    parser.add_argument("--input", type=Path, required=True, help="Perf-run JSON (from benchmarks.perf.runner).")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the HTML dashboard.")
    parser.add_argument(
        "--tokens", type=Path, default=None, help="Optional Tier-A tokens JSON (from benchmarks.tokens.tier_a.runner)."
    )
    args = parser.parse_args(argv)
    run = json.loads(args.input.read_text())
    token_report = json.loads(args.tokens.read_text()) if args.tokens else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(run, token_report))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
