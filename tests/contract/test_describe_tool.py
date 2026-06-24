"""End-to-end contract for the ``describe_tool`` MCP tool.

The unit tests in `tests/unit/test_tool_introspection.py` exercise the
builders against canned inputs. This test wires the tool into a
maximal-flag FastMCP server and calls it through the real MCP
dispatch path, so the assertions cover the live shape an agent will
actually see — including ``inputSchema`` derivation from the
function signature, the FastMCP registration plumbing, and the
`mcpg.about` bucket lookup against the canonical capability list.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from mcpg.config import load_settings
from mcpg.tools import register_tools

_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"


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
    server: FastMCP = FastMCP("mcpg-describe-tool-fixture")
    register_tools(server, settings)
    return server


async def _call_describe_tool(server: FastMCP, name: str) -> dict[str, Any]:
    """Invoke ``describe_tool`` through the MCP call path and return the
    parsed JSON payload from the response.

    FastMCP returns ``(content_list, structured_content)`` for typed
    tools (with a populated outputSchema) and the dict-returning shape
    for the others. We accept either so the assertion doesn't bind on
    FastMCP's internal envelope across versions.
    """
    result: Any = await server.call_tool("describe_tool", {"name": name})
    if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], dict):
        return result[1]
    content_list: Any = result[0] if isinstance(result, tuple) else result
    first = content_list[0]
    text = getattr(first, "text", None)
    assert text is not None, f"unexpected tool result shape: {result!r}"
    parsed: dict[str, Any] = json.loads(text)
    return parsed


@pytest.mark.asyncio
async def test_describe_tool_returns_full_descriptor_for_a_real_tool() -> None:
    server = _build_maximal_server()
    descriptor = await _call_describe_tool(server, "run_select")
    assert descriptor["registered"] is True
    assert descriptor["name"] == "run_select"
    # The description, input_schema, and bucket fields must all be
    # populated for a flagship tool — if any of these is missing the
    # descriptor isn't actionable from an agent's POV.
    assert descriptor["description"]
    assert descriptor["input_schema"]["type"] == "object"
    assert descriptor["bucket"] is not None
    assert descriptor["bucket"]["id"] == "query_execution"


@pytest.mark.asyncio
async def test_describe_tool_returns_did_you_mean_for_a_typo() -> None:
    server = _build_maximal_server()
    descriptor = await _call_describe_tool(server, "run_sleect")
    assert descriptor["registered"] is False
    # `run_select` should be in the close-match suggestions for the typo.
    assert "run_select" in descriptor["did_you_mean"]


@pytest.mark.asyncio
async def test_describe_tool_returns_empty_did_you_mean_for_a_truly_unknown_name() -> None:
    server = _build_maximal_server()
    descriptor = await _call_describe_tool(server, "totally_invented_xyzzy_qux")
    assert descriptor["registered"] is False
    assert descriptor["did_you_mean"] == []


@pytest.mark.asyncio
async def test_describe_tool_is_itself_registered_and_describable() -> None:
    """The bootstrapping check — describe_tool can introspect itself.
    Useful sanity, and serves as the smoke test for any future refactor
    of the registration plumbing."""
    server = _build_maximal_server()
    descriptor = await _call_describe_tool(server, "describe_tool")
    assert descriptor["registered"] is True
    assert descriptor["bucket"] is not None
    assert descriptor["bucket"]["id"] == "observability"
