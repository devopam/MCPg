"""Contract test for the MCP prompts surface.

Pins the prompt names + argument shapes MCPg registers on a
maximal-flag server. Prompts are MCP's pre-built-workflow primitive
(separate from tools and resources); without this test, a refactor in
`mcpg.tools._register_prompts` or `mcpg.prompts` could silently drop a
prompt and clients would only notice when their `prompts/list` reply
shrank at runtime.

Companion to `test_mcp_resources.py` — same shape, same fixture URL.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcpg.config import load_settings
from mcpg.tools import register_tools

_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"

# Prompt name → ordered tuple of (arg_name, required) pairs.
# Order matters in the manifest because FastMCP keeps the function's
# signature order; agents calling via `prompts/get` send arguments by
# name so order doesn't bind on the wire, but the manifest order is
# the canonical signature the docstring should reference.
_EXPECTED_PROMPTS: dict[str, tuple[tuple[str, bool], ...]] = {
    "diagnose_slow_query": (("sql", True),),
    "bisect_slow_migration": (
        ("migration_id", True),
        ("baseline_schema", True),
        ("current_schema", True),
    ),
    "review_rls_policy": (
        ("schema", True),
        ("table", True),
    ),
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
    server: FastMCP = FastMCP("mcpg-prompts-fixture")
    register_tools(server, settings)
    return server


def test_prompts_match_manifest() -> None:
    """Every prompt in the manifest must be registered with the right argument shape."""
    server = _build_maximal_server()
    registered = {p.name: p for p in server._prompt_manager.list_prompts()}

    missing = set(_EXPECTED_PROMPTS.keys()) - set(registered.keys())
    extra = set(registered.keys()) - set(_EXPECTED_PROMPTS.keys())
    assert not missing, (
        f"prompts missing from the registered set: {sorted(missing)}. "
        f"Either restore the registration in `_register_prompts` "
        f"(`src/mcpg/tools.py`) or drop the manifest entry deliberately."
    )
    assert not extra, (
        f"unexpected prompts registered: {sorted(extra)}. "
        f"Add them to the manifest above with an explanation, or remove the "
        f"registration if unintended."
    )

    drift: list[str] = []
    for name, expected_args in _EXPECTED_PROMPTS.items():
        actual_args = tuple((arg.name, arg.required) for arg in (registered[name].arguments or []))
        if actual_args != expected_args:
            drift.append(f"  {name}: expected args {expected_args}, got {actual_args}")
    assert not drift, "MCP prompt argument shapes drifted from manifest:\n" + "\n".join(drift)


def test_prompts_carry_human_readable_description() -> None:
    """Every prompt must ship a non-empty description for client menus."""
    server = _build_maximal_server()
    for prompt in server._prompt_manager.list_prompts():
        if prompt.name in _EXPECTED_PROMPTS:
            assert prompt.description and len(prompt.description) > 20, (
                f"{prompt.name} should ship a meaningful description; got "
                f"{prompt.description!r}. Descriptions appear in the client's "
                f"prompts menu — terse names alone aren't enough context."
            )


def test_prompts_carry_human_readable_title() -> None:
    """Every prompt must ship a title for client picker UIs."""
    server = _build_maximal_server()
    for prompt in server._prompt_manager.list_prompts():
        if prompt.name in _EXPECTED_PROMPTS:
            assert prompt.title, (
                f"{prompt.name} should ship a `title=` kwarg on its decorator. "
                f"Clients fall back to the name when the title is missing, "
                f"which reads as unpolished in picker UIs."
            )


# Sample arguments used to render each prompt body. Real values shouldn't
# matter for tool-name extraction, but every required arg must be present
# so `render` doesn't reject the call.
_PROMPT_RENDER_ARGS: dict[str, dict[str, str]] = {
    "diagnose_slow_query": {"sql": "SELECT 1"},
    "bisect_slow_migration": {
        "migration_id": "m1",
        "baseline_schema": "baseline",
        "current_schema": "current",
    },
    "review_rls_policy": {"schema": "public", "table": "widgets"},
}

def _extract_tool_names_from_body(body: str) -> set[str]:
    """Pull tool-call references out of a prompt body.

    Prompt bodies mix tool calls with reason codes, column-name
    examples, catalog references, severity labels — all of which can
    look like ``lower_snake_case`` identifiers in backticks. To
    distinguish, we only accept two narrow shapes that *unambiguously*
    denote a tool call:

      1. ``\\`name(...)\\``` — the agent is meant to call it.
      2. ``Call \\`name\\``` / ``call \\`name\\``` / ``via \\`name\\``` —
         the surrounding prose explicitly names it as something to
         invoke.

    Anything else (`` `tenant_id` ``, `` `critical` ``, `` `pg_stat_user_tables` ``)
    stays out of the candidate set. New tool references should follow
    one of the two shapes above so the contract test continues to
    cover them.
    """
    import re

    # Shape 1: backtick-wrapped function-call form.
    parens = re.compile(r"`([a-z][a-z0-9_]+)\([^`]*\)`")
    # Shape 2: prose verb ("Call" / "call" / "via" / "use") immediately
    # preceding a backtick-wrapped identifier. Case-insensitive on the
    # verb; the identifier itself stays lowercase.
    prose = re.compile(r"\b(?:[Cc]all|via|[Uu]se)\s+`([a-z][a-z0-9_]+)`")
    return set(parens.findall(body)) | set(prose.findall(body))


async def test_every_tool_name_in_every_prompt_body_is_actually_registered() -> None:
    """Catch the failure mode the original unit test missed.

    Asserting that a prompt mentions `recommend_indexes` proves nothing
    if no tool by that name exists on the server. This test renders
    each prompt with sample args, extracts every backtick-wrapped
    lower_snake_case identifier, and verifies each one matches a real
    registered tool. Without this guardrail, prompts can ship that
    route agents to dead-end tool calls (gemini-code-assist found
    four such drifts in PR #161's first cut).
    """
    server = _build_maximal_server()
    registered_tools = {t.name for t in await server.list_tools()}

    drift: list[str] = []
    for prompt in server._prompt_manager.list_prompts():
        if prompt.name not in _EXPECTED_PROMPTS:
            continue
        render_args = _PROMPT_RENDER_ARGS[prompt.name]
        messages = await prompt.render(arguments=render_args)
        for message in messages:
            body = getattr(message.content, "text", "") or ""
            referenced = _extract_tool_names_from_body(body)
            missing = referenced - registered_tools
            if missing:
                drift.append(
                    f"  {prompt.name}: references unregistered tools {sorted(missing)}"
                )

    assert not drift, (
        "Prompt bodies reference tool names that don't exist on the registered server:\n"
        + "\n".join(drift)
        + "\n\nEither fix the prompt to use the canonical tool name, or add the "
        "name to `_FALSE_POSITIVE_NAMES` above if it's a false positive."
    )
