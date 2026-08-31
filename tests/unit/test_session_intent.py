"""Tests for the session-intent surface filter (roadmap 8.8)."""

from __future__ import annotations

from mcpg.session_intent import (
    INTENT_PRESETS,
    filter_server_tools,
    parse_intent_setting,
    resolve_intent_to_buckets,
)

# ---------------------------------------------------------------------------
# parse_intent_setting
# ---------------------------------------------------------------------------


def test_parse_returns_empty_for_none() -> None:
    assert parse_intent_setting(None) == ()


def test_parse_returns_empty_for_blank_string() -> None:
    assert parse_intent_setting("   ") == ()


def test_parse_strips_and_lowercases_each_entry() -> None:
    assert parse_intent_setting("LOOKUP, Migration ,  vector_rag") == (
        "lookup",
        "migration",
        "vector_rag",
    )


def test_parse_drops_empty_segments() -> None:
    assert parse_intent_setting("lookup,,monitor") == ("lookup", "monitor")


# ---------------------------------------------------------------------------
# resolve_intent_to_buckets
# ---------------------------------------------------------------------------


def test_resolve_returns_none_for_empty_input() -> None:
    """No intent configured ⇒ no filter applied."""
    assert resolve_intent_to_buckets(()) is None


def test_resolve_expands_lookup_preset() -> None:
    buckets = resolve_intent_to_buckets(("lookup",))
    assert buckets == INTENT_PRESETS["lookup"]


def test_resolve_admin_preset_short_circuits_to_none() -> None:
    """``admin`` is the no-filter sentinel — even mixed with other entries."""
    assert resolve_intent_to_buckets(("admin",)) is None
    assert resolve_intent_to_buckets(("lookup", "admin", "migration")) is None


def test_resolve_unions_multiple_presets() -> None:
    buckets = resolve_intent_to_buckets(("lookup", "monitor"))
    assert buckets == INTENT_PRESETS["lookup"] | INTENT_PRESETS["monitor"]


def test_resolve_treats_unknown_names_as_raw_bucket_ids() -> None:
    """A non-preset name is taken verbatim as a bucket id — the filter
    step decides whether anything matches it."""
    buckets = resolve_intent_to_buckets(("schema_introspection",))
    assert buckets == frozenset({"schema_introspection"})


def test_resolve_combines_preset_and_raw_bucket() -> None:
    buckets = resolve_intent_to_buckets(("lookup", "vector_search"))
    assert buckets == INTENT_PRESETS["lookup"] | {"vector_search"}


# ---------------------------------------------------------------------------
# filter_server_tools — uses a fake MCPServer with the minimum surface.
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeToolManager:
    def __init__(self, tool_names: list[str]) -> None:
        self._tools = [_FakeTool(n) for n in tool_names]

    def list_tools(self) -> list[_FakeTool]:
        return list(self._tools)


class _FakeServer:
    """Just enough of the MCPServer surface for filter_server_tools."""

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_manager = _FakeToolManager(tool_names)
        self.removed: list[str] = []

    def remove_tool(self, name: str) -> None:
        self._tool_manager._tools = [t for t in self._tool_manager._tools if t.name != name]
        self.removed.append(name)


def test_filter_removes_tools_outside_allowed_buckets() -> None:
    """list_tables → schema_introspection (kept); drop_database → operations
    (removed under lookup intent)."""
    server = _FakeServer(["list_tables", "drop_database"])
    removed = filter_server_tools(server, INTENT_PRESETS["lookup"])  # type: ignore[arg-type]
    assert "drop_database" in removed
    assert "list_tables" not in removed


def test_filter_keeps_describe_self_and_describe_tool_always() -> None:
    """These two are the introspection escape hatch — never removed."""
    server = _FakeServer(["describe_self", "describe_tool", "drop_database"])
    # Intent = vector_rag — neither describe_* belongs to its bucket set,
    # but they must survive.
    removed = filter_server_tools(server, INTENT_PRESETS["vector_rag"])  # type: ignore[arg-type]
    assert "describe_self" not in removed
    assert "describe_tool" not in removed
    assert "drop_database" in removed


def test_filter_is_idempotent() -> None:
    """Running twice removes nothing the second time."""
    server = _FakeServer(["list_tables", "drop_database"])
    filter_server_tools(server, INTENT_PRESETS["lookup"])  # type: ignore[arg-type]
    second_pass = filter_server_tools(server, INTENT_PRESETS["lookup"])  # type: ignore[arg-type]
    assert second_pass == []


def test_filter_returns_sorted_removed_list() -> None:
    server = _FakeServer(["zeta_tool", "alpha_tool", "drop_database"])
    # All three classify outside schema_introspection.
    removed = filter_server_tools(server, frozenset({"schema_introspection"}))  # type: ignore[arg-type]
    assert removed == sorted(removed)


def test_filter_with_unknown_buckets_removes_everything_except_keep_list() -> None:
    """A bogus bucket set keeps describe_* and removes everything else."""
    server = _FakeServer(["describe_self", "describe_tool", "list_tables", "drop_database"])
    removed = filter_server_tools(server, frozenset({"not_a_real_bucket"}))  # type: ignore[arg-type]
    assert removed == ["drop_database", "list_tables"]


