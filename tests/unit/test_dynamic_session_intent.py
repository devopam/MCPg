"""Tests for the dynamic session-intent runtime layer (roadmap 22)."""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver
from _mcp_test_helpers import create_connected_server_and_client_session

import mcpg.dynamic_session_intent as dynamic_session_intent
from mcpg.config import load_settings
from mcpg.dynamic_session_intent import (
    STDIO_SESSION_KEY,
    DynamicIntentError,
    DynamicSessionIntentMiddleware,
    enable_intent,
    enabled_intents,
    session_key_from_headers,
    visible_tool_names,
)
from mcpg.server import create_server
from mcpg.session_intent import ALWAYS_KEEP


@pytest.fixture(autouse=True)
def _reset_dynamic_intent_state() -> Any:
    """Every test in this module shares the module-level `_session_intents`
    dict. Most tests use a unique session key per test so collisions are
    unlikely, but the end-to-end tests below all dispatch through stdio
    (`request=None`), which collapses to the single `STDIO_SESSION_KEY`
    sentinel -- so they'd silently accumulate state across each other (and
    across whatever test order pytest picks) without this. Clearing before
    and after every test makes the module's test isolation an explicit
    guarantee rather than an accident of key naming or file order."""
    dynamic_session_intent._session_intents.clear()
    yield
    dynamic_session_intent._session_intents.clear()


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
    """`enabled_intents` is a union with `default_intent`, not a replace --
    "sessions only grow their visible surface" per the design spec, not
    "sessions swap their surface for whatever they last enabled". Enabling
    `vector_rag` must not cost the session its starting `core` default."""
    session_key = "session-2"
    await enable_intent(session_key, "vector_rag")
    assert enabled_intents(session_key, default_intent=("core",)) == frozenset({"vector_rag", "core"})


@pytest.mark.asyncio
async def test_enable_intent_accumulates_multiple_calls() -> None:
    session_key = "session-3"
    await enable_intent(session_key, "vector_rag")
    await enable_intent(session_key, "monitor")
    assert enabled_intents(session_key, default_intent=("core",)) == frozenset({"vector_rag", "monitor", "core"})


@pytest.mark.asyncio
async def test_enable_intent_is_idempotent() -> None:
    session_key = "session-4"
    await enable_intent(session_key, "core")
    await enable_intent(session_key, "core")
    assert enabled_intents(session_key, default_intent=("lookup",)) == frozenset({"core", "lookup"})


@pytest.mark.asyncio
async def test_growth_from_default_intent_is_additive_not_a_replace() -> None:
    """Regression test for a bug caught in a second final-review pass on
    top of roadmap 22: `enabled_intents` must UNION whatever a session
    explicitly enables with its starting `default_intent`, never drop the
    default the moment anything else gets enabled. "Sessions only grow
    their visible surface, no disable in v1" (the design spec, and
    `enable_session_intent`'s own tool description) is a real guarantee
    -- a session that starts implicit at `core` and then enables one more
    preset must never lose tools it already had.

    Uses `monitor` deliberately, not `vector_rag`: `vector_rag`'s buckets
    (`schema_introspection` / `query_execution` / ...) happen to overlap
    `core`'s own headline-tool buckets, so a session that lost its
    `default_intent` entirely would still *look* like it grew when probed
    with `vector_rag` -- the tools survive via bucket overlap, not because
    the default was actually preserved. `monitor`'s buckets
    (`operations_and_health` / `advisors` / `observability` /
    `audit_trail`) have zero overlap with `core`, so this test can't be
    fooled the same way."""
    session_key = "session-growth-1"
    default_intent = ("core",)
    assert enabled_intents(session_key, default_intent=default_intent) == frozenset({"core"})

    await enable_intent(session_key, "monitor")

    enabled = enabled_intents(session_key, default_intent=default_intent)
    assert "core" in enabled, "the implicit starting default must survive enabling something else"
    assert "monitor" in enabled
    assert enabled == frozenset({"core", "monitor"})


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
    assert enabled_intents("session-A", default_intent=("core",)) == frozenset({"vector_rag", "core"})
    assert enabled_intents("session-B", default_intent=("core",)) == frozenset({"monitor", "core"})


