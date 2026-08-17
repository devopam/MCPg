"""Tests for the dynamic session-intent runtime layer (roadmap 22)."""

from __future__ import annotations

import pytest
from mcp_types import ListToolsResult, Tool

from mcpg.dynamic_session_intent import (
    STDIO_SESSION_KEY,
    DynamicIntentError,
    DynamicSessionIntentMiddleware,
    enable_intent,
    enabled_intents,
    session_key_from_headers,
    visible_tool_names,
)

# ---------------------------------------------------------------------------
# session_key_from_headers
# ---------------------------------------------------------------------------


def test_session_key_from_headers_none_uses_stdio_sentinel() -> None:
    assert session_key_from_headers(None) == STDIO_SESSION_KEY


def test_session_key_from_headers_missing_header_uses_stdio_sentinel() -> None:
    assert session_key_from_headers({}) == STDIO_SESSION_KEY


def test_session_key_from_headers_reads_mcp_session_id() -> None:
    assert session_key_from_headers({"mcp-session-id": "abc123"}) == "abc123"


# ---------------------------------------------------------------------------
# enable_intent / enabled_intents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enabled_intents_defaults_when_nothing_enabled() -> None:
    assert enabled_intents("new-session-key-1", default_intent=("core",)) == frozenset({"core"})


@pytest.mark.asyncio
async def test_enable_intent_then_enabled_intents_reflects_it() -> None:
    session_key = "session-2"
    await enable_intent(session_key, "vector_rag")
    assert enabled_intents(session_key, default_intent=("core",)) == frozenset({"vector_rag"})


@pytest.mark.asyncio
async def test_enable_intent_accumulates_multiple_calls() -> None:
    session_key = "session-3"
    await enable_intent(session_key, "vector_rag")
    await enable_intent(session_key, "monitor")
    assert enabled_intents(session_key, default_intent=("core",)) == frozenset({"vector_rag", "monitor"})


@pytest.mark.asyncio
async def test_enable_intent_is_idempotent() -> None:
    session_key = "session-4"
    await enable_intent(session_key, "core")
    await enable_intent(session_key, "core")
    assert enabled_intents(session_key, default_intent=("lookup",)) == frozenset({"core"})


@pytest.mark.asyncio
async def test_enable_intent_accepts_a_raw_bucket_id() -> None:
    session_key = "session-5"
    # "cache_and_foreign_data" is a real bucket id from mcpg.about.BUCKET_IDS
    # not covered by any INTENT_PRESETS entry — verified empirically:
    # BUCKET_IDS - set().union(*INTENT_PRESETS.values()) includes it.
    await enable_intent(session_key, "cache_and_foreign_data")
    assert "cache_and_foreign_data" in enabled_intents(session_key, default_intent=("core",))


@pytest.mark.asyncio
async def test_enable_intent_rejects_unknown_name() -> None:
    with pytest.raises(DynamicIntentError):
        await enable_intent("session-6", "not_a_real_preset_or_bucket")


@pytest.mark.asyncio
async def test_enable_intent_rejects_blank_name() -> None:
    with pytest.raises(DynamicIntentError):
        await enable_intent("session-7", "   ")


# ---------------------------------------------------------------------------
# enable_intent_and_notify — the notify callback Task 7's tool wraps around
# ctx.session.send_tool_list_changed; tested here with a fake so it doesn't
# need a live MCP session.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_intent_and_notify_calls_notify_once_after_enabling() -> None:
    from mcpg.dynamic_session_intent import enable_intent_and_notify

    calls: list[int] = []

    async def fake_notify() -> None:
        calls.append(1)

    await enable_intent_and_notify("session-notify-1", "core", notify=fake_notify)
    assert enabled_intents("session-notify-1", default_intent=()) == frozenset({"core"})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_enable_intent_and_notify_does_not_notify_on_error() -> None:
    from mcpg.dynamic_session_intent import enable_intent_and_notify

    calls: list[int] = []

    async def fake_notify() -> None:
        calls.append(1)

    with pytest.raises(DynamicIntentError):
        await enable_intent_and_notify("session-notify-2", "not_a_real_preset", notify=fake_notify)
    assert calls == []


