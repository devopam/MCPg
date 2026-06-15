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
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from mcpg._vendor.sql import SqlDriver
from mcpg.introspection import describe_table, list_foreign_keys, list_tables
from mcpg.query import DEFAULT_MAX_ROWS, QueryError, run_select

if TYPE_CHECKING:
    from mcpg.config import Settings

logger = logging.getLogger(__name__)

# Default models per provider — chosen for low cost / high availability
# at writing time. Override via ``MCPG_NL2SQL_MODEL``.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
}

# Human-readable env-var hint per provider, used in error messages
# when MCPg needs to tell the operator which env var to set to
# enable a given provider. Single source of truth so the wording
# stays consistent between startup validation (config.py) and
# runtime tool errors (tools.py).
VENDOR_ENV_VAR_HINT: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY (or GOOGLE_API_KEY)",
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

# Plain unquoted PostgreSQL identifier — matches the rule every other
# surface in the codebase uses (pg_search, turboquant, rag_efficiency,
# locks, …). Anything that would require delimited quoting is refused
# here rather than parsed out of an LLM-facing string.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Default deny-list of schemas that NL→SQL must never touch — operator
# internals and PG system schemas. The LLM can be tricked into emitting
# queries against these; a strict default keeps the blast radius
# bounded even when the prompt-injection guard fails. Operators add to
# this via MCPG_NL2SQL_SCHEMA_DENYLIST; a non-empty
# MCPG_NL2SQL_SCHEMA_ALLOWLIST flips the policy to allowlist-only.
DEFAULT_SCHEMA_DENYLIST: frozenset[str] = frozenset(
    name.lower()
    for name in (
        "pg_catalog",
        "pg_toast",
        "information_schema",
        "mcpg_audit",
        "mcpg_rag",
        "mcpg_migrations",
    )
)

# Cap on the rendered ``DEFAULT <expr>`` text per column in the schema
# brief. Long defaults are usually generated SQL expressions
# (``COALESCE(... CASE WHEN ... THEN ... END)``) and are mostly noise
# for the LLM; an attacker with prior CREATE TABLE access can plant a
# multi-kilobyte default that overrides the system prompt for every
# subsequent NL→SQL call (stored prompt injection — deep-review
# nl2sql audit P0 #1). Bound at 80 chars and strip newlines so
# legitimate defaults (``now()``, ``gen_random_uuid()``,
# ``'pending'::text``) still inform the LLM without the injection
# surface.
_DEFAULT_EXPR_MAX_CHARS = 80


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


@dataclass(frozen=True, slots=True)
class ProviderCallParams:
    """The fully-resolved set of inputs needed to call one provider.

    Produced by :func:`resolve_provider_call_params` so the tool
    wrapper layer in :mod:`mcpg.tools` doesn't carry provider-
    selection / API-key dispatch / model-override logic. The tool
    wrapper becomes a thin pass-through: build a provider with the
    constructor args here, then call :func:`translate_nl_to_sql`.
    """

    provider_name: str
    api_key: str
    model: str
    base_url: str | None


