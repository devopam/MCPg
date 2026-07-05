"""Tool-level tests for the `translate_nl_to_sql` provider routing.

The lower-level :func:`mcpg.nl2sql.translate_nl_to_sql` is covered in
:mod:`test_nl2sql`; this file specifically exercises the **tool
wrapper's** provider-selection logic — the bit that consults
``Settings.nl2sql_api_keys`` and dispatches a call to the matching
provider via ``provider=``.

Success-path tests would need to mock out the LLM HTTP call, which
is unrelated to routing — covered separately in `test_nl2sql`.
These tests focus on the resolution + error paths.
"""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import Settings, load_settings
from mcpg.server import create_server


def _settings_with(env_extra: dict[str, str]) -> Settings:
    return load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", **env_extra})


async def test_translate_nl_to_sql_errors_clearly_when_no_provider_configured() -> None:
    settings = _settings_with({})
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "translate_nl_to_sql",
            {"question": "How many widgets?", "schema": "public"},
        )

    assert result.isError is True
    msg = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    assert "no provider configured" in msg
    assert "ANTHROPIC_API_KEY" in msg


async def test_translate_nl_to_sql_errors_when_caller_picks_unconfigured_provider() -> None:
    # Anthropic is configured; the caller asks for openai (which isn't).
    settings = _settings_with({"ANTHROPIC_API_KEY": "sk-ant"})
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "translate_nl_to_sql",
            {"question": "x", "schema": "public", "provider": "openai"},
        )

    assert result.isError is True
    msg = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    assert "'openai' is not configured" in msg
    assert "anthropic" in msg  # tells the caller what IS configured
    assert "OPENAI_API_KEY" in msg


@pytest.mark.parametrize("provider_arg", ["Anthropic", " ANTHROPIC ", "  anthropic\t"])
async def test_translate_nl_to_sql_normalises_provider_arg_case_and_whitespace(provider_arg: str) -> None:
    # Anthropic is the only configured provider. A mixed-case /
    # whitespace-padded `provider=` should still resolve to it — the
    # caller getting an "unknown provider" or "not configured" error
    # because of stray whitespace would be surprising. Hitting the same
    # code path as the lower-case form means we don't get one of those
    # errors; we get an "openai is not configured" error only when the
    # name itself is wrong.
    settings = _settings_with({"ANTHROPIC_API_KEY": "sk-ant"})
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        # We can't easily assert success here without mocking the LLM
        # HTTP call, but we CAN assert that the resolution step doesn't
        # error on case / whitespace — i.e. we don't get an
        # "unknown provider" or "not configured" message. Any error we
        # do see should be from the downstream HTTP call.
        result = await client.call_tool(
            "translate_nl_to_sql",
            {"question": "x", "schema": "public", "provider": provider_arg},
        )
        msg = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        assert "unknown NL→SQL provider" not in msg
        assert "is not configured" not in msg


async def test_translate_nl_to_sql_errors_on_unknown_provider_name() -> None:
    settings = _settings_with({"ANTHROPIC_API_KEY": "sk-ant"})
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "translate_nl_to_sql",
            {"question": "x", "schema": "public", "provider": "cohere"},
        )

    assert result.isError is True
    msg = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    assert "unknown NL→SQL provider" in msg
    assert "anthropic" in msg


async def test_valid_but_unconfigured_new_vendor_names_its_env_var() -> None:
    """deepseek is a real provider now — picking it without a key must say
    which env var enables it, not call it unknown."""
    settings = _settings_with({"ANTHROPIC_API_KEY": "sk-ant"})
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "translate_nl_to_sql",
            {"question": "x", "schema": "public", "provider": "deepseek"},
        )

    assert result.isError is True
    msg = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    assert "not configured" in msg
    assert "DEEPSEEK_API_KEY" in msg


async def test_get_server_info_surfaces_default_and_available_providers() -> None:
    settings = _settings_with(
        {
            "ANTHROPIC_API_KEY": "sk-ant",
            "OPENAI_API_KEY": "sk-oa",
            "MCPG_NL2SQL_PROVIDER": "openai",
        }
    )
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("get_server_info", {})

    assert result.isError is False
    info = result.structuredContent
    assert info is not None
    assert info["nl2sql_default_provider"] == "openai"
    assert sorted(info["nl2sql_available_providers"]) == ["anthropic", "openai"]


@pytest.mark.parametrize(
    ("env", "expected_default"),
    [
        ({"ANTHROPIC_API_KEY": "x"}, "anthropic"),
        ({"OPENAI_API_KEY": "x"}, "openai"),
        ({"GEMINI_API_KEY": "x"}, "gemini"),
        ({"GOOGLE_API_KEY": "x"}, "gemini"),
        # All three present, no explicit pin → preference order anthropic.
        (
            {
                "ANTHROPIC_API_KEY": "x",
                "OPENAI_API_KEY": "x",
                "GEMINI_API_KEY": "x",
            },
            "anthropic",
        ),
        # OpenAI + Gemini, no anthropic → openai wins.
        (
            {"OPENAI_API_KEY": "x", "GEMINI_API_KEY": "x"},
            "openai",
        ),
    ],
)
async def test_default_provider_resolution_via_get_server_info(env: dict[str, str], expected_default: str) -> None:
    settings = _settings_with(env)
    server = create_server(settings, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("get_server_info", {})

    info = result.structuredContent
    assert info is not None
    assert info["nl2sql_default_provider"] == expected_default