# ---------------------------------------------------------------------------
# _session_intents bounded LRU (final-review Finding 4) -- the design spec
# (§4) anticipated the SDK giving no clean session-teardown signal and
# specified a bounded LRU/TTL fallback so a long-lived streamable-http
# deployment doesn't leak memory one entry per distinct session forever.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_intents_state_is_capped_evicting_oldest_first() -> None:
    cap = dynamic_session_intent._MAX_SESSION_INTENTS_ENTRIES
    try:
        dynamic_session_intent._MAX_SESSION_INTENTS_ENTRIES = 5
        for i in range(5):
            await enable_intent(f"lru-session-{i}", "core")
        assert len(dynamic_session_intent._session_intents) == 5

        # One more distinct session pushes past the cap -- the LEAST
        # recently touched entry ("lru-session-0") must be evicted, not an
        # arbitrary or newest one.
        await enable_intent("lru-session-5", "core")
        assert len(dynamic_session_intent._session_intents) == 5
        assert "lru-session-0" not in dynamic_session_intent._session_intents
        assert "lru-session-5" in dynamic_session_intent._session_intents
        for i in range(1, 5):
            assert f"lru-session-{i}" in dynamic_session_intent._session_intents

        # A subsequent enable_intent call for an already-tracked session
        # counts as a touch: it must not itself be evicted next, even
        # though it's numerically the "oldest" remaining key by insertion
        # order.
        await enable_intent("lru-session-1", "monitor")
        await enable_intent("lru-session-6", "core")
        assert "lru-session-1" in dynamic_session_intent._session_intents  # touched, survived
        assert "lru-session-2" not in dynamic_session_intent._session_intents  # now the true LRU, evicted
    finally:
        dynamic_session_intent._MAX_SESSION_INTENTS_ENTRIES = cap


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
async def test_visible_tool_names_growth_never_drops_the_default_intent() -> None:
    """`visible_tool_names`-level counterpart to
    `test_growth_from_default_intent_is_additive_not_a_replace`: growth
    must be strictly additive at the actual function the middleware calls
    on every `tools/list`, not just at the `enabled_intents` layer beneath
    it. Uses `monitor` (zero bucket overlap with `core`) so a session that
    silently lost its `core` default couldn't hide behind bucket overlap
    the way `migration` (used in the test above, which shares
    `schema_introspection`/`query_execution` with `core`) could."""
    session_key = "session-growth-visible-1"
    registered = frozenset(
        {"list_tables", "run_select", "describe_self", "describe_tool", "list_active_queries", "run_advisors"}
    )
    before = visible_tool_names(session_key, default_intent=("core",), registered=registered)
    assert "list_tables" in before  # core headline tool
    assert "run_select" in before  # core headline tool
    assert "list_active_queries" not in before  # operations_and_health, not in core

    await enable_intent(session_key, "monitor")
    after = visible_tool_names(session_key, default_intent=("core",), registered=registered)

    assert before <= after, f"lost tools after enabling monitor: {sorted(before - after)}"
    assert "list_tables" in after  # still visible -- the core default must survive
    assert "run_select" in after  # still visible -- the core default must survive
    assert "list_active_queries" in after  # newly visible via monitor
    assert "run_advisors" in after  # newly visible via monitor (advisors bucket)


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


