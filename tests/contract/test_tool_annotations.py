"""Contract tests for MCP ``ToolAnnotations`` — the on-wire safety hints.

MCPg's READ / WRITE / DDL / SHELL / LISTEN gating is its core safety
story, but until the annotations sweep it never reached the wire — a
client saw ``run_select`` and ``terminate_backend`` as equally opaque.
These tests pin the derivation in ``tools._apply_tool_wire_metadata``:

1. Every registered tool carries annotations with ``readOnlyHint`` set.
2. The read-only set is exactly the surface reachable in read-only
   access mode (self-consistency: the gate IS the classification).
3. Known-dangerous tools are never marked read-only; canonical read
   tools always are.
4. ``openWorldHint`` is False everywhere except the tools that reach
   external services (the NL→SQL provider call).
5. Every write-capable tool carries an explicit ``destructiveHint``
   backed by the curated destructive / non-destructive partition —
   which must cover the write surface exactly, so a new write tool
   can't ship unclassified.
6. Every tool carries a human-readable title (directory listings —
   e.g. the Claude connector directory — require one per tool).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcpg.config import load_settings
from mcpg.tools import register_tools

_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"

# Spot-check anchors. Not exhaustive — the mode-diff test below is the
# exhaustive one — but these fail loudly with a readable message if the
# derivation ever inverts.
_MUST_BE_READ_ONLY = {"list_schemas", "run_select", "explain_query", "describe_table", "get_server_info"}
_MUST_NOT_BE_READ_ONLY = {"run_ddl", "terminate_backend", "cancel_query", "restore_database", "complete_migration"}
_OPEN_WORLD = {"translate_nl_to_sql"}


def _build_server(access_mode: str) -> FastMCP:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": access_mode,
            **(
                {"MCPG_ALLOW_DDL": "true", "MCPG_ALLOW_SHELL": "true", "MCPG_ALLOW_LISTEN": "true"}
                if access_mode == "unrestricted"
                else {}
            ),
        }
    )
    server: FastMCP = FastMCP(f"mcpg-annotations-fixture-{access_mode}")
    register_tools(server, settings)
    return server


def test_every_tool_carries_annotations_with_read_only_hint() -> None:
    server = _build_server("unrestricted")
    missing = [
        t.name for t in server._tool_manager.list_tools() if t.annotations is None or t.annotations.readOnlyHint is None
    ]
    assert not missing, f"tools without a readOnlyHint annotation: {missing}"


def test_read_only_hints_match_the_read_only_mode_surface() -> None:
    """The exhaustive check: readOnlyHint=True ⟺ registered in read-only mode.

    The access-mode gate is MCPg's actual enforcement boundary, so the
    hint must agree with it exactly — a tool reachable in read-only mode
    but hinted otherwise (or vice versa) means the derivation drifted.
    """
    maximal = _build_server("unrestricted")
    read_only_surface = {t.name for t in _build_server("read-only")._tool_manager.list_tools()}

    hinted_read_only = {
        t.name for t in maximal._tool_manager.list_tools() if t.annotations and t.annotations.readOnlyHint
    }
    assert hinted_read_only == read_only_surface, (
        f"hinted-but-not-gated: {sorted(hinted_read_only - read_only_surface)}; "
        f"gated-but-not-hinted: {sorted(read_only_surface - hinted_read_only)}"
    )


def test_spot_check_anchor_tools() -> None:
    server = _build_server("unrestricted")
    by_name = {t.name: t for t in server._tool_manager.list_tools()}
    for name in _MUST_BE_READ_ONLY:
        assert name in by_name, f"anchor tool missing from the unrestricted surface: {name}"
        assert by_name[name].annotations is not None and by_name[name].annotations.readOnlyHint is True, name
    for name in _MUST_NOT_BE_READ_ONLY:
        assert name in by_name, f"anchor tool missing from the unrestricted surface: {name}"
        assert by_name[name].annotations is not None and by_name[name].annotations.readOnlyHint is False, name


def test_open_world_hint_is_closed_except_external_service_tools() -> None:
    server = _build_server("unrestricted")
    for tool in server._tool_manager.list_tools():
        assert tool.annotations is not None
        expected = tool.name in _OPEN_WORLD
        assert tool.annotations.openWorldHint is expected, (
            f"{tool.name}: openWorldHint={tool.annotations.openWorldHint}, expected {expected}"
        )


async def test_annotations_reach_the_wire_via_list_tools() -> None:
    """What the MCP client actually receives must carry the hints too."""
    server = _build_server("unrestricted")
    wire_tools = await server.list_tools()
    sample = {t.name: t for t in wire_tools}
    assert sample["run_select"].annotations is not None
    assert sample["run_select"].annotations.readOnlyHint is True
    assert sample["run_select"].annotations.openWorldHint is False
    assert sample["run_ddl"].annotations is not None
    assert sample["run_ddl"].annotations.readOnlyHint is False
    assert sample["translate_nl_to_sql"].annotations is not None
    assert sample["translate_nl_to_sql"].annotations.openWorldHint is True


def test_sweep_preserves_annotations_a_registration_set_explicitly() -> None:
    """Derived hints fill gaps; they must never clobber explicit ones.

    No call site passes ``annotations=`` today, but a future per-tool
    override (say ``destructiveHint=False`` on a maintenance tool) has
    to survive the sweep — explicit beats derived, field by field.
    """
    from mcp.types import ToolAnnotations

    from mcpg.tools import _apply_tool_wire_metadata

    server: FastMCP = FastMCP("mcpg-annotations-merge-fixture")

    @server.tool(
        name="preexisting_annotated_tool",
        description="fixture",
        annotations=ToolAnnotations(title="Keep me", destructiveHint=False, readOnlyHint=False),
    )
    def preexisting() -> str:
        return "x"

    _apply_tool_wire_metadata(server, read_only_names={"preexisting_annotated_tool"})

    annotations = {t.name: t.annotations for t in server._tool_manager.list_tools()}["preexisting_annotated_tool"]
    assert annotations is not None
    assert annotations.title == "Keep me"  # preserved
    assert annotations.destructiveHint is False  # preserved
    assert annotations.readOnlyHint is False  # explicit False beats the derived True
    assert annotations.openWorldHint is False  # unset -> filled by the derivation


def test_destructive_partition_exactly_covers_the_write_surface() -> None:
    """The destructive/non-destructive sets partition the write surface exactly.

    The partition is hand-curated, so this is the drift guard: a new
    write-capable tool fails here until someone consciously classifies
    it, and a tool that stops existing can't linger in either set.
    """
    from mcpg.tools import _DESTRUCTIVE_TOOLS, _NON_DESTRUCTIVE_WRITE_TOOLS

    overlap = _DESTRUCTIVE_TOOLS & _NON_DESTRUCTIVE_WRITE_TOOLS
    assert not overlap, f"tools classified as both destructive and non-destructive: {sorted(overlap)}"

    maximal = _build_server("unrestricted")
    write_surface = {
        t.name for t in maximal._tool_manager.list_tools() if t.annotations and t.annotations.readOnlyHint is False
    }
    classified = _DESTRUCTIVE_TOOLS | _NON_DESTRUCTIVE_WRITE_TOOLS
    assert write_surface == classified, (
        f"unclassified write tools (add to one of the partitions in tools.py): {sorted(write_surface - classified)}; "
        f"classified but not registered as write tools: {sorted(classified - write_surface)}"
    )


def test_destructive_hint_is_explicit_on_writes_and_absent_on_reads() -> None:
    from mcpg.tools import _DESTRUCTIVE_TOOLS

    server = _build_server("unrestricted")
    for tool in server._tool_manager.list_tools():
        assert tool.annotations is not None
        if tool.annotations.readOnlyHint:
            # Meaningless for read-only tools per the MCP spec — stay unset.
            assert tool.annotations.destructiveHint is None, tool.name
        else:
            expected = tool.name in _DESTRUCTIVE_TOOLS
            assert tool.annotations.destructiveHint is expected, (
                f"{tool.name}: destructiveHint={tool.annotations.destructiveHint}, expected {expected}"
            )


async def test_every_tool_has_a_human_readable_title() -> None:
    server = _build_server("unrestricted")
    wire_tools = await server.list_tools()
    untitled = [t.name for t in wire_tools if not (t.title or "").strip()]
    assert not untitled, f"tools without a title: {untitled}"
    by_name = {t.name: t.title for t in wire_tools}
    # Spot-check the acronym/product-name rendering.
    assert by_name["run_ddl"] == "Run DDL"
    assert by_name["translate_nl_to_sql"] == "Translate NL to SQL"
    assert by_name["recommend_ivfflat_probes"] == "Recommend IVFFlat probes"
    assert by_name["get_pg19_ddl_status"] == "Get PG 19 DDL status"
    assert by_name["run_select"] == "Run SELECT"


async def test_every_prompt_argument_has_a_description() -> None:
    server = _build_server("read-only")
    prompts = await server.list_prompts()
    assert prompts, "expected the prompt surface to be registered in read-only mode"
    undescribed = [
        f"{p.name}.{a.name}" for p in prompts for a in (p.arguments or []) if not (a.description or "").strip()
    ]
    assert not undescribed, f"prompt arguments without descriptions: {undescribed}"
