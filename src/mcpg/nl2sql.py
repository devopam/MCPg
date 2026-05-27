"""Natural-language → SQL helper.

``translate_nl_to_sql`` gathers schema context, asks a configurable
LLM to translate the question into a read-only SQL query, and
optionally runs it through the existing safety + execution stack.

Provider plumbing is intentionally thin: three concrete classes
(:class:`AnthropicProvider`, :class:`OpenAIProvider`,
:class:`GeminiProvider`) talk to each vendor's HTTPS API directly via
``httpx``. No SDK dependency, no hidden state. The provider is picked
at startup via ``MCPG_NL2SQL_PROVIDER``; the API key is read from
``MCPG_NL2SQL_API_KEY`` (or the vendor's conventional environment
variable as a fallback).

Safety:

* The generated SQL is passed through ``SafeSqlDriver``'s allowlist
  before execution — writes / DDL / multi-statement input are
  rejected regardless of what the model produced.
* Execution is opt-in (``execute=False`` by default); the agent gets
  the SQL to review before running.
* The LLM is instructed to return JSON with two fields (``sql``,
  ``explanation``). When the response fails to parse, the raw text
  is surfaced as ``explanation`` and ``sql`` is empty — agents see
  the failure instead of silently running garbage.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import describe_table, list_foreign_keys, list_tables
from mcpg.query import DEFAULT_MAX_ROWS, QueryError, run_select

logger = logging.getLogger(__name__)

# Default models per provider — chosen for low cost / high availability
# at writing time. Override via ``MCPG_NL2SQL_MODEL``.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
}

# Conservative budget — NL→SQL responses are usually a few hundred
# tokens of JSON. Override via ``MCPG_NL2SQL_MAX_TOKENS``.
DEFAULT_MAX_TOKENS = 2048

# Hard upper bound — refuse calls above this even if the env asked
# for more, so a misconfiguration can't surprise-bill the operator.
HARD_MAX_TOKENS = 16_384

# Default request timeout. Most NL→SQL completions finish in under
# 30s; pad for slow networks / slower models.
DEFAULT_TIMEOUT_SECONDS = 60.0

_SUPPORTED_PROVIDERS = frozenset({"anthropic", "openai", "gemini"})

# Schema-brief sizing — bounded so the prompt doesn't explode on large
# schemas. The agent can always paginate by passing a specific
# ``table_filter`` to focus on a subset.
DEFAULT_MAX_TABLES_IN_BRIEF = 30
DEFAULT_COLUMNS_PER_TABLE = 60


class NL2SQLError(Exception):
    """Raised when NL→SQL translation is rejected or fails."""


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """Result of :func:`translate_nl_to_sql`.

    ``sql`` is the generated query; empty when parsing failed.
    ``explanation`` is the model's natural-language rationale. When
    ``execute=True`` and the SQL passed the safety check, ``rows`` /
    ``columns`` / ``row_count`` are populated and ``executed`` is
    ``True``. On safety / execution failure, ``error`` carries the
    reason and the rest is empty.
    """

    sql: str
    explanation: str
    model: str
    provider: str
    executed: bool
    rows: list[dict[str, Any]]
    columns: list[str]
    row_count: int
    error: str | None


@runtime_checkable
class LLMProvider(Protocol):
    """Common interface for the NL→SQL chat completion call.

    Each provider's :meth:`complete` returns the raw text from the
    LLM. The caller does JSON parsing — that way provider-specific
    JSON-mode quirks don't leak into this module.
    """

    name: str

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        timeout: float,
    ) -> str:
        """Send the prompt; return the completion text. Raises on transport error."""
        ...


class AnthropicProvider:
    """Anthropic Messages API caller — `POST /v1/messages`."""

    name = "anthropic"

    def __init__(self, api_key: str, *, base_url: str = "https://api.anthropic.com") -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        timeout: float,
    ) -> str:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
        response.raise_for_status()
        body = response.json()
        # content is a list of blocks; for plain text we want the first
        # text block. JSON-mode isn't strictly supported, so we ask for
        # JSON in the prompt and trust the model to comply.
        for block in body.get("content", []):
            if block.get("type") == "text":
                return str(block.get("text", ""))
        return ""


class OpenAIProvider:
    """OpenAI / OpenAI-compatible chat completions caller.

    Works against ``api.openai.com`` by default; override
    ``base_url`` for self-hosted gateways (Ollama, vLLM, LM Studio,
    OpenRouter, Azure proxies).
    """

    name = "openai"

    def __init__(self, api_key: str, *, base_url: str = "https://api.openai.com/v1") -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        timeout: float,
    ) -> str:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "authorization": f"Bearer {self._api_key}",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices", [])
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content", ""))


class GeminiProvider:
    """Google Gemini generateContent caller."""

    name = "gemini"

    def __init__(self, api_key: str, *, base_url: str = "https://generativelanguage.googleapis.com") -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        timeout: float,
    ) -> str:
        # Gemini accepts the API key as a query string or `x-goog-api-key`
        # header — we use the header to avoid logging-route leakage.
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1beta/models/{model}:generateContent",
                headers={
                    "x-goog-api-key": self._api_key,
                    "content-type": "application/json",
                },
                json={
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": max_tokens,
                        "responseMimeType": "application/json",
                    },
                },
            )
        response.raise_for_status()
        body = response.json()
        candidates = body.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(str(p.get("text", "")) for p in parts)


def is_valid_provider(name: str) -> bool:
    """Return ``True`` for any name :func:`build_provider` will accept."""
    return name in _SUPPORTED_PROVIDERS


def build_provider(
    name: str,
    api_key: str,
    *,
    base_url: str | None = None,
) -> LLMProvider:
    """Construct the provider matching ``name``.

    Raises:
        NL2SQLError: When ``name`` is not in :data:`_SUPPORTED_PROVIDERS`.
    """
    if name == "anthropic":
        return AnthropicProvider(api_key=api_key, **({"base_url": base_url} if base_url else {}))
    if name == "openai":
        return OpenAIProvider(api_key=api_key, **({"base_url": base_url} if base_url else {}))
    if name == "gemini":
        return GeminiProvider(api_key=api_key, **({"base_url": base_url} if base_url else {}))
    raise NL2SQLError(f"unknown NL→SQL provider {name!r}; supported: {sorted(_SUPPORTED_PROVIDERS)}")


_SYSTEM_PROMPT = """You are a PostgreSQL expert helping an automation agent.