def _tool(name: str) -> dict[str, Any]:
    """A minimal wire-shaped tool dict -- what `call_next` actually hands
    the middleware for a `tools/list` request in production. By the time
    middleware sees it, `ServerRunner._serialize` has already dumped the
    handler's `ListToolsResult` into a plain `dict[str, Any]`; middleware
    never receives a `ListToolsResult` pydantic instance. See
    `DynamicSessionIntentMiddleware.__call__`'s comment."""
    return {"name": name, "inputSchema": {"type": "object", "properties": {}}}


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

    async def call_next(_ctx: object) -> dict[str, Any]:
        return {
            "tools": [
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
        }

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    names = {t["name"] for t in result["tools"]}
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

    async def call_next(_ctx: object) -> dict[str, Any]:
        return {
            "tools": [
                _tool("list_active_queries"),  # operations_and_health -> in monitor
                _tool("list_tables"),  # schema_introspection -> NOT in monitor
                _tool("describe_self"),
            ]
        }

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]
    names = {t["name"] for t in result["tools"]}
    assert "list_active_queries" in names
    assert "list_tables" not in names
    assert "describe_self" in names


@pytest.mark.asyncio
async def test_middleware_is_isolated_per_session() -> None:
    from mcpg.dynamic_session_intent import enable_intent

    middleware = DynamicSessionIntentMiddleware()
    await enable_intent("s4-A", "monitor")

    async def call_next(_ctx: object) -> dict[str, Any]:
        return {
            "tools": [
                _tool("list_active_queries"),
                _tool("list_tables"),
            ]
        }

    ctx_a = _FakeCtx(method="tools/list", request=_FakeRequest({"mcp-session-id": "s4-A"}))
    ctx_b = _FakeCtx(method="tools/list", request=_FakeRequest({"mcp-session-id": "s4-B"}))

    result_a = await middleware(ctx_a, call_next)  # type: ignore[arg-type]
    result_b = await middleware(ctx_b, call_next)  # type: ignore[arg-type]

    names_a = {t["name"] for t in result_a["tools"]}
    names_b = {t["name"] for t in result_b["tools"]}
    assert "list_active_queries" in names_a  # session A enabled monitor
    assert "list_active_queries" not in names_b  # session B never did


@pytest.mark.asyncio
async def test_middleware_noop_on_stdio_where_request_is_none() -> None:
    middleware = DynamicSessionIntentMiddleware()
    ctx = _FakeCtx(method="tools/list", request=None)

    async def call_next(_ctx: object) -> dict[str, Any]:
        # list_pending_migrations, not run_ddl -- see the comment in
        # test_middleware_filters_tools_list_to_default_intent above:
        # run_ddl is genuinely part of core's declared tool_names.
        return {"tools": [_tool("list_tables"), _tool("list_pending_migrations")]}

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]
    names = {t["name"] for t in result["tools"]}
    assert "list_tables" in names
    assert "list_pending_migrations" not in names  # still filtered to core -- stdio just uses the sentinel session key


# ---------------------------------------------------------------------------
# End-to-end regression test through REAL MCP dispatch (final-review Finding
# 1). Every test above exercises the middleware directly with a hand-built
# `call_next` -- exactly the shape that let the middleware's original
# `isinstance(result, ListToolsResult)` guard go unnoticed as unsatisfiable
# for 6+ reviews: those tests encode the bug's own (wrong) assumption about
# what `call_next` hands back. This test instead stands up a real
# `MCPServer`, drives it through the real dispatch stack (`ServerRunner`,
# which serializes `tools/list` to a wire dict BEFORE any middleware runs --
# see `DynamicSessionIntentMiddleware.__call__`'s comment) via a real
# `ClientSession`, and asserts on what a real client actually sees. This is
# the test that would have caught the bug.
# ---------------------------------------------------------------------------


def _dynamic_settings() -> Any:
    return load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_DYNAMIC_SESSION_INTENT": "true",
        }
    )


