"""Tool outputSchema contract test — guards the structured-output surface.

Closes a gap that ``test_tool_return_shapes.py`` doesn't cover: the
return-shape snapshot pins the dataclass field names of every tool's
underlying helper, but it doesn't assert that the tool's MCP
``outputSchema`` is actually populated on the wire. FastMCP auto-derives
``outputSchema`` from the function's return type annotation only — a
handler annotated ``-> dict[str, Any]`` produces ``outputSchema = None``
and the client (LangChain, LangGraph, etc.) can't validate the response.

This test:

1. Boots a maximal-flag FastMCP server (same fixture as the surface
   snapshot test).
2. Walks every registered tool.
3. For tools listed in ``_TOOLS_WITH_STRUCTURED_OUTPUT``, asserts the
   tool's ``output_schema`` is a non-empty JSON Schema and its
   declared properties include every expected field.

As we sweep more tools from ``dict[str, Any]`` to typed dataclass
returns, add their names + expected fields to the manifest below. The
manifest is the explicit "what's structured-output today" list — a PR
that touches a converted tool's return shape will trip this test
before it merges.

The companion contract test ``test_tool_return_shapes.py`` still
pins the dataclass field set itself, so a rename / removal of a
field on the helper is caught by *both* tests in concert.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcpg.config import load_settings
from mcpg.tools import register_tools

# Mirrors the loopback fixture URL the sibling surface-snapshot test
# uses — never actually connected; ``register_tools`` only reads
# ``Settings`` fields.
_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"

# Map tool name → expected property keys in the auto-derived JSON Schema.
# Field sets here mirror the dataclass field set in the helper module —
# kept in sync explicitly (rather than re-derived) so the test fails loud
# on intentional shape changes, prompting the PR author to update both
# this manifest and the snapshot.
_TOOLS_WITH_STRUCTURED_OUTPUT: dict[str, frozenset[str]] = {
    # PG 19 DDL helpers (Phase 3 PR-9 — the first sweep).
    "get_pg19_ddl_status": frozenset(
        {
            "available",
            "server_version_num",
            "server_version",
            "has_pg_get_roledef",
            "has_pg_get_databasedef",
            "has_pg_get_tablespacedef",
            "detail",
        }
    ),
    "get_role_ddl": frozenset({"object_type", "object_name", "found", "ddl"}),
    "get_database_ddl": frozenset({"object_type", "object_name", "found", "ddl"}),
    "get_tablespace_ddl": frozenset({"object_type", "object_name", "found", "ddl"}),
    "validate_check_constraint": frozenset(
        {
            "table_schema",
            "table",
            "constraint_name",
            "was_valid",
            "now_valid",
            "changed",
            "validate_sql",
        }
    ),
    # PG 19 SQL/PGQ helpers (Phase 3 PR-13 sweep).
    "get_pgq_status": frozenset({"available", "server_version_num", "server_version", "detail"}),
    # List-returning handlers: FastMCP wraps `list[Dataclass]` returns into a
    # `{"result": [...]}` envelope at the top level. The per-item dataclass
    # fields live under `$defs` — the field-level snapshot test in
    # `test_tool_return_shapes.py` pins those. We only assert the envelope key
    # here so the schema-population gate still trips on a dict-typed regression.
    "list_property_graphs": frozenset({"result"}),
    "describe_property_graph": frozenset({"schema", "name", "vertex_tables", "edge_tables"}),
    "run_pgq": frozenset({"columns", "rows", "row_count", "truncated"}),
    "create_property_graph": frozenset({"schema", "name", "created"}),
    "drop_property_graph": frozenset({"schema", "name", "dropped"}),
    # PG 19 runtime toggles.
    "get_data_checksums_status": frozenset({"available", "server_version_num", "server_version", "enabled", "detail"}),
    "get_logical_replication_status": frozenset(
        {
            "available",
            "server_version_num",
            "server_version",
            "wal_level",
            "effective_wal_level",
            "max_replication_slots",
            "detail",
        }
    ),
    "enable_data_checksums": frozenset({"was_enabled", "now_enabled", "changed", "toggle_sql"}),
    "disable_data_checksums": frozenset({"was_enabled", "now_enabled", "changed", "toggle_sql"}),
    "enable_logical_replication_on_demand": frozenset(
        {"previous_wal_level", "new_wal_level", "requires_restart", "detail"}
    ),
    # PG 19 skip-scan.
    "get_skip_scan_status": frozenset({"available", "server_version_num", "server_version", "detail"}),
    "recommend_skip_scan_indexes": frozenset({"result"}),
    # PG 19 partitions.
    "get_pg19_partitions_status": frozenset({"available", "server_version_num", "server_version", "detail"}),
    "merge_partitions": frozenset(
        {
            "parent_schema",
            "parent_table",
            "source_partitions",
            "target_partition",
            "merge_sql",
        }
    ),
    "split_partition": frozenset(
        {
            "parent_schema",
            "parent_table",
            "source_partition",
            "new_partitions",
            "split_sql",
        }
    ),
    # WAIT FOR LSN.
    "get_wait_for_lsn_status": frozenset(
        {"available", "server_version_num", "server_version", "is_in_recovery", "detail"}
    ),
    "get_current_wal_lsn": frozenset({"role", "lsn"}),
    "recommend_read_your_writes": frozenset(
        {
            "recommend_use",
            "reason",
            "is_in_recovery",
            "server_version_num",
            "current_lag_bytes",
            "detail",
        }
    ),
    "wait_for_lsn": frozenset({"lsn", "timeout_ms", "timed_out", "wait_sql"}),
    # PG 19 stats.
    "get_pg19_stats_status": frozenset(
        {
            "available",
            "server_version_num",
            "server_version",
            "has_pg_stat_lock",
            "has_pg_stat_recovery",
            "detail",
        }
    ),
    "read_pg_stat_lock": frozenset({"result"}),
    "read_pg_stat_recovery": frozenset({"result"}),
    "analyze_lock_hotspots": frozenset({"available", "server_version_num", "detail", "hotspots"}),
    # PG 19 async I/O.
    "get_aio_status": frozenset(
        {
            "available",
            "server_version_num",
            "server_version",
            "io_method",
            "io_min_workers",
            "io_max_workers",
            "detail",
        }
    ),
    "recommend_io_method": frozenset({"available", "server_version_num", "detail", "recommendations"}),
    # PG 19 in-server REPACK.
    "get_repack_status": frozenset({"available", "server_version_num", "server_version", "detail"}),
    "repack_table": frozenset({"schema", "table", "concurrently", "repack_sql"}),
}


def _build_maximal_server() -> FastMCP:
    """Build a FastMCP server with every capability gate flipped on.

    Same shape as the fixture used by ``test_tool_surface_snapshot``;
    duplicated here to keep this test self-contained.
    """
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
        }
    )
    server: FastMCP = FastMCP("mcpg-output-schemas-fixture")
    register_tools(server, settings)
    return server


def test_converted_tools_emit_non_empty_output_schemas() -> None:
    """Every tool in the manifest must expose a populated JSON Schema."""
    server = _build_maximal_server()
    registered = {t.name: t for t in server._tool_manager.list_tools()}

    missing_from_server: list[str] = []
    schema_missing: list[str] = []
    for name in _TOOLS_WITH_STRUCTURED_OUTPUT:
        if name not in registered:
            missing_from_server.append(name)
            continue
        schema = registered[name].output_schema
        if not schema or not schema.get("properties"):
            schema_missing.append(name)
    assert not missing_from_server, (
        f"manifest references tools not registered on the maximal server: "
        f"{', '.join(missing_from_server)}. Either the tool was removed (update the "
        f"manifest) or a capability gate is now blocking it from registering."
    )
    assert not schema_missing, (
        f"the following tools are in the structured-output manifest but their "
        f"output_schema is None / empty: {', '.join(schema_missing)}. The most "
        f"common cause is a handler whose return annotation is still "
        f"`dict[str, Any]` — change it to the helper's dataclass return type."
    )


def test_converted_tools_output_schemas_carry_expected_fields() -> None:
    """The auto-derived schema must declare every expected dataclass field.

    Asserts on the JSON Schema's ``properties`` keys. Extra fields aren't
    flagged (an additive shape change is fine); missing fields trip the
    test so a field rename / removal can't slip past.
    """
    server = _build_maximal_server()
    registered = {t.name: t for t in server._tool_manager.list_tools()}

    drift: list[str] = []
    for name, expected_fields in _TOOLS_WITH_STRUCTURED_OUTPUT.items():
        if name not in registered:
            continue  # caught by the sibling test
        schema = registered[name].output_schema or {}
        properties = set((schema.get("properties") or {}).keys())
        missing = expected_fields - properties
        if missing:
            drift.append(f"{name}: missing fields {sorted(missing)}")
    assert not drift, (
        "the following tools' output_schema fields drifted from the manifest:\n  "
        + "\n  ".join(drift)
        + "\nUpdate the manifest if the rename is intentional; otherwise revert the helper change."
    )


def test_converted_tool_count_grows_monotonically() -> None:
    """Sanity gate — the structured-output manifest should never shrink.

    FastMCP auto-wraps even ``list[dict[str, Any]]`` returns into a
    ``{"result": [...]}`` envelope, so we can't usefully canary on
    "tools still annotated dict[str, Any] should have no schema" (they
    do, just a permissive one). Instead, we lock in a floor: as we
    sweep more tools onto typed returns the manifest grows; this test
    fails when the count drops below the recorded floor.

    Bump the ``floor`` literal below when adding to the manifest;
    never decrement it without a deliberate "we're rolling back
    structured output for tool X" conversation in the PR.
    """
    floor = 33
    actual = len(_TOOLS_WITH_STRUCTURED_OUTPUT)
    assert actual >= floor, (
        f"structured-output manifest dropped from at-least-{floor} tools "
        f"to {actual}. Either bump the floor down deliberately (and document "
        f"why in the PR), or restore the manifest entries that were removed."
    )