def resolve_provider_call_params(settings: Settings, requested_provider: str | None) -> ProviderCallParams:
    """Pick the provider for this call and resolve its full call shape.

    Threads three signals together:

    1. ``requested_provider`` from the caller (``provider=`` arg on
       ``translate_nl_to_sql``) — highest precedence.
    2. ``settings.nl2sql_provider`` (``MCPG_NL2SQL_PROVIDER``) — the
       operator-configured default.
    3. Whatever vendor keys are present in the operator-supplied
       credentials. If neither (1) nor (2) is set, fail fast — this
       is a configuration issue the caller can't solve.

    Overrides for ``model`` / ``base_url`` only apply when the chosen
    provider IS the configured default. Forwarding an Anthropic-shaped
    model id to an OpenAI call (or vice versa) would just break, so
    non-default calls always use the upstream default model + no
    base_url override.
    """
    api_keys = dict(settings.nl2sql_api_keys)
    # Normalize each candidate *individually* and only then pick the
    # first non-empty one. A bare ``(a or b or "").strip().lower()``
    # would let a whitespace-only ``requested_provider`` short-circuit
    # the chain (``"   "`` is truthy) and bury a perfectly-valid
    # operator default behind a misleading "no provider configured"
    # error (gemini review on #102).
    requested_norm = (requested_provider or "").strip().lower() or None
    default_norm = (settings.nl2sql_provider or "").strip().lower() or None
    chosen = requested_norm or default_norm
    if chosen is None:
        # No provider arg AND no default configured AND no vendor keys
        # in the env — provider= alone can't fix this, the operator
        # needs to set at least one vendor API key.
        raise NL2SQLError(
            "translate_nl_to_sql has no provider configured. Set at "
            "least one of ANTHROPIC_API_KEY / OPENAI_API_KEY / "
            "GEMINI_API_KEY (or GOOGLE_API_KEY) in the server's "
            "environment. The tool's provider= argument selects "
            "between providers that are already configured — it can't "
            "supply credentials on its own."
        )
    if not is_valid_provider(chosen):
        raise NL2SQLError(f"unknown NL→SQL provider {chosen!r}; supported: anthropic, openai, gemini")
    api_key = api_keys.get(chosen)
    if api_key is None:
        configured = sorted(api_keys) or ["(none)"]
        raise NL2SQLError(
            f"provider {chosen!r} is not configured (currently configured: "
            f"{', '.join(configured)}). Set {VENDOR_ENV_VAR_HINT[chosen]} "
            "in the environment, or pick a configured provider via the "
            "provider= argument."
        )

    # Compare against the normalized form of the configured default —
    # ``chosen`` is already strip()ped + lower()ed and
    # ``settings.nl2sql_provider`` can carry mixed casing or stray
    # whitespace from the env. Without normalization here, a default
    # like ``"Anthropic\n"`` would silently disable the operator's
    # ``MCPG_NL2SQL_MODEL`` / ``MCPG_NL2SQL_BASE_URL`` overrides and
    # route traffic at the public endpoint — gemini review on #102
    # called this out as security-critical (the base_url path is
    # often a private proxy / regional gateway).
    is_default = chosen == default_norm
    model = settings.nl2sql_model if (is_default and settings.nl2sql_model) else DEFAULT_MODELS[chosen]
    base_url = settings.nl2sql_base_url if is_default else None
    return ProviderCallParams(
        provider_name=chosen,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


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


def _sanitize_default_expr(value: str | None) -> str | None:
    """Make a column-DEFAULT expression safe to interpolate into an LLM prompt.

    The DDL value reaches us via ``pg_get_expr(adbin, adrelid)``, so an
    attacker who can ``CREATE TABLE`` (or ``ALTER COLUMN … SET DEFAULT``)
    in any schema reachable by NL→SQL can plant a default whose text
    contains line breaks + adversarial instructions. The legacy brief
    rendered that text verbatim — every subsequent ``translate_nl_to_sql``
    call against the same schema would carry the injection in its
    user prompt.

    This helper strips control characters, collapses whitespace, and
    caps the result so injected payloads can't override the rest of
    the brief. Returns ``None`` when the input is empty after the
    scrub so callers can omit the ``DEFAULT`` clause entirely on
    empty / all-control-char inputs.
    """
    if value is None:
        return None
    # Strip ASCII control chars + collapse runs of whitespace into a
    # single space. The control-char filter catches ``\n`` / ``\r`` /
    # ``\t`` (the classic prompt-break payload) plus any other C0/C1
    # bytes a creative attacker might try.
    flattened = "".join(ch if ch.isprintable() else " " for ch in value)
    collapsed = " ".join(flattened.split())
    if not collapsed:
        return None
    if len(collapsed) <= _DEFAULT_EXPR_MAX_CHARS:
        return collapsed
    return collapsed[: _DEFAULT_EXPR_MAX_CHARS - 1] + "…"


def _resolve_schema_policy(
    env: Mapping[str, str] | None = None,
) -> tuple[frozenset[str], frozenset[str] | None]:
    """Build the (denylist, allowlist) tuple from settings/env.

    Allowlist semantics — when set, only schemas in the allowlist are
    reachable; the denylist is ignored. When the allowlist is unset
    (the typical deployment), every schema is reachable except those
    explicitly denied. Each value is whitespace-trimmed and lowered;
    empty entries are dropped.
    """
    source = env if env is not None else os.environ
    extra_deny = (source.get("MCPG_NL2SQL_SCHEMA_DENYLIST") or "").strip()
    allow_raw = (source.get("MCPG_NL2SQL_SCHEMA_ALLOWLIST") or "").strip()

    denylist = set(DEFAULT_SCHEMA_DENYLIST)
    if extra_deny:
        denylist.update(item.strip().lower() for item in extra_deny.split(",") if item.strip())
    allowlist: frozenset[str] | None = None
    if allow_raw:
        allowlist = frozenset(item.strip().lower() for item in allow_raw.split(",") if item.strip())
    return frozenset(denylist), allowlist


def _validate_schema_name(schema: str, *, env: Mapping[str, str] | None = None) -> str:
    """Validate ``schema`` as an identifier and enforce deny/allow policy.

    Returns the validated (lowercased, normalised) schema name. Raises
    :class:`NL2SQLError` on:

    * a non-identifier value (e.g. ``"public; --"``) — the prompt-
      injection vector flagged in the deep-review nl2sql audit P0 #2.
    * a schema present in the deny-list (default catches PG system
      schemas + MCPg-internal schemas).
    * a non-allowlist schema when ``MCPG_NL2SQL_SCHEMA_ALLOWLIST`` is
      set (strict-mode deployments).
    """
    if not isinstance(schema, str) or not schema.strip():
        raise NL2SQLError("schema must be a non-empty identifier string")
    candidate = schema.strip()
    if not _IDENTIFIER.match(candidate):
        raise NL2SQLError(
            f"schema {schema!r} is not a valid SQL identifier — must match {_IDENTIFIER.pattern}. "
            "NL→SQL refuses schemas that would require delimited quoting to keep prompt-injection "
            "via the schema name out of scope."
        )
    normalised = candidate.lower()
    denylist, allowlist = _resolve_schema_policy(env)
    if allowlist is not None and normalised not in allowlist:
        # Surface only the offending schema name — the full allowlist is
        # operator configuration and shouldn't leak through a caller-
        # facing error (sourcery review, PR #106).
        raise NL2SQLError(
            f"schema {schema!r} is not permitted by MCPG_NL2SQL_SCHEMA_ALLOWLIST; NL→SQL refuses to query it."
        )
    if normalised in denylist:
        raise NL2SQLError(
            f"schema {schema!r} is on the NL→SQL deny-list; pick a non-system schema "
            "or amend MCPG_NL2SQL_SCHEMA_DENYLIST if you really mean to expose it."
        )
    return normalised


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
            # ``col.default`` reaches us via ``pg_get_expr(adbin, adrelid)``;
            # _sanitize_default_expr strips control chars + caps length
            # so an attacker-planted DEFAULT can't break out of the
            # schema brief and override the system prompt (deep-review
            # nl2sql audit P0 #1, "stored prompt injection via DEFAULT").
            safe_default = _sanitize_default_expr(col.default)
            default = f" DEFAULT {safe_default}" if safe_default else ""
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
    env: Mapping[str, str] | None = None,
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
        env: Optional configuration mapping. When omitted, the
            ``MCPG_NL2SQL_SCHEMA_*`` settings are read from
            ``os.environ``. Pass a custom mapping for multi-tenant or
            test scenarios where the global env shouldn't drive policy.

    Raises:
        NL2SQLError: When inputs fail validation or the model returns
            something fundamentally unusable.
    """
    if not question.strip():
        raise NL2SQLError("question must not be empty")
    if max_tokens < 1 or max_tokens > HARD_MAX_TOKENS:
        raise NL2SQLError(f"max_tokens must be between 1 and {HARD_MAX_TOKENS}")
    # _validate_schema_name does three things in one place:
    #   1. Rejects non-identifier values (the schema name lands in the
    #      LLM prompt verbatim — caller-side prompt injection vector).
    #   2. Enforces MCPG_NL2SQL_SCHEMA_DENYLIST (operator-internals
    #      and PG system schemas off by default).
    #   3. Enforces MCPG_NL2SQL_SCHEMA_ALLOWLIST when set (strict
    #      deployments lock NL→SQL to one or two schemas).
    schema = _validate_schema_name(schema, env=env)

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