Your job: translate the user's natural-language question into ONE read-only PostgreSQL query.

Hard rules:
- The query MUST be a single SELECT (or WITH ... SELECT).
  No INSERT / UPDATE / DELETE / DDL / DCL / multi-statement input.
- Always qualify table names with their schema.
- Prefer explicit column lists over SELECT *.
- Add a LIMIT when the result could be large.
- Inline literals directly into the SQL so it runs as-is — do NOT
  emit $1 / $2 / %s placeholders.

Respond with strict JSON only:
{"sql": "SELECT ... FROM schema.table ...", "explanation": "what the query does in one or two sentences"}

If the question cannot be answered with the given schema, return:
{"sql": "", "explanation": "why this can't be answered"}"""


_USER_PROMPT_TEMPLATE = """Schema name: {schema}

Tables (truncated to {max_tables}):
{schema_brief}

User question:
{question}

Return JSON with `sql` and `explanation`. Inline literals — do NOT use $1 / $2 placeholders."""


async def _build_schema_brief(
    driver: SqlDriver,
    schema: str,
    *,
    max_tables: int,
    columns_per_table: int,
    table_filter: tuple[str, ...] | None,
) -> str:
    """Produce a compact text description of ``schema`` for the prompt.

    Lists tables with their columns (type + nullability) and the FKs
    between them. Bounded so the prompt stays small on big schemas.
    """
    tables = await list_tables(driver, schema)
    filter_set = set(table_filter) if table_filter else None
    relevant = [t for t in tables if filter_set is None or t.name in filter_set]
    if not relevant:
        return f"(no tables match filter in schema {schema!r})"
    bounded = relevant[:max_tables]

    lines: list[str] = []
    for table in bounded:
        lines.append(f"- {schema}.{table.name} ({table.type.lower()}):")
        columns = await describe_table(driver, schema, table.name)
        for col in columns[:columns_per_table]:
            nullable = "" if not col.nullable else " NULL"
            default = f" DEFAULT {col.default}" if col.default else ""
            lines.append(f"    * {col.name}: {col.data_type}{nullable}{default}")
        if len(columns) > columns_per_table:
            lines.append(f"    * ... +{len(columns) - columns_per_table} more columns")

    if filter_set is None and len(relevant) > max_tables:
        lines.append(f"... +{len(relevant) - max_tables} more tables not shown")

    foreign_keys = await list_foreign_keys(driver, schema)
    if foreign_keys:
        lines.append("")
        lines.append("Foreign keys:")
        bounded_names = {t.name for t in bounded}
        for fk in foreign_keys:
            if fk.from_table not in bounded_names:
                continue
            lines.append(
                f"- {schema}.{fk.from_table}({','.join(fk.from_columns)}) "
                f"-> {fk.to_schema}.{fk.to_table}({','.join(fk.to_columns)})"
            )

    return "\n".join(lines)


# Strip Markdown code fences the model might emit around the JSON
# despite being told to return JSON only. Greedy enough to handle both
# ```json and ```sql variants and trim either side.
_CODE_FENCE = re.compile(r"^\s*```(?:json|sql)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_response(raw: str) -> tuple[str, str]:
    """Return ``(sql, explanation)`` parsed from a provider's text reply.

    Tolerates a leading / trailing code fence and falls back to
    treating the whole response as ``explanation`` (with empty SQL)
    when the JSON shape isn't recognised — so the agent always sees
    *something*.
    """
    stripped = _CODE_FENCE.sub("", raw).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return ("", raw.strip())
    # json.loads succeeds for non-dict shapes (lists, raw strings,
    # numbers); .get on those crashes. Treat anything non-dict as a
    # parse failure so the raw text reaches the agent unchanged.
    if not isinstance(parsed, dict):
        return ("", raw.strip())
    sql = str(parsed.get("sql", "")).strip()
    explanation = str(parsed.get("explanation", "")).strip()
    return (sql, explanation)


async def translate_nl_to_sql(
    driver: SqlDriver,
    *,
    provider: LLMProvider,
    model: str,
    question: str,
    schema: str,
    execute: bool = False,
    table_filter: tuple[str, ...] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_tables_in_brief: int = DEFAULT_MAX_TABLES_IN_BRIEF,
    columns_per_table: int = DEFAULT_COLUMNS_PER_TABLE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> TranslationResult:
    """Translate ``question`` into a SQL query against ``schema``.

    Args:
        provider: A constructed :class:`LLMProvider` instance.
        model: Vendor-specific model id (e.g. ``claude-sonnet-4-6``).
        execute: When ``True``, the generated SQL is validated by
            ``SafeSqlDriver`` and run via :func:`mcpg.query.run_select`.
            Writes / DDL / multi-statement input are rejected.
        table_filter: Optional restrict-list of table names; the
            schema brief omits everything else. Useful when the user's
            question is clearly about a known subset.
        max_tokens / max_rows / max_tables_in_brief / columns_per_table:
            Bounds on the prompt and the executed result. The hard
            cap on ``max_tokens`` is :data:`HARD_MAX_TOKENS`.

    Raises:
        NL2SQLError: When inputs fail validation or the model returns
            something fundamentally unusable.
    """
    if not question.strip():
        raise NL2SQLError("question must not be empty")
    if max_tokens < 1 or max_tokens > HARD_MAX_TOKENS:
        raise NL2SQLError(f"max_tokens must be between 1 and {HARD_MAX_TOKENS}")

    schema_brief = await _build_schema_brief(
        driver,
        schema,
        max_tables=max_tables_in_brief,
        columns_per_table=columns_per_table,
        table_filter=table_filter,
    )
    # ``.replace`` (not ``.format``) so curly braces in the user's
    # question (e.g. asking about a jsonb literal like ``{"k":1}``)
    # don't get interpreted as format placeholders and crash with
    # KeyError. The placeholders below are fixed strings; no need
    # for str.format's escape semantics.
    user_prompt = (
        _USER_PROMPT_TEMPLATE.replace("{schema}", schema)
        .replace("{max_tables}", str(max_tables_in_brief))
        .replace("{schema_brief}", schema_brief)
        .replace("{question}", question.strip())
    )

    try:
        raw = await provider.complete(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise NL2SQLError(f"NL→SQL provider request failed: {exc}") from exc

    sql, explanation = _parse_response(raw)

    if not execute or not sql:
        return TranslationResult(
            sql=sql,
            explanation=explanation,
            model=model,
            provider=provider.name,
            executed=False,
            rows=[],
            columns=[],
            row_count=0,
            error=None if sql else "model returned no SQL",
        )

    # Execution path: route through the same safety stack as run_select.
    try:
        result = await run_select(driver, sql, max_rows=max_rows)
    except QueryError as exc:
        return TranslationResult(
            sql=sql,
            explanation=explanation,
            model=model,
            provider=provider.name,
            executed=False,
            rows=[],
            columns=[],
            row_count=0,
            error=str(exc),
        )

    return TranslationResult(
        sql=sql,
        explanation=explanation,
        model=model,
        provider=provider.name,
        executed=True,
        rows=result.rows,
        columns=result.columns,
        row_count=result.row_count,
        error=None,
    )