@pytest.mark.asyncio
async def test_sessions_are_isolated() -> None:
    """The concurrency/isolation property this whole feature exists for."""
    await enable_intent("session-A", "vector_rag")
    await enable_intent("session-B", "monitor")
    assert enabled_intents("session-A", default_intent=("core",)) == frozenset({"vector_rag"})
    assert enabled_intents("session-B", default_intent=("core",)) == frozenset({"monitor"})


# ---------------------------------------------------------------------------
# visible_tool_names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_visible_tool_names_new_session_gets_default_intent() -> None:
    registered = frozenset(
        {"list_tables", "run_select", "run_ddl", "list_pending_migrations", "describe_self", "describe_tool"}
    )
    visible = visible_tool_names("session-8", default_intent=("core",), registered=registered)
    # "core" resolves to the headline tools of schema_introspection + query_execution
    # (see mcpg.session_intent._TOOL_NAME_PRESETS) — that includes list_tables,
    # run_select, AND run_ddl (run_ddl is a headline tool of query_execution;
    # verified via test_core_preset_is_headline_tools_of_schema_and_query_buckets).
    # It does NOT include tools from other buckets, e.g. migrations'
    # list_pending_migrations.
    assert "list_tables" in visible
    assert "run_select" in visible
    assert "run_ddl" in visible
    assert "describe_self" in visible  # ALWAYS_KEEP
    assert "list_pending_migrations" not in visible


@pytest.mark.asyncio
async def test_visible_tool_names_grows_after_enable_intent() -> None:
    session_key = "session-9"
    # "list_pending_migrations" is a headline tool of the "migrations" bucket,
    # which "core" (schema_introspection + query_execution only) never covers —
    # unlike run_ddl, which is already visible under "core" (see the previous
    # test), so it can't demonstrate growth.
    registered = frozenset({"list_tables", "list_pending_migrations", "describe_self", "describe_tool"})
    before = visible_tool_names(session_key, default_intent=("core",), registered=registered)
    assert "list_pending_migrations" not in before

    await enable_intent(session_key, "migration")  # migration includes the migrations bucket
    after = visible_tool_names(session_key, default_intent=("core",), registered=registered)
    assert "list_pending_migrations" in after


@pytest.mark.asyncio
async def test_visible_tool_names_never_exceeds_registered() -> None:
    """The Layer 1/Layer 2 ceiling: visible_tool_names must intersect with
    whatever the caller says is actually registered, even if a resolved
    intent would otherwise include more."""
    session_key = "session-10"
    await enable_intent(session_key, "vector_rag")
    # Simulate a Layer-1-narrowed registry that never had vector tools.
    registered = frozenset({"list_tables", "describe_self", "describe_tool"})
    visible = visible_tool_names(session_key, default_intent=("core",), registered=registered)
    assert visible <= registered


# ---------------------------------------------------------------------------
# DynamicSessionIntentMiddleware
# ---------------------------------------------------------------------------


def _tool(name: str) -> Tool:
    """A minimal real `Tool` instance -- `ListToolsResult.tools` is a
    pydantic-validated `list[Tool]` field, so a plain fake object with
    just a `.name` attribute would fail construction (`input_schema`
    is a required field with no default). This gives every field
    pydantic actually requires and nothing more."""
    return Tool(name=name, input_schema={"type": "object", "properties": {}})


class _FakeSettings:
    def __init__(self, session_intent: tuple[str, ...] = ()) -> None:
        self.session_intent = session_intent


class _FakeLifespanContext:
    def __init__(self, settings: _FakeSettings) -> None:
        self.settings = settings


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None) -> None:
        self.headers = headers


class _FakeCtx:
    def __init__(
        self,
        *,
        method: str,
        request: object | None,
        settings: _FakeSettings | None = None,
    ) -> None:
        self.method = method
        self.request = request
        self.lifespan_context = _FakeLifespanContext(settings or _FakeSettings())