# ---------------------------------------------------------------------------
# Preset shape sanity — every preset bucket id is a real bucket.
# ---------------------------------------------------------------------------


def test_every_preset_bucket_id_is_a_real_bucket() -> None:
    from mcpg.about import BUCKET_IDS

    for preset_name, buckets in INTENT_PRESETS.items():
        for bucket_id in buckets:
            assert bucket_id in BUCKET_IDS, f"intent preset {preset_name!r} references unknown bucket {bucket_id!r}"


def test_admin_preset_is_empty_set_sentinel() -> None:
    """Empty preset set is the 'no filter' sentinel; admin must use it."""
    assert INTENT_PRESETS["admin"] == frozenset()


# ---------------------------------------------------------------------------
# ALWAYS_KEEP (public export)
# ---------------------------------------------------------------------------


def test_always_keep_includes_the_dynamic_meta_tools() -> None:
    from mcpg.session_intent import ALWAYS_KEEP

    assert {
        "describe_self",
        "describe_tool",
        "list_session_intents",
        "enable_session_intent",
    } == ALWAYS_KEEP


# ---------------------------------------------------------------------------
# "core" preset
# ---------------------------------------------------------------------------


def test_core_preset_is_headline_tools_of_schema_and_query_buckets() -> None:
    from mcpg.about import CAPABILITIES
    from mcpg.session_intent import _TOOL_NAME_PRESETS

    expected = {
        name
        for cap in CAPABILITIES
        if cap.id in ("schema_introspection", "query_execution")
        for name in cap.headline_tools
    }
    assert _TOOL_NAME_PRESETS["core"] == frozenset(expected)
    assert len(_TOOL_NAME_PRESETS["core"]) == 12


# ---------------------------------------------------------------------------
# resolve_intent — two-set resolution
# ---------------------------------------------------------------------------


def test_resolve_intent_returns_none_for_empty_input() -> None:
    from mcpg.session_intent import resolve_intent

    assert resolve_intent(()) is None


def test_resolve_intent_bucket_preset_matches_resolve_intent_to_buckets() -> None:
    from mcpg.session_intent import IntentResolution, resolve_intent

    resolution = resolve_intent(("lookup",))
    assert resolution == IntentResolution(buckets=INTENT_PRESETS["lookup"], tool_names=frozenset())


def test_resolve_intent_admin_short_circuits_to_none() -> None:
    from mcpg.session_intent import resolve_intent

    assert resolve_intent(("admin",)) is None
    assert resolve_intent(("lookup", "admin")) is None


def test_resolve_intent_core_preset_is_tool_names_only() -> None:
    from mcpg.session_intent import _TOOL_NAME_PRESETS, resolve_intent

    resolution = resolve_intent(("core",))
    assert resolution is not None
    assert resolution.buckets == frozenset()
    assert resolution.tool_names == _TOOL_NAME_PRESETS["core"]


def test_resolve_intent_combines_bucket_and_tool_name_presets() -> None:
    from mcpg.session_intent import _TOOL_NAME_PRESETS, resolve_intent

    resolution = resolve_intent(("core", "monitor"))
    assert resolution is not None
    assert resolution.buckets == INTENT_PRESETS["monitor"]
    assert resolution.tool_names == _TOOL_NAME_PRESETS["core"]


def test_resolve_intent_unknown_name_falls_back_to_raw_bucket_id() -> None:
    from mcpg.session_intent import IntentResolution, resolve_intent

    resolution = resolve_intent(("vector_search",))
    assert resolution == IntentResolution(buckets=frozenset({"vector_search"}), tool_names=frozenset())


# ---------------------------------------------------------------------------
# resolved_tool_names — the shared keep-decision Layer 1 and Layer 2 both use
# ---------------------------------------------------------------------------


def test_resolved_tool_names_regression_core_keeps_14_not_0() -> None:
    """The bug caught during design: passing a tool-name-only resolution
    through a bucket-only keep-check kept 0 of 14 tools, not 14. This must
    pass against the fixed implementation."""
    from mcpg.session_intent import _TOOL_NAME_PRESETS, resolve_intent, resolved_tool_names

    resolution = resolve_intent(("core",))
    assert resolution is not None
    candidates = _TOOL_NAME_PRESETS["core"] | {"describe_self", "describe_tool", "run_ddl_unrelated_tool"}
    kept = resolved_tool_names(resolution, candidates)
    assert kept == _TOOL_NAME_PRESETS["core"] | {"describe_self", "describe_tool"}


def test_resolved_tool_names_bucket_preset_still_works() -> None:
    from mcpg.session_intent import resolve_intent, resolved_tool_names

    resolution = resolve_intent(("lookup",))
    assert resolution is not None
    candidates = ["list_tables", "run_ddl", "describe_self"]
    kept = resolved_tool_names(resolution, candidates)
    # list_tables -> schema_introspection (in lookup); run_ddl -> query_execution (in lookup);
    # describe_self -> always_keep regardless.
    assert kept == frozenset({"list_tables", "run_ddl", "describe_self"})
