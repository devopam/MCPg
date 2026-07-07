#!/usr/bin/env python3
"""Regenerate the reference tables that tend to drift in the docs.

Two catalogues in the docs rot every time a tool or module is added:

* the **tool index** in ``docs/tools.md`` — every MCP tool, grouped;
* the **module map** in ``docs/architecture.md`` — every ``mcpg.*`` module.

Both are derived here from the single sources of truth (the registered
tool surface and the package layout) so they can be regenerated instead
of hand-edited. ``tests/contract/test_doc_tables.py`` fails CI if either
doc drifts from what this script would produce, so a new tool or module
can't silently go undocumented.

Usage::

    python tools/generate_doc_tables.py            # print both tables
    python tools/generate_doc_tables.py --tools    # just the tool index
    python tools/generate_doc_tables.py --modules  # just the module map

The generated tables live between ``<!-- BEGIN … -->`` / ``<!-- END … -->``
markers in the docs; paste the regenerated block between the markers.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "tests" / "contract" / "tool_surface.snapshot.json"
TOOLS_PY = ROOT / "src" / "mcpg" / "tools.py"
PKG = ROOT / "src" / "mcpg"

# ---------------------------------------------------------------------------
# Tool index — parsed from the `_register_*` groups in tools.py so each tool
# lands in exactly the bucket the code registers it under.
# ---------------------------------------------------------------------------

# Ordered display rows: (heading, [register-group, …] or explicit names, gate).
# Groups whose siblings split read / write / ddl are merged into one row; the
# dedup pass below keeps every tool appearing exactly once, first-seen wins.
_TOOL_ROWS: list[tuple[str, list[str], str]] = [
    ("Server & self-description", ["_register_server_info"], "read"),
    ("Catalog — schemas / tables / columns", ["@catalog"], "read"),
    ("Catalog — compact", ["get_compact_schema"], "read"),
    ("Visualisation & structural diff", ["_register_diagrams", "_register_schema_diff"], "read"),
    ("Query & cursors", ["_register_query"], "read"),
    (
        "Health, tuning & advisors",
        ["@health_rest", "@advisors_rest", "_register_composite"],
        "read",
    ),
    ("Search", ["@search"], "read"),
    (
        "Test-data factory",
        ["generate_test_data", "generate_test_row_for", "seed_table_with_sample_data"],
        "read / **WRITE** (`seed_table_with_sample_data`)",
    ),
    (
        "Locks & blocking chains",
        ["list_locks", "find_blocking_chains", "walk_blocking_chains", "read_pg_stat_lock", "analyze_lock_hotspots"],
        "read",
    ),
    (
        "I/O, buffercache & WAL",
        [
            "read_pg_stat_io",
            "read_pg_buffercache_summary",
            "read_pg_buffercache_relations",
            "read_pg_wal_records",
            "read_pg_wal_stats",
            "get_wal_archive_status",
            "_register_aio_reads",
        ],
        "read",
    ),
    (
        "PITR & read-your-writes",
        ["check_pitr_readiness", "read_pg_stat_recovery", "_register_wait_for_lsn_reads", "wait_for_lsn"],
        "read / **WRITE** (`wait_for_lsn`)",
    ),
    (
        "Vector tuning & analytics (pgvector)",
        ["_register_vector_tuning", "recommend_vector_quantization", "_register_rag_efficiency", "import_vectors"],
        "read / **WRITE** (`import_vectors`)",
    ),
    ("RAG rerank analytics", ["_register_rag_analytics"], "read"),
    (
        "RAG telemetry",
        ["_register_rag_telemetry_efficiency_read", "_register_rag_telemetry_write", "_register_rag_telemetry_ddl"],
        "read / **WRITE** / **DDL**",
    ),
    (
        "pg_turboquant",
        ["_register_turboquant_reads", "_register_turboquant_writes", "_register_turboquant_ddl"],
        "read / **WRITE** / **DDL**",
    ),
    ("pg_search (ParadeDB BM25)", ["_register_pg_search_reads", "_register_pg_search_ddl"], "read / **DDL**"),
    (
        "Apache AGE graph + Cypher",
        ["_register_graphs_reads", "_register_graphs_writes", "generate_graph_projection"],
        "read / **DDL** (`create_graph`, `drop_graph`)",
    ),
    ("SQL/PGQ property graphs", ["_register_pgq_reads", "_register_pgq_ddl"], "read / **DDL**"),
    ("Redis FDW cache", ["_register_redis_fdw_reads", "_register_redis_fdw_ddl"], "read / **DDL**"),
    ("Live ops", ["_register_liveops"], "read"),
    (
        "Audit trail",
        ["_register_audit_trail", "prune_audit_events"],
        "read / **WRITE** (`prune_audit_events`)",
    ),
    (
        "Data movement",
        ["_register_data_movement", "_register_data_movement_writes", "_register_data_movement_shell"],
        "read / **WRITE** / **SHELL**",
    ),
    ("LISTEN/NOTIFY bridge", ["_register_listen"], "**LISTEN**"),
    ("Staged migrations", ["_register_migrations"], "**DDL** (`list_pending_migrations` is read)"),
    ("Migration history", ["read_migration_history", "list_unapplied_migration_scripts"], "read"),
    ("ORM-DSL exporters", ["_register_prisma"], "read"),
    (
        "TimescaleDB hypertables",
        ["_register_timescaledb_reads", "_register_timescaledb_writes"],
        "read / **DDL** writes",
    ),
    ("pg_partman", ["_register_partman"], "**DDL**"),
    ("pg_prewarm", ["_register_pg_prewarm_reads", "_register_pg_prewarm_writes"], "read / **WRITE**"),
    ("pg_repack", ["_register_repack_reads", "_register_repack_writes"], "read / **WRITE** (`repack_table`)"),
    ("pg_cron scheduling", ["_register_cron_write"], "**WRITE**"),
    (
        "PG 19 — data checksums & logical replication toggles",
        ["_register_pg19_runtime_reads", "_register_pg19_runtime_writes"],
        "read / **WRITE**",
    ),
    (
        "PG 19 — DDL introspection & constraints",
        ["_register_pg19_ddl_reads", "_register_pg19_ddl_writes"],
        "read / **WRITE** (`validate_check_constraint`)",
    ),
    ("PG 19 — skip scan", ["_register_pg19_skip_scan_reads"], "read"),
    (
        "PG 19 — partition merge / split",
        ["_register_pg19_partitions_reads", "_register_pg19_partitions_writes"],
        "read / **DDL** writes",
    ),
    ("PG 19 — stats status", ["get_pg19_stats_status"], "read"),
    ("WarehousePG (MPP)", ["_register_warehousepg_reads"], "read"),
    ("Logical replication pub/sub", ["_register_logical_replication_writes"], "**DDL**"),
    (
        "Write & DDL core",
        ["_register_write", "_register_maintenance", "_register_backend_control", "_register_ddl"],
        "**WRITE** / **DDL**",
    ),
]

# Names carved out of a shared register-group into their own row above.
_SEARCH = [
    "fuzzy_search",
    "full_text_search",
    "vector_search",
    "vector_range_search",
    "mmr_search",
    "hybrid_search",
    "geo_search",
]
_INTRO_SPECIAL = {
    "list_locks",
    "find_blocking_chains",
    "walk_blocking_chains",
    "read_pg_stat_io",
    "read_pg_buffercache_summary",
    "read_pg_buffercache_relations",
    "read_pg_wal_records",
    "read_pg_wal_stats",
    "get_wal_archive_status",
    "check_pitr_readiness",
    "read_migration_history",
    "get_compact_schema",
}
_ADVISORS_SPECIAL = ["generate_graph_projection", "generate_test_data", "generate_test_row_for"]


def _register_groups() -> dict[str, list[str]]:
    src = TOOLS_PY.read_text()
    valid = _snapshot_names()
    funcs = [(m.group(1), m.start()) for m in re.finditer(r"^def (_register_\w+)\(", src, re.M)]
    funcs.append(("__END__", len(src)))
    groups: dict[str, list[str]] = {}
    for (fname, start), (_, end) in pairwise(funcs):
        names = [n for n in re.findall(r'name="([a-z_][a-z0-9_]+)"', src[start:end]) if n in valid]
        if names:
            groups[fname] = names
    return groups


def _snapshot_names() -> set[str]:
    data = json.loads(SNAPSHOT.read_text())
    return {t["name"] for t in data["tools"]}


def tool_index_rows() -> list[tuple[str, list[str], str]]:
    """Resolve the display rows to concrete tool-name lists (dedup, first wins)."""
    g = _register_groups()
    catalog = [n for n in g["_register_introspection"] if n not in _INTRO_SPECIAL]
    health_rest = [n for n in g["_register_health"] if n not in _SEARCH and n != "recommend_vector_quantization"]
    advisors_rest = [n for n in g["_register_advisors"] if n not in _ADVISORS_SPECIAL]
    aliases = {
        "@catalog": catalog,
        "@search": _SEARCH,
        "@health_rest": health_rest,
        "@advisors_rest": advisors_rest,
    }

    seen: set[str] = set()
    out: list[tuple[str, list[str], str]] = []
    for heading, tokens, gate in _TOOL_ROWS:
        names: list[str] = []
        for tok in tokens:
            if tok in aliases:
                names += aliases[tok]
            elif tok in g:
                names += g[tok]
            else:  # a bare tool name
                names.append(tok)
        keep = [n for n in names if not (n in seen or seen.add(n))]
        out.append((heading, keep, gate))
    return out


def render_tool_index() -> str:
    rows = tool_index_rows()
    total = len(_snapshot_names())
    lines = [
        f"## Tool index ({total} tools)",
        "",
        "Grouped by feature area. The **Gate** column shows the capability",
        "each tool needs — plain `read` tools are available in every access",
        "mode; **WRITE** needs `restricted`+; **DDL** / **SHELL** / **LISTEN**",
        "need `unrestricted` **plus** the matching `MCPG_ALLOW_*` opt-in (see",
        "[capability gates](#capability-gates-at-a-glance) above).",
        "",
        "| Category | Gate | Tools |",
        "|---|---|---|",
    ]
    for heading, names, gate in rows:
        if not names:
            continue
        lines.append(f"| **{heading}** | {gate} | " + ", ".join(f"`{n}`" for n in names) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module map — every leaf module under src/mcpg, described by its docstring.
# ---------------------------------------------------------------------------

# Modules whose docstring is empty or unhelpful get a curated one-liner here.
_MODULE_FALLBACK = {
    "mcpg.config": "Env-driven, validated `Settings` (frozen dataclass); redacts secrets in `__repr__`.",
    "mcpg.context": "`AppContext` — per-server state (settings, DB, cursor/listen managers) shared with tool wrappers.",
    "mcpg.schema_diff": "Structural schema diff powering `compare_schemas`.",
    "mcpg._vendor": "Vendored MIT-licensed `SafeSqlDriver` + connection-pool kernel (SQL parse / allowlist / bind).",
}


def _first_sentence(doc: str) -> str:
    text = " ".join(doc.split())
    text = text.replace("``", "`")  # RST double-backticks → Markdown code spans
    if not text:
        return ""
    # Prefer the first sentence; fall back to the whole collapsed line.
    m = re.match(r"(.+?\.)(?:\s|$)", text)
    return (m.group(1) if m else text).strip()


def module_descriptions() -> dict[str, str]:
    """Map every documented leaf module to a one-line responsibility."""
    out: dict[str, str] = {}
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).with_suffix("")
        parts = rel.parts
        if parts[-1] in ("__init__", "__main__"):
            continue
        # Collapse the vendored SQL kernel into one aggregate row.
        if parts[0] == "_vendor":
            out.setdefault("mcpg._vendor", _MODULE_FALLBACK["mcpg._vendor"])
            continue
        name = "mcpg." + ".".join(parts)
        try:
            doc = ast.get_docstring(ast.parse(path.read_text())) or ""
        except SyntaxError:
            doc = ""
        desc = _first_sentence(doc) or _MODULE_FALLBACK.get(name, "")
        out[name] = _MODULE_FALLBACK.get(name, desc) if not desc else desc
    # Ensure the aggregate vendor row exists even if walk order missed it.
    out.setdefault("mcpg._vendor", _MODULE_FALLBACK["mcpg._vendor"])
    return out


def documented_module_names() -> set[str]:
    return set(module_descriptions())


def render_module_map() -> str:
    mods = module_descriptions()
    lines = [
        f"## Module map ({len(mods)} modules)",
        "",
        "Every `mcpg.*` module and what it owns, alphabetical. The layered",
        "request path through these lives in the [Overview](#overview) diagram;",
        "this table is the exhaustive index. Regenerate with",
        "`python tools/generate_doc_tables.py --modules`.",
        "",
        "| Module | Responsibility |",
        "|---|---|",
    ]
    for name in sorted(mods):
        lines.append(f"| `{name}` | {mods[name]} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tools", action="store_true", help="print only the tool index")
    ap.add_argument("--modules", action="store_true", help="print only the module map")
    args = ap.parse_args()
    show_tools = args.tools or not args.modules
    show_modules = args.modules or not args.tools
    if show_tools:
        print(render_tool_index())
    if show_tools and show_modules:
        print()
    if show_modules:
        print(render_module_map())


if __name__ == "__main__":
    main()