@pytest.mark.asyncio
async def test_middleware_passes_through_non_tools_list_requests() -> None:
    middleware = DynamicSessionIntentMiddleware()
    ctx = _FakeCtx(method="tools/call", request=_FakeRequest({"mcp-session-id": "s1"}))

    async def call_next(_ctx: object) -> dict[str, str]:
        return {"untouched": "yes"}

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]
    assert result == {"untouched": "yes"}


@pytest.mark.asyncio
async def test_middleware_filters_tools_list_to_default_intent() -> None:
    middleware = DynamicSessionIntentMiddleware()
    ctx = _FakeCtx(method="tools/list", request=_FakeRequest({"mcp-session-id": "s2"}))

    async def call_next(_ctx: object) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                _tool("list_tables"),
                # list_pending_migrations, not run_ddl: run_ddl IS one of
                # core's 12 declared headline names (query_execution),
                # so it would survive this filter -- it's only excluded
                # from a REAL server's registered surface by a separate,
                # access-mode-based gate (Layer 1's Capability checks,
                # not simulated by this fake call_next). Caught during
                # Task 5: the plan's earlier draft assumed core excludes
                # run_ddl outright, which is false.
                _tool("list_pending_migrations"),
                _tool("describe_self"),
            ]
        )

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]
    assert isinstance(result, ListToolsResult)
    names = {t.name for t in result.tools}
    # Exact set, not just presence/absence -- "no more, no less" (spec section 6).
    assert names == {"list_tables", "describe_self"}


@pytest.mark.asyncio
async def test_middleware_uses_configured_static_intent_as_default() -> None:
    middleware = DynamicSessionIntentMiddleware()
    ctx = _FakeCtx(
        method="tools/list",
        request=_FakeRequest({"mcp-session-id": "s3"}),
        settings=_FakeSettings(session_intent=("monitor",)),
    )

    async def call_next(_ctx: object) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                _tool("list_active_queries"),  # operations_and_health -> in monitor
                _tool("list_tables"),  # schema_introspection -> NOT in monitor
                _tool("describe_self"),
            ]
        )

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]
    names = {t.name for t in result.tools}  # type: ignore[union-attr]
    assert "list_active_queries" in names
    assert "list_tables" not in names
    assert "describe_self" in names


@pytest.mark.asyncio
async def test_middleware_is_isolated_per_session() -> None:
    from mcpg.dynamic_session_intent import enable_intent

    middleware = DynamicSessionIntentMiddleware()
    await enable_intent("s4-A", "monitor")

    async def call_next(_ctx: object) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                _tool("list_active_queries"),
                _tool("list_tables"),
            ]
        )

    ctx_a = _FakeCtx(method="tools/list", request=_FakeRequest({"mcp-session-id": "s4-A"}))
    ctx_b = _FakeCtx(method="tools/list", request=_FakeRequest({"mcp-session-id": "s4-B"}))

    result_a = await middleware(ctx_a, call_next)  # type: ignore[arg-type]
    result_b = await middleware(ctx_b, call_next)  # type: ignore[arg-type]

    names_a = {t.name for t in result_a.tools}  # type: ignore[union-attr]
    names_b = {t.name for t in result_b.tools}  # type: ignore[union-attr]
    assert "list_active_queries" in names_a  # session A enabled monitor
    assert "list_active_queries" not in names_b  # session B never did


@pytest.mark.asyncio
async def test_middleware_noop_on_stdio_where_request_is_none() -> None:
    middleware = DynamicSessionIntentMiddleware()
    ctx = _FakeCtx(method="tools/list", request=None)

    async def call_next(_ctx: object) -> ListToolsResult:
        # list_pending_migrations, not run_ddl -- see the comment in
        # test_middleware_filters_tools_list_to_default_intent above:
        # run_ddl is genuinely part of core's declared tool_names.
        return ListToolsResult(tools=[_tool("list_tables"), _tool("list_pending_migrations")])

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]
    names = {t.name for t in result.tools}  # type: ignore[union-attr]
    assert "list_tables" in names
    assert "list_pending_migrations" not in names  # still filtered to core -- stdio just uses the sentinel session key
