"""Tests for the ``_with_example`` description helper used across tools.py.

The helper itself is trivial — these tests exist mostly so a regression
in the rendered format (which agents already depend on) gets caught at
PR time rather than after rollout, and so an unrelated refactor of
``tools.py`` doesn't silently drop the example sections from descriptions.
"""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.tools import _with_example

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


def test_with_example_appends_canonical_example_marker() -> None:
    rendered = _with_example("Do the thing.", "do_the_thing(arg='value')")
    assert "Do the thing." in rendered
    # The "Example:" marker is the contract — agents and downstream
    # tooling key off it. Keep it stable.
    assert "Example: `do_the_thing(arg='value')`" in rendered
    # And the example sits AFTER the description, separated by a blank
    # line, so it doesn't run into the prose when rendered as markdown.
    assert rendered.endswith("Example: `do_the_thing(arg='value')`")
    assert "\n\n" in rendered


def test_with_example_preserves_multiline_descriptions() -> None:
    # Real tool descriptions are often multi-line; the helper should
    # leave them alone and only tack the example onto the end.
    description = "Line one.\nLine two."
    rendered = _with_example(description, "example()")
    assert rendered.startswith("Line one.\nLine two.")
    assert rendered.endswith("Example: `example()`")


@pytest.mark.parametrize(
    "tool_name",
    [
        # A representative sample of the tools we wired up in F2.
        # If any of these stops shipping an example the agent UX
        # regresses — pin them explicitly.
        "list_schemas",
        "list_tables",
        "describe_table",
        "list_indexes",
        "run_select",
        "explain_query",
        "translate_nl_to_sql",
        "summarize_table",
        "check_database_health",
        "vector_search",
        "hybrid_search",
        "generate_schema_diagram",
        "compare_schemas",
        "cluster_vectors",
        "monitor_embedding_drift",
    ],
)
async def test_high_traffic_tools_ship_example_in_description(tool_name: str) -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    assert tool_name in by_name, f"{tool_name!r} not registered"
    description = by_name[tool_name].description or ""
    assert "Example: `" in description, (
        f"{tool_name!r} description is missing the example block — "
        "wrap its description in `_with_example(...)` to keep the agent UX consistent."
    )
