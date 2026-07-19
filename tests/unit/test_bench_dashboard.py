"""Unit tests for the JSON -> HTML dashboard generator (roadmap 19.2).

Covers the pure rendering path: helpers, and that ``render_html`` produces a
complete, **self-contained** document (no external hosts) with the expected
sections. No filesystem, no browser.
"""

from __future__ import annotations

from typing import Any

from benchmarks.dashboard import generate as dash


def _lat(p50: float) -> dict[str, Any]:
    return {
        "p50": p50,
        "p95": p50 * 1.5,
        "p99": p50 * 2,
        "mean": p50,
        "stdev": 0.0,
        "min": p50,
        "max": p50,
        "median_ci95": [p50, p50],
    }


def _run() -> dict[str, Any]:
    results = [
        {
            "path": "native",
            "query_id": "q1",
            "compute_class": "heavy",
            "result_size": "~100",
            "temperature": "warm",
            "concurrency": 1,
            "n": 50,
            "latency_ms": _lat(100.0),
            "throughput_rps": None,
            "samples_ns": [],
            "decomposition_ns": None,
        },
        {
            "path": "server_side",
            "query_id": "q1",
            "compute_class": "heavy",
            "result_size": "~100",
            "temperature": "warm",
            "concurrency": 1,
            "n": 50,
            "latency_ms": _lat(104.0),
            "throughput_rps": None,
            "samples_ns": [],
            "decomposition_ns": {
                "t_parse": 40000,
                "t_pool": 9000,
                "t_txn": 22000,
                "t_db": 100_000_000,
                "t_serialize": 15000,
                "t_protocol": None,
                "t_cache": None,
            },
        },
        # A concurrency-sweep row — must not pollute the single-client baseline.
        {
            "path": "server_side",
            "query_id": "q1",
            "compute_class": "heavy",
            "result_size": "~100",
            "temperature": "warm",
            "concurrency": 64,
            "n": 640,
            "latency_ms": _lat(900.0),
            "throughput_rps": 5000.0,
            "samples_ns": [],
            "decomposition_ns": None,
        },
        {
            "path": "native",
            "query_id": "q1",
            "compute_class": "heavy",
            "result_size": "~100",
            "temperature": "warm",
            "concurrency": 64,
            "n": 640,
            "latency_ms": _lat(880.0),
            "throughput_rps": 5300.0,
            "samples_ns": [],
            "decomposition_ns": None,
        },
    ]
    assertions = [
        {
            "name": "t_db_matches_native",
            "query_id": "q1",
            "passed": True,
            "detail": {"native_t_db_ms": 100.0, "server_t_db_ms": 100.4, "delta_ms": 0.4},
        }
    ]
    return {
        "metadata": {
            "timestamp": "2026-07-18T00:00:00Z",
            "mcpg_version": "0.6.11",
            "scale_factor": 10,
            "iterations": 50,
        },
        "results": results,
        "assertions": assertions,
        "schema_version": 1,
        "kind": "performance",
    }


def test_fmt_ms_units() -> None:
    assert dash._fmt_ms(None) == "—"
    assert dash._fmt_ms(0) == "0"
    assert dash._fmt_ms(0.06) == "60 µs"
    assert dash._fmt_ms(1.5) == "1.50 ms"
    assert dash._fmt_ms(420) == "420 ms"


def test_nice_max_fine_grained() -> None:
    assert dash._nice_max(546) == 600  # not rounded all the way up to 1000
    assert dash._nice_max(0) == 1.0
    assert dash._nice_max(95) == 100


def test_baseline_rows_excludes_concurrency() -> None:
    rows = dash._baseline_rows(_run()["results"])
    assert all(r["concurrency"] == 1 and r["temperature"] == "warm" for r in rows)
    assert len(rows) == 2  # native + server_side single-client only


def test_baseline_rows_excludes_concurrency_level_1_row() -> None:
    # The concurrency sweep emits a concurrency==1 row (its level-1 point) with
    # throughput set and no decomposition. It shares (path, query_id) with the
    # real single-client baseline and must NOT shadow it.
    run = _run()
    run["results"].append(
        {
            "path": "server_side",
            "query_id": "q1",
            "compute_class": "heavy",
            "result_size": "~100",
            "temperature": "warm",
            "concurrency": 1,
            "n": 20,
            "latency_ms": _lat(999.0),  # a *different*, misleading latency
            "throughput_rps": 42.0,  # <-- marks it as a sweep row
            "samples_ns": [],
            "decomposition_ns": None,
        }
    )
    rows = dash._baseline_rows(run["results"])
    # Still exactly the two real baselines; the sweep level-1 row is excluded.
    assert len(rows) == 2
    assert all(r.get("throughput_rps") is None for r in rows)
    server = next(r for r in rows if r["path"] == "server_side")
    assert server["latency_ms"]["p50"] == 104.0  # the real baseline, not 999.0


