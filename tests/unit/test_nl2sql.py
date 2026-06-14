"""Tests for the NL→SQL helper (Phase 10.2)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from _fakes import FakeRoutingDriver

from mcpg.config import load_settings
from mcpg.nl2sql import (
    DEFAULT_MODELS,
    HARD_MAX_TOKENS,
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
    NL2SQLError,
    OpenAIProvider,
    ProviderCallParams,
    _parse_response,
    build_provider,
    is_valid_provider,
    resolve_provider_call_params,
    translate_nl_to_sql,
)


@dataclass
class _StubProvider:
    """LLMProvider double — returns whatever ``response`` was given.

    Captures the prompts so tests can assert on the schema brief.
    """

    name: str = "stub"
    response: str = ""
    captured_system: str = ""
    captured_user: str = ""
    captured_model: str = ""
    captured_max_tokens: int = 0

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        timeout: float,
    ) -> str:
        _ = timeout
        self.captured_system = system_prompt
        self.captured_user = user_prompt
        self.captured_model = model
        self.captured_max_tokens = max_tokens
        return self.response


def _routes_for_simple_schema() -> dict[str, list[dict[str, object]]]:
    """SQL→rows routes that build a tiny one-table schema brief."""
    return {
        # list_tables — substring unique to its query (relispartition
        # only appears in this one).
        "c.relispartition": [{"name": "widget", "relkind": "r", "is_partition": False}],
        # describe_table — substring unique to its column listing query.
        "format_type(a.atttypid, a.atttypmod)": [
            {
                "column_name": "id",
                "data_type": "integer",
                "nullable": False,
                "column_default": None,
                "type_name": "int4",
                "type_mod": -1,
            },
            {
                "column_name": "name",
                "data_type": "text",
                "nullable": True,
                "column_default": None,
                "type_name": "text",
                "type_mod": -1,
            },
        ],
        # list_foreign_keys — empty for the simple schema.
        "WHERE con.contype = 'f'": [],
    }


def test_is_valid_provider_recognises_the_three_built_ins() -> None:
    assert is_valid_provider("anthropic")
    assert is_valid_provider("openai")
    assert is_valid_provider("gemini")
    assert not is_valid_provider("perplexity")


def test_build_provider_returns_the_right_concrete_class() -> None:
    assert isinstance(build_provider("anthropic", "key"), AnthropicProvider)
    assert isinstance(build_provider("openai", "key"), OpenAIProvider)
    assert isinstance(build_provider("gemini", "key"), GeminiProvider)


def test_build_provider_rejects_unknown_name() -> None:
    with pytest.raises(NL2SQLError, match="unknown"):
        build_provider("bogus", "key")


def test_default_models_table_covers_every_supported_provider() -> None:
    assert set(DEFAULT_MODELS) == {"anthropic", "openai", "gemini"}


def test_parse_response_extracts_sql_and_explanation_from_clean_json() -> None:
    raw = '{"sql": "SELECT 1", "explanation": "smoke test"}'
    sql, explanation = _parse_response(raw)
    assert sql == "SELECT 1"
    assert explanation == "smoke test"


def test_parse_response_strips_markdown_code_fences() -> None:
    raw = '```json\n{"sql": "SELECT 1", "explanation": "fenced"}\n```'
    sql, explanation = _parse_response(raw)
    assert sql == "SELECT 1"
    assert explanation == "fenced"


def test_parse_response_falls_back_to_raw_text_on_invalid_json() -> None:
    sql, explanation = _parse_response("I cannot answer this question.")
    assert sql == ""
    assert "cannot answer" in explanation


async def test_translate_nl_to_sql_rejects_blank_question() -> None:
    with pytest.raises(NL2SQLError, match="empty"):
        await translate_nl_to_sql(
            FakeRoutingDriver({}),  # type: ignore[arg-type]
            provider=_StubProvider(),  # type: ignore[arg-type]
            model="claude-sonnet-4-6",
            question="   ",
            schema="public",
        )


async def test_translate_nl_to_sql_rejects_max_tokens_above_hard_cap() -> None:
    with pytest.raises(NL2SQLError, match="max_tokens"):
        await translate_nl_to_sql(
            FakeRoutingDriver({}),  # type: ignore[arg-type]
            provider=_StubProvider(),  # type: ignore[arg-type]
            model="claude-sonnet-4-6",
            question="count widgets",
            schema="public",
            max_tokens=HARD_MAX_TOKENS + 1,
        )


async def test_translate_nl_to_sql_returns_generation_only_when_execute_false() -> None:
    provider = _StubProvider(response='{"sql": "SELECT count(*) FROM public.widget", "explanation": "row count"}')
    driver = FakeRoutingDriver(_routes_for_simple_schema())

    result = await translate_nl_to_sql(
        driver,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model="claude-sonnet-4-6",
        question="how many widgets are there?",
        schema="public",
        execute=False,
    )

    assert result.sql == "SELECT count(*) FROM public.widget"
    assert result.explanation == "row count"
    assert result.executed is False
    assert result.rows == []
    assert result.error is None
    # Schema brief reached the model — sanity-check that the table name
    # made it into the user prompt.
    assert "public.widget" in provider.captured_user
    assert "id: integer" in provider.captured_user


async def test_translate_nl_to_sql_records_provider_and_model_on_the_result() -> None:
    provider = _StubProvider(response='{"sql": "SELECT 1", "explanation": "smoke"}')

    result = await translate_nl_to_sql(
        FakeRoutingDriver(_routes_for_simple_schema()),  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model="my-model-id",
        question="smoke",
        schema="public",
    )

    assert result.model == "my-model-id"
    assert result.provider == "stub"


async def test_translate_nl_to_sql_reports_error_when_model_returns_no_sql() -> None:
    provider = _StubProvider(response='{"sql": "", "explanation": "out of scope"}')

    result = await translate_nl_to_sql(
        FakeRoutingDriver(_routes_for_simple_schema()),  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model="m",
        question="something orthogonal",
        schema="public",
    )

    assert result.sql == ""
    assert result.executed is False
    assert result.error == "model returned no SQL"


async def test_translate_nl_to_sql_executes_when_safe_and_returns_rows() -> None:
    """The generated SELECT goes through SafeSqlDriver + run_select."""
    provider = _StubProvider(response='{"sql": "SELECT id FROM widget", "explanation": "list ids"}')
    # Routes: schema-brief queries (specific substrings), then a route
    # for the user's SELECT that supplies the rows run_select sees.
    routes = _routes_for_simple_schema()
    routes["FROM widget"] = [{"id": 1}, {"id": 2}]
    driver = FakeRoutingDriver(routes)

    result = await translate_nl_to_sql(
        driver,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model="m",
        question="list ids",
        schema="public",
        execute=True,
    )

    assert result.executed is True
    assert result.rows == [{"id": 1}, {"id": 2}]
    assert result.columns == ["id"]
    assert result.row_count == 2
    assert result.error is None


async def test_translate_nl_to_sql_rejects_unsafe_sql_at_execution_layer() -> None:
    """If the model hallucinates a DELETE, run_select's safety allowlist
    catches it — the tool reports the error rather than executing."""
    provider = _StubProvider(response='{"sql": "DELETE FROM widget", "explanation": "destructive"}')

    result = await translate_nl_to_sql(
        FakeRoutingDriver(_routes_for_simple_schema()),  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model="m",
        question="wipe the table",
        schema="public",
        execute=True,
    )

    assert result.executed is False
    assert result.sql == "DELETE FROM widget"
    assert result.error is not None
    # The SafeSqlDriver allowlist message ends up in `error` — we don't
    # pin the exact wording (it's vendored), just that it surfaces.
    assert result.rows == []


def test_llm_provider_protocol_is_satisfied_by_the_concrete_classes() -> None:
    # Structural check: the three concrete providers each match the
    # Protocol surface (name + complete signature).
    for provider in (
        AnthropicProvider(api_key="x"),
        OpenAIProvider(api_key="x"),
        GeminiProvider(api_key="x"),
    ):
        assert isinstance(provider, LLMProvider)  # type: ignore[arg-type]
        assert hasattr(provider, "complete")
        assert provider.name in DEFAULT_MODELS


def test_parse_response_returns_empty_when_json_is_not_an_object() -> None:
    """Regression: a JSON list / scalar must not crash ``.get`` access.

    Some models return ``"sorry"`` (a quoted string) or a JSON array
    when they don't understand the schema. ``json.loads`` succeeds on
    both; the result must fall through to the raw-text branch.
    """
    sql, explanation = _parse_response('"sorry, I cannot answer"')
    assert sql == ""
    assert "sorry" in explanation

    sql, explanation = _parse_response("[1, 2, 3]")
    assert sql == ""
    assert "[1, 2, 3]" in explanation


async def test_translate_nl_to_sql_handles_curly_braces_in_the_question() -> None:
    """Regression: a question containing ``{`` / ``}`` (e.g. asking about a
    jsonb literal) must NOT crash the prompt builder."""
    provider = _StubProvider(response='{"sql": "SELECT 1", "explanation": "ok"}')

    result = await translate_nl_to_sql(
        FakeRoutingDriver(_routes_for_simple_schema()),  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        model="m",
        question="find rows where data = '{\"k\": 1}' and {empty: braces}",
        schema="public",
    )

    # No crash, and the question reached the model verbatim.
    assert result.sql == "SELECT 1"
    assert '{"k": 1}' in provider.captured_user
    assert "{empty: braces}" in provider.captured_user


# --- resolve_provider_call_params (PR-G: business logic relocated
# from the tools.py wrapper). The tool wrapper now just calls this
# helper, builds the provider, and forwards to translate_nl_to_sql,
# so the resolution rules need direct test coverage here. -----------

_DB_URL = "postgresql://u:p@localhost/db"


def _settings(**extra: str):
    env = {"MCPG_DATABASE_URL": _DB_URL}
    env.update(extra)
    return load_settings(env)


def test_resolve_provider_call_params_uses_explicit_request_arg_over_default() -> None:
    settings = _settings(
        ANTHROPIC_API_KEY="ant-1",
        OPENAI_API_KEY="oai-1",
        MCPG_NL2SQL_PROVIDER="anthropic",
    )
    params = resolve_provider_call_params(settings, "openai")
    assert isinstance(params, ProviderCallParams)
    assert params.provider_name == "openai"
    assert params.api_key == "oai-1"
    # ``model`` falls back to the upstream default when this call
    # ISN'T using the configured default provider — operator's
    # MCPG_NL2SQL_MODEL would be Anthropic-shaped here.
    assert params.model == DEFAULT_MODELS["openai"]
    assert params.base_url is None


def test_resolve_provider_call_params_applies_overrides_only_on_default_call() -> None:
    settings = _settings(
        ANTHROPIC_API_KEY="ant-1",
        MCPG_NL2SQL_PROVIDER="anthropic",
        MCPG_NL2SQL_MODEL="claude-sonnet-X",
        MCPG_NL2SQL_BASE_URL="https://proxy.example",
    )
    # No explicit provider arg → uses the configured default → the
    # operator's model + base_url overrides ALSO apply.
    params = resolve_provider_call_params(settings, None)
    assert params.provider_name == "anthropic"
    assert params.model == "claude-sonnet-X"
    assert params.base_url == "https://proxy.example"


def test_resolve_provider_call_params_normalises_case_and_whitespace() -> None:
    settings = _settings(ANTHROPIC_API_KEY="ant-1", MCPG_NL2SQL_PROVIDER="anthropic")
    params = resolve_provider_call_params(settings, "  ANTHROPIC  ")
    assert params.provider_name == "anthropic"


def test_resolve_provider_call_params_errors_when_nothing_configured() -> None:
    settings = _settings()  # no vendor keys, no MCPG_NL2SQL_PROVIDER
    with pytest.raises(NL2SQLError, match="has no provider configured"):
        resolve_provider_call_params(settings, None)


def test_resolve_provider_call_params_errors_on_unknown_provider() -> None:
    settings = _settings(ANTHROPIC_API_KEY="ant-1", MCPG_NL2SQL_PROVIDER="anthropic")
    with pytest.raises(NL2SQLError, match="unknown NL"):
        resolve_provider_call_params(settings, "azure")


def test_resolve_provider_call_params_errors_when_caller_picks_unconfigured() -> None:
    settings = _settings(ANTHROPIC_API_KEY="ant-1", MCPG_NL2SQL_PROVIDER="anthropic")
    with pytest.raises(NL2SQLError, match="provider 'openai' is not configured"):
        resolve_provider_call_params(settings, "openai")


def test_resolve_falls_back_to_default_when_request_arg_is_whitespace_only() -> None:
    """Regression for gemini review on #102: ``(req or default or "")``
    short-circuits on a truthy whitespace string, which then strips
    to empty, which then ``or None``s back to nothing — burying a
    perfectly-valid operator default behind a misleading "no provider
    configured" error. Each candidate is now normalized individually."""
    settings = _settings(ANTHROPIC_API_KEY="ant-1", MCPG_NL2SQL_PROVIDER="anthropic")
    params = resolve_provider_call_params(settings, "   ")
    assert params.provider_name == "anthropic"
    assert params.api_key == "ant-1"


def test_resolve_compares_against_normalized_default_for_override_decision() -> None:
    """Defence-in-depth check raised on the gemini review on #102.

    ``load_settings`` already strips + lowercases ``MCPG_NL2SQL_PROVIDER``
    (config.py:544), so the env path can't carry stray casing into
    ``settings.nl2sql_provider`` — meaning the agent's "private-proxy
    traffic leaks to a public endpoint" scenario doesn't fire through
    the documented config path. But code that constructs ``Settings``
    directly (test fixtures, Python-only bootstraps) can. Normalizing
    the default before the equality check in
    ``resolve_provider_call_params`` keeps the override logic robust
    regardless of how ``Settings`` got built.

    Asserted via the load_settings path: when the operator's default
    matches the chosen call, both overrides must apply."""
    settings = _settings(
        ANTHROPIC_API_KEY="ant-1",
        MCPG_NL2SQL_PROVIDER="anthropic",
        MCPG_NL2SQL_MODEL="claude-sonnet-X",
        MCPG_NL2SQL_BASE_URL="https://proxy.example",
    )
    params = resolve_provider_call_params(settings, None)
    assert params.provider_name == "anthropic"
    assert params.model == "claude-sonnet-X"
    assert params.base_url == "https://proxy.example"
