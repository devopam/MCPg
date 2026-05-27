"""Tests for the NL→SQL helper (Phase 10.2)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from _fakes import FakeRoutingDriver

from mcpg.nl2sql import (
    DEFAULT_MODELS,
    HARD_MAX_TOKENS,
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
    NL2SQLError,
    OpenAIProvider,
    _parse_response,
    build_provider,
    is_valid_provider,
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