@pytest.mark.asyncio
async def test_e2e_fresh_session_tools_list_is_narrowed_to_core() -> None:
    """A fresh session with MCPG_DYNAMIC_SESSION_INTENT=1 alone (no static
    MCPG_SESSION_INTENT) sees only the `core` default -- not the full
    read-only surface. Matches docs/user-guide.md's documented 14-tool
    claim (10 core survivors + all 4 ALWAYS_KEEP tools, in default
    read-only mode) -- verified empirically here, not hardcoded blindly."""
    server = create_server(_dynamic_settings(), database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_tools()

    names = {t.name for t in listed.tools}
    assert len(names) == 14, f"expected 14 tools, got {len(names)}: {sorted(names)}"
    assert "list_session_intents" in names  # ALWAYS_KEEP, and the flag is on
    assert "enable_session_intent" in names
    assert "describe_self" in names
    assert "list_tables" in names  # a core headline tool
    # A non-core, non-always-kept tool must NOT be visible yet.
    assert "list_active_queries" not in names  # operations_and_health, not in core


@pytest.mark.asyncio
async def test_e2e_enable_session_intent_grows_the_real_tools_list() -> None:
    """The full round trip the bug hid: enabling an intent through a real
    `enable_session_intent` tool call must actually change a subsequent real
    `tools/list` response. Before the Finding-1 fix this stayed flat at the
    unfiltered count (187 observed against this fixture) instead of 14 -> 71.

    Note on semantics: `enabled_intents` unions whatever a session
    explicitly enables (`monitor` here) with its starting `default_intent`
    (`core`) -- it never drops the default just because something else got
    enabled. "Sessions only grow their visible surface" (the design spec,
    and `enable_session_intent`'s own tool description) is a real
    guarantee: growth must be strictly additive, or a session could
    silently lose tools it already had by enabling one more preset. An
    earlier version of this fix (and this test) got this backwards --
    `enabled_intents` returned the explicitly-enabled set alone once
    anything had been enabled, dropping `default_intent` entirely; caught
    in a second final-review pass after `vector_rag` (bucket-overlapping
    with `core`) had masked the loss in an earlier regression attempt.
    `monitor`'s buckets (`operations_and_health` / `advisors` /
    `observability` / `audit_trail`) have zero overlap with `core`'s
    schema/query headline tools, so this test can't be fooled the same
    way: every one of `before_names` must still be in `after_names`."""
    server = create_server(_dynamic_settings(), database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        before = await client.list_tools()
        before_names = {t.name for t in before.tools}

        result = await client.call_tool("enable_session_intent", {"name": "monitor"})
        assert result.is_error is False
        assert result.structured_content == {"ok": True, "enabled": ["core", "monitor"]}

        after = await client.list_tools()
        after_names = {t.name for t in after.tools}

    assert len(before_names) == 14
    assert len(after_names) == 71, f"expected 71 tools after enabling monitor, got {len(after_names)}"
    assert before_names < after_names  # strict growth: every core/always-kept tool survives, nothing lost
    assert ALWAYS_KEEP <= before_names
    assert ALWAYS_KEEP <= after_names
    assert "list_tables" in after_names  # a core headline tool -- must still be visible after enabling monitor
    assert "list_active_queries" in after_names  # operations_and_health, newly visible via monitor
    assert "list_active_queries" not in before_names  # not visible under the core default alone


@pytest.mark.asyncio
async def test_e2e_enable_session_intent_admin_reveals_everything_registered() -> None:
    """`visible_tool_names`'s "resolution is None" branch (the admin/no-filter
    sentinel) has no direct test anywhere else -- verify it through real
    dispatch: enabling "admin" must reveal every tool Layer 1 left
    registered, i.e. the full unfiltered read-only surface."""
    server = create_server(_dynamic_settings(), database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        unfiltered = {t.name for t in (await server.list_tools())}

        result = await client.call_tool("enable_session_intent", {"name": "admin"})
        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["ok"] is True

        listed = await client.list_tools()

    names = {t.name for t in listed.tools}
    assert names == unfiltered


@pytest.mark.asyncio
async def test_e2e_dynamic_intent_error_path_returns_structured_error() -> None:
    """Cheap coverage of the already-verified `DynamicIntentError` path
    through the same real-dispatch infrastructure this fix adds: an unknown
    intent name comes back as a structured `{"ok": false, ...}` payload with
    `isError=false`, not a protocol-level tool-call error."""
    server = create_server(_dynamic_settings(), database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("enable_session_intent", {"name": "not_a_real_preset_or_bucket"})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["ok"] is False
    assert "not_a_real_preset_or_bucket" in result.structured_content["error"]
