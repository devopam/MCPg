"""Contract test for the MCP resources surface.

Pins the four `mcpg://…` resource URIs MCPg registers on a
maximal-flag server. Resources are MCP's preload-on-connect primitive
(separate from tools and prompts); without this test, a refactor in
`mcpg.tools._register_resources` or `mcpg.resources` could silently
drop a resource and clients would only notice at runtime.

Companion to `test_tool_surface_snapshot.py` and
`test_tool_output_schemas.py` — same shape (build a maximal server,
inspect what registered, assert against an explicit manifest).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcpg.config import load_settings
from mcpg.tools import register_tools

# Mirrors the fixture URL the sibling contract tests use — never
# actually connected.
_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"

# Static resources expected on every maximal-flag server. The
# template-typed siblings (`mcpg://capabilities/{bucket_id}`,
# `mcpg://schema/{schema_name}`) are checked separately because
# FastMCP routes them through `list_templates` not `list_resources`.
_EXPECTED_STATIC_RESOURCES: frozenset[str] = frozenset(
    {
        "mcpg://about/index",
        "mcpg://capabilities/index",
    }
)

# URI templates expected on every maximal-flag server. Each entry
# includes the variable name(s) for the routing-shape assertion.
_EXPECTED_TEMPLATE_RESOURCES: dict[str, frozenset[str]] = {
    "mcpg://capabilities/{bucket_id}": frozenset({"bucket_id"}),
    "mcpg://schema/{schema_name}": frozenset({"schema_name"}),
}


def _build_maximal_server() -> FastMCP:
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
        }
    )
    server: FastMCP = FastMCP("mcpg-resources-fixture")
    register_tools(server, settings)
    return server


def test_static_resources_match_manifest() -> None:
    """Every URI in the static-resources manifest must be registered."""
    server = _build_maximal_server()
    registered = {str(r.uri) for r in server._resource_manager.list_resources()}
    missing = _EXPECTED_STATIC_RESOURCES - registered
    extra = registered - _EXPECTED_STATIC_RESOURCES
    assert not missing, (
        f"static resources missing from the registered set: {sorted(missing)}. "
        f"Either restore the registration in `_register_resources` "
        f"(`src/mcpg/tools.py`) or drop the manifest entry deliberately."
    )
    assert not extra, (
        f"unexpected static resources registered: {sorted(extra)}. "
        f"Add them to the manifest above with an explanation, or remove the "
        f"registration if unintended."
    )


def test_template_resources_match_manifest() -> None:
    """Every URI template + variable set in the template manifest must be registered."""
    server = _build_maximal_server()
    registered = {
        t.uri_template: set(t.parameters.get("properties", {}).keys())
        for t in server._resource_manager.list_templates()
    }
    drift: list[str] = []
    for uri_template, expected_vars in _EXPECTED_TEMPLATE_RESOURCES.items():
        if uri_template not in registered:
            drift.append(f"  missing: {uri_template}")
            continue
        actual_vars = registered[uri_template]
        if actual_vars != expected_vars:
            drift.append(
                f"  variable drift on {uri_template}: expected {sorted(expected_vars)}, got {sorted(actual_vars)}"
            )
    extra = set(registered.keys()) - set(_EXPECTED_TEMPLATE_RESOURCES.keys())
    if extra:
        drift.append(f"  unexpected templates: {sorted(extra)}")
    assert not drift, "MCP resource templates drifted from manifest:\n" + "\n".join(drift)


def test_static_resources_carry_json_mime_type() -> None:
    """Every resource we register emits JSON — uniformity is the contract."""
    server = _build_maximal_server()
    for resource in server._resource_manager.list_resources():
        if str(resource.uri).startswith("mcpg://"):
            assert resource.mime_type == "application/json", (
                f"{resource.uri} should emit application/json, got "
                f"{resource.mime_type!r}. JSON is the uniformity contract for "
                f"mcpg://… resources — see `src/mcpg/resources.py` docstring."
            )


def test_template_resources_carry_json_mime_type() -> None:
    """Same uniformity contract for templates."""
    server = _build_maximal_server()
    for template in server._resource_manager.list_templates():
        if template.uri_template.startswith("mcpg://"):
            assert template.mime_type == "application/json", (
                f"{template.uri_template} should emit application/json, got {template.mime_type!r}."
            )