def test_render_html_is_complete_and_self_contained() -> None:
    out = dash.render_html(_run())
    # A full document.
    assert out.startswith("<!doctype html>")
    assert "<title>MCPg performance benchmark</title>" in out
    # Self-contained: no external hosts, no CDN scripts, no remote assets.
    assert "http://" not in out
    assert "https://" not in out
    assert "<script src" not in out
    assert "cdn" not in out.lower()
    # Theme-aware: both scopes present.
    assert "prefers-color-scheme:dark" in out
    assert '[data-theme="dark"]' in out


def test_render_html_has_sections_and_verdict() -> None:
    out = dash.render_html(_run())
    assert "1 / 1" in out  # verdict: 1 of 1 t_db gates passed
    assert "Warm latency" in out
    assert "Overhead decomposition" in out
    assert "Throughput under concurrency" in out  # concurrency rows present
    assert "Native (psycopg)" in out and "MCPg server-side" in out
    assert "<svg" in out
    assert "match" in out  # gate badge


def _token_report() -> dict[str, Any]:
    comps = [
        {
            "name": "compact_schema",
            "category": "schema",
            "mcpg_tokens": 574,
            "raw_tokens": 2375,
            "savings_pct": 75.8,
            "ratio": 4.1,
            "detail": {},
        },
        {
            "name": "analyze_plan",
            "category": "query-plan",
            "mcpg_tokens": 146,
            "raw_tokens": 3847,
            "savings_pct": 96.2,
            "ratio": 26.3,
            "detail": {},
        },
        {
            "name": "tool surface: full (252 tools) vs bare",
            "category": "tool-context",
            "mcpg_tokens": 63878,
            "raw_tokens": 193,
            "savings_pct": -33000.0,
            "ratio": 0.003,
            "detail": {"surface": "full (unrestricted)", "tools": 252},
        },
        {
            "name": "tool surface: intent=lookup (53 tools) vs bare",
            "category": "tool-context",
            "mcpg_tokens": 11281,
            "raw_tokens": 193,
            "savings_pct": -5000.0,
            "ratio": 0.017,
            "detail": {"surface": "intent=lookup", "tools": 53},
        },
    ]
    return {
        "metadata": {
            "encoding": "o200k_base",
            "break_even": {
                "mean_per_call_saving_tokens": 2751.0,
                "surfaces": [
                    {
                        "name": "full (unrestricted)",
                        "tool_count": 252,
                        "mcpg_tokens": 63878,
                        "upfront_extra_tokens": 63685,
                        "break_even_tasks": 24,
                    },
                    {
                        "name": "intent=lookup",
                        "tool_count": 53,
                        "mcpg_tokens": 11281,
                        "upfront_extra_tokens": 11088,
                        "break_even_tasks": 5,
                    },
                ],
                "upfront_extra_tokens": 63685,
                "break_even_tasks": 24,
            },
        },
        "comparisons": comps,
        "schema_version": 1,
        "kind": "tokens_tier_a",
    }


def test_render_html_appends_token_section() -> None:
    out = dash.render_html(_run(), token_report=_token_report())
    assert "Token efficiency" in out
    assert "break-even" in out
    assert "o200k_base" in out
    assert "-76%" in out or "-96%" in out  # per-call savings badge
    # The moves-left surface story: both surfaces + the range appear.
    assert "5" in out and "24" in out  # break-even range 5-24
    assert "intent=lookup" in out
    # Still a complete, self-contained document.
    assert out.startswith("<!doctype html>")
    assert "http://" not in out and "https://" not in out


def test_render_html_no_token_section_without_report() -> None:
    out = dash.render_html(_run())  # token_report defaults to None
    assert "Token efficiency" not in out


def test_render_html_omits_absent_sections() -> None:
    run = _run()
    # Strip decomposition + concurrency -> those sections should not render.
    for r in run["results"]:
        r["decomposition_ns"] = None
        r["throughput_rps"] = None
    out = dash.render_html(run)
    assert "Overhead decomposition" not in out
    assert "Throughput under concurrency" not in out
    assert "Warm latency" in out  # latency still there
