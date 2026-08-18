# Dynamic Session Intent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator statically narrow MCPg's tool surface to a headline-tools-based `"core"` preset (usable today via `MCPG_SESSION_INTENT=core`), and optionally let a session grow that surface at runtime — without a restart, without affecting other sessions — via two new opt-in meta-tools gated behind `MCPG_DYNAMIC_SESSION_INTENT`.

**Architecture:** Two layers built on the existing, shipped `src/mcpg/session_intent.py` (roadmap 8.8) rather than a parallel system. Layer 1 (static) adds a headline-tools-based `"core"` preset and the two-set (`buckets`, `tool_names`) resolution needed to support a preset that isn't bucket-shaped. Layer 2 (dynamic, new `src/mcpg/dynamic_session_intent.py` module) is a session-scoped `ServerMiddleware` that filters `tools/list` responses per session — response filtering, not registry mutation, so it composes safely under concurrent sessions on `streamable-http`/`sse`.

**Tech Stack:** Python 3.12–3.14, `mcp` SDK 2.0 (`mcp.server.mcpserver.MCPServer`, `mcp.server.context.ServerMiddleware`), `pytest` + `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-08-16-dynamic-toolsets-design.md` (twice-corrected; §0 documents the reconciliation with `session_intent.py`, §4 documents the Layer-1 filter-logic bug that was caught and fixed before this plan was written).

## Global Constraints

- Zero behavior change for the default (no env vars set) and for existing `MCPG_SESSION_INTENT` users who don't opt into `MCPG_DYNAMIC_SESSION_INTENT` — both are parity-tested (Task 9, Task 10).
- `INTENT_PRESETS`'s existing five entries and its `dict[str, frozenset[str]]` shape are untouched. `resolve_intent_to_buckets` is untouched. Every new surface is additive.
- Buckets and tool names are always checked as two separate sets, never merged into one — a merged predicate was tried during design and verified to keep 0 of the expected 14 tools instead of 14 (spec §4).
- Layer 2 is visibility-only, never an authorization boundary (spec §7) — `tools/call` is never filtered by anything in this plan.
- This project's pre-commit hook runs the full `pytest tests/unit` suite (~3 minutes) on every commit — pass a Bash timeout of at least 300000ms for every commit step below.

---

## Task 1: `session_intent.py` — two-set resolution + the `"core"` preset

**Files:**
- Modify: `src/mcpg/session_intent.py`
- Test: `tests/unit/test_session_intent.py`

**Interfaces:**
- Produces: `IntentResolution` (`typing.NamedTuple`, fields `buckets: frozenset[str]`, `tool_names: frozenset[str]`); `resolve_intent(intent_values: tuple[str, ...]) -> IntentResolution | None`; `resolved_tool_names(resolution: IntentResolution, candidate_names: Iterable[str], *, always_keep: frozenset[str] = ALWAYS_KEEP) -> frozenset[str]`; `ALWAYS_KEEP: frozenset[str]` (public, includes `describe_self`, `describe_tool`, `list_session_intents`, `enable_session_intent`); `_TOOL_NAME_PRESETS: dict[str, frozenset[str]]` (one entry, `"core"`).
- Consumes: `mcpg.about.CAPABILITIES`, `mcpg.about.classify_tool` (already imported in this module).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_session_intent.py` (after the existing `filter_server_tools` tests, before the module ends):

```python
from mcpg.session_intent import (
    ALWAYS_KEEP,
    IntentResolution,
    _TOOL_NAME_PRESETS,
    resolve_intent,
    resolved_tool_names,
)


# ---------------------------------------------------------------------------
# ALWAYS_KEEP (public export)
# ---------------------------------------------------------------------------


def test_always_keep_includes_the_dynamic_meta_tools() -> None:
    assert ALWAYS_KEEP == {
        "describe_self",
        "describe_tool",
        "list_session_intents",
        "enable_session_intent",
    }


# ---------------------------------------------------------------------------
# "core" preset
# ---------------------------------------------------------------------------


def test_core_preset_is_headline_tools_of_schema_and_query_buckets() -> None:
    from mcpg.about import CAPABILITIES

    expected = {
        name
        for cap in CAPABILITIES
        if cap.id in ("schema_introspection", "query_execution")
        for name in cap.headline_tools
    }
    assert _TOOL_NAME_PRESETS["core"] == frozenset(expected)
    assert len(_TOOL_NAME_PRESETS["core"]) == 12


# ---------------------------------------------------------------------------
# resolve_intent — two-set resolution
# ---------------------------------------------------------------------------


def test_resolve_intent_returns_none_for_empty_input() -> None:
    assert resolve_intent(()) is None


def test_resolve_intent_bucket_preset_matches_resolve_intent_to_buckets() -> None:
    resolution = resolve_intent(("lookup",))
    assert resolution == IntentResolution(buckets=INTENT_PRESETS["lookup"], tool_names=frozenset())


def test_resolve_intent_admin_short_circuits_to_none() -> None:
    assert resolve_intent(("admin",)) is None
    assert resolve_intent(("lookup", "admin")) is None


def test_resolve_intent_core_preset_is_tool_names_only() -> None:
    resolution = resolve_intent(("core",))
    assert resolution is not None
    assert resolution.buckets == frozenset()
    assert resolution.tool_names == _TOOL_NAME_PRESETS["core"]


def test_resolve_intent_combines_bucket_and_tool_name_presets() -> None:
    resolution = resolve_intent(("core", "monitor"))
    assert resolution is not None
    assert resolution.buckets == INTENT_PRESETS["monitor"]
    assert resolution.tool_names == _TOOL_NAME_PRESETS["core"]


def test_resolve_intent_unknown_name_falls_back_to_raw_bucket_id() -> None:
    resolution = resolve_intent(("vector_search",))
    assert resolution == IntentResolution(buckets=frozenset({"vector_search"}), tool_names=frozenset())


# ---------------------------------------------------------------------------
# resolved_tool_names — the shared keep-decision Layer 1 and Layer 2 both use
# ---------------------------------------------------------------------------


def test_resolved_tool_names_regression_core_keeps_14_not_0() -> None:
    """The bug caught during design: passing a tool-name-only resolution
    through a bucket-only keep-check kept 0 of 14 tools, not 14. This must
    pass against the fixed implementation."""
    resolution = resolve_intent(("core",))
    assert resolution is not None
    candidates = _TOOL_NAME_PRESETS["core"] | {"describe_self", "describe_tool", "run_ddl_unrelated_tool"}
    kept = resolved_tool_names(resolution, candidates)
    assert kept == _TOOL_NAME_PRESETS["core"] | {"describe_self", "describe_tool"}


def test_resolved_tool_names_bucket_preset_still_works() -> None:
    resolution = resolve_intent(("lookup",))
    assert resolution is not None
    candidates = ["list_tables", "run_ddl", "describe_self"]
    kept = resolved_tool_names(resolution, candidates)
    # list_tables -> schema_introspection (in lookup); run_ddl -> query_execution (in lookup);
    # describe_self -> always_keep regardless.
    assert kept == frozenset({"list_tables", "run_ddl", "describe_self"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_session_intent.py -v`
Expected: FAIL — `ImportError: cannot import name 'IntentResolution' from 'mcpg.session_intent'` (and similar for the other new names).

- [ ] **Step 3: Implement**

In `src/mcpg/session_intent.py`:

1. Add `NamedTuple` to the `typing` import at the top (currently only `TYPE_CHECKING` is imported):

```python
from typing import TYPE_CHECKING, NamedTuple
```

2. Change the `about` import to also pull `CAPABILITIES`:

```python
from mcpg.about import CAPABILITIES, classify_tool
```

3. Rename `_ALWAYS_KEEP` to `ALWAYS_KEEP`, export it, and add the two new meta-tool names (they don't need to exist as registered tools for this frozenset literal to be valid — it's just data):

```python
# Tools that are NEVER removed by the intent filter — without them an
# agent connecting to a narrowed surface has no way to learn what's on
# the wire, or (for the two dynamic-session-intent meta-tools, roadmap
# 22) to discover/grow its own surface at runtime. All four are
# read-only, no DB access. Public (not `_`-prefixed): the dynamic
# layer in `mcpg.dynamic_session_intent` reuses this directly rather
# than defining its own always-visible set.
ALWAYS_KEEP: frozenset[str] = frozenset(
    {
        "describe_self",
        "describe_tool",
        "list_session_intents",
        "enable_session_intent",
    }
)
```

4. Delete the old `filter_server_tools` function body's `always_keep: frozenset[str] = _ALWAYS_KEEP` default reference — it now reads `always_keep: frozenset[str] = ALWAYS_KEEP` (see step 6 below, which replaces the whole function).

5. Add the `"core"` preset and `IntentResolution`, placed right after the `INTENT_PRESETS` dict definition (after the closing `}` of `INTENT_PRESETS`, before `resolve_intent_to_buckets`):

```python
# Headline-tools-based preset — deliberately NOT a bucket set. About 4x
# tighter than the coarsest bucket preset (`lookup`, 3 buckets ~56
# tools): built from `about.py`'s own curated `headline_tools`, so
# there's one source of truth for "what matters most" shared across
# this preset, the dynamic session-intent feature (roadmap 22), and
# `describe_self`'s headline display. Kept in a separate dict from
# `INTENT_PRESETS` rather than widening that dict's value type —
# additive, not a breaking change to the existing five entries or
# their `dict[str, frozenset[str]]` public shape.
_TOOL_NAME_PRESETS: dict[str, frozenset[str]] = {
    "core": frozenset(
        name
        for cap in CAPABILITIES
        if cap.id in ("schema_introspection", "query_execution")
        for name in cap.headline_tools
    ),
}


class IntentResolution(NamedTuple):
    """The two independently-checked halves of a resolved intent.

    Kept separate rather than merged into one set: bucket ids and tool
    names are different namespaces (a bucket preset like ``lookup``
    names buckets; ``core`` names literal tools), and a merged
    predicate silently drops every tool-name preset's tools — a
    bucket-only keep-check never matches a literal tool name against a
    bucket id. Caught during design, not left as a latent bug.
    """

    buckets: frozenset[str]
    tool_names: frozenset[str]


def resolve_intent(intent_values: tuple[str, ...]) -> IntentResolution | None:
    """Like :func:`resolve_intent_to_buckets`, but also resolves
    tool-name presets (currently just ``core``). Supersedes
    ``resolve_intent_to_buckets`` for callers that need tool-name
    presets; that function is unchanged and still bucket-only.

    Returns ``None`` under the same conditions as
    ``resolve_intent_to_buckets``: empty input, or the ``admin``
    sentinel present anywhere in the input.
    """
    if not intent_values:
        return None
    buckets: set[str] = set()
    tool_names: set[str] = set()
    for raw in intent_values:
        name = raw.strip().lower()
        if not name:
            continue
        if name in INTENT_PRESETS:
            preset_buckets = INTENT_PRESETS[name]
            if not preset_buckets:
                return None  # admin sentinel
            buckets |= preset_buckets
        elif name in _TOOL_NAME_PRESETS:
            tool_names |= _TOOL_NAME_PRESETS[name]
        else:
            buckets.add(name)
    return IntentResolution(buckets=frozenset(buckets), tool_names=frozenset(tool_names))
```

6. Replace the existing `filter_server_tools` function with a version built on a new shared helper, `resolved_tool_names`, so Layer 2 (Task 5) can reuse the exact same keep-decision instead of duplicating it:

```python
def resolved_tool_names(
    resolution: IntentResolution,
    candidate_names: Iterable[str],
    *,
    always_keep: frozenset[str] = ALWAYS_KEEP,
) -> frozenset[str]:
    """The subset of ``candidate_names`` that survive ``resolution``.

    The one keep-decision both :func:`filter_server_tools` (registry
    mutation, launch-time) and ``mcpg.dynamic_session_intent``'s
    response filter (per-session, runtime) apply — kept in exactly one
    place so the two layers can never silently diverge.
    """
    kept: set[str] = set()
    for name in candidate_names:
        if name in always_keep or name in resolution.tool_names:
            kept.add(name)
        elif classify_tool(name) in resolution.buckets:
            kept.add(name)
    return frozenset(kept)


def filter_server_tools(
    server: MCPServer,
    allowed_buckets: frozenset[str],
    *,
    allowed_tool_names: frozenset[str] = frozenset(),
    always_keep: frozenset[str] = ALWAYS_KEEP,
) -> list[str]:
    """Remove every registered tool that doesn't survive the resolved intent.

    ``allowed_tool_names`` is new (additive) — existing callers that
    only pass ``allowed_buckets`` positionally are unaffected, since it
    defaults to empty and every existing preset is bucket-only.

    Returns the list of removed tool names, sorted. Idempotent.
    """
    resolution = IntentResolution(buckets=allowed_buckets, tool_names=allowed_tool_names)
    all_names = [tool.name for tool in server._tool_manager.list_tools()]
    keep = resolved_tool_names(resolution, all_names, always_keep=always_keep)
    removed: list[str] = []
    for name in all_names:
        if name not in keep:
            server.remove_tool(name)
            removed.append(name)
    return sorted(removed)
```

7. Add `Iterable` to the imports (`from collections.abc import Iterable`, near the top with the other imports).

8. Update `__all__`:

```python
__all__ = [
    "ALWAYS_KEEP",
    "INTENT_PRESETS",
    "IntentResolution",
    "filter_server_tools",
    "parse_intent_setting",
    "resolve_intent",
    "resolve_intent_to_buckets",
    "resolved_tool_names",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_session_intent.py -v`
Expected: all pass, including the pre-existing tests (they reference `resolve_intent_to_buckets`, unchanged).

- [ ] **Step 5: Run mypy**

Run: `uv run mypy src/mcpg/session_intent.py`
Expected: `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/mcpg/session_intent.py tests/unit/test_session_intent.py
git commit -m "feat(session_intent): add core preset + two-set intent resolution

Advances roadmap row: 22"
```

---

## Task 2: Switch the production call site to `resolve_intent`

**Files:**
- Modify: `src/mcpg/tools.py:7129-7134`
- Test: `tests/unit/test_tools.py` (or a new focused test — see Step 1)

**Interfaces:**
- Consumes: `mcpg.session_intent.resolve_intent`, `mcpg.session_intent.filter_server_tools` (Task 1).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_tools.py`:

```python
import pytest
from mcp.server.mcpserver import MCPServer

from mcpg.config import load_settings
from mcpg.tools import register_tools

_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"


async def test_session_intent_core_actually_narrows_the_surface() -> None:
    """Regression test for the call-site switch: MCPG_SESSION_INTENT=core
    must narrow the registered surface to ~14 tools, not to 0 (the bug
    this task fixes) and not leave it at 254 (the bug of not switching
    the call site at all)."""
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_SESSION_INTENT": "core",
        }
    )
    server: MCPServer = MCPServer("mcpg-core-intent-fixture")
    register_tools(server, settings)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "describe_self" in names
    assert "describe_tool" in names
    assert "run_ddl" not in names  # not permitted under read-only access_mode anyway, but also not in core
    assert "list_tables" in names
    assert "run_select" in names
    assert 13 <= len(names) <= 15  # 12 headline tools + describe_self + describe_tool
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_tools.py::test_session_intent_core_actually_narrows_the_surface -v`
Expected: FAIL — `len(names)` is 2 (only `describe_self`/`describe_tool` survive), because the call site still uses `resolve_intent_to_buckets`, which returns an empty bucket set for `("core",)` (no entry in `INTENT_PRESETS`, "core" falls through to "treat as raw bucket id" — matches nothing, since no tool has `classify_tool(name) == "core"`).

- [ ] **Step 3: Switch the call site**

In `src/mcpg/tools.py`, replace lines 7129-7134:

```python
    if settings.session_intent:
        from mcpg.session_intent import filter_server_tools, resolve_intent_to_buckets

        allowed = resolve_intent_to_buckets(settings.session_intent)
        if allowed is not None:
            filter_server_tools(server, allowed)
```

with:

```python
    if settings.session_intent:
        from mcpg.session_intent import filter_server_tools, resolve_intent

        resolution = resolve_intent(settings.session_intent)
        if resolution is not None:
            filter_server_tools(server, resolution.buckets, allowed_tool_names=resolution.tool_names)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_tools.py::test_session_intent_core_actually_narrows_the_surface -v`
Expected: PASS.

- [ ] **Step 5: Run the full session_intent + tools unit suites to check for regressions**

Run: `uv run pytest tests/unit/test_session_intent.py tests/unit/test_tools.py -v`
Expected: all pass — existing bucket-preset behavior (`lookup`, `migration`, etc.) is unchanged since `resolve_intent` returns the same `.buckets` value `resolve_intent_to_buckets` would have for those.

- [ ] **Step 6: Commit**

```bash
git add src/mcpg/tools.py tests/unit/test_tools.py
git commit -m "fix(tools): switch session_intent call site to resolve_intent

MCPG_SESSION_INTENT=core previously resolved to an empty bucket set
(core has no INTENT_PRESETS entry) and filtered down to 2 tools
instead of ~14 -- the one production call site needed the new
two-set resolver from the previous commit, not just its existence.

Advances roadmap row: 22"
```

---

## Task 3: `about.py` — classify the two new meta-tools

**Files:**
- Modify: `src/mcpg/about.py`
- Test: `tests/unit/test_about.py`

**Interfaces:**
- Produces: `classify_tool("list_session_intents") == "observability"`, `classify_tool("enable_session_intent") == "observability"`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_about.py`:

```python
def test_list_session_intents_is_not_caught_by_the_list_prefix_pattern() -> None:
    """Regression guard: `list_session_intents` starts with `list_`, which
    the generic schema_introspection catch-all pattern would otherwise
    match. It needs an explicit override to land in `observability`."""
    assert classify_tool("list_session_intents") == "observability"


def test_enable_session_intent_is_classified() -> None:
    assert classify_tool("enable_session_intent") == "observability"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_about.py -k "session_intent" -v`
Expected: FAIL — `test_list_session_intents_is_not_caught_by_the_list_prefix_pattern` gets `"schema_introspection"` instead of `"observability"` (caught by the `^(list_|describe_table)` catch-all); `test_enable_session_intent_is_classified` gets `None`.

- [ ] **Step 3: Add the overrides**

In `src/mcpg/about.py`, add two entries to `_TOOL_TO_BUCKET_OVERRIDES` (near the top of that dict, after the opening `{`):

```python
    # list_session_intents / enable_session_intent are the dynamic
    # session-intent meta-tools (roadmap 22). Both need an explicit
    # override: "list_session_intents" would otherwise be caught by
    # the generic ^list_ catch-all pattern below (schema_introspection)
    # rather than landing in observability where it belongs, and
    # "enable_session_intent" matches no existing pattern at all.
    "list_session_intents": "observability",
    "enable_session_intent": "observability",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_about.py -k "session_intent" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mcpg/about.py tests/unit/test_about.py
git commit -m "feat(about): classify the dynamic session-intent meta-tools

Advances roadmap row: 22"
```

---

## Task 4: `config.py` — `MCPG_DYNAMIC_SESSION_INTENT` setting

**Files:**
- Modify: `src/mcpg/config.py:245` (field), `:1223` (parsing block area), `:1319` (constructor call)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings.dynamic_session_intent: bool` (default `False`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_config.py` (near the existing `session_intent` / `allow_ddl` tests):

```python
def test_dynamic_session_intent_defaults_to_false() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _FIXTURE_DB_URL})
    assert settings.dynamic_session_intent is False


def test_dynamic_session_intent_true_parses() -> None:
    settings = load_settings(
        {"MCPG_DATABASE_URL": _FIXTURE_DB_URL, "MCPG_DYNAMIC_SESSION_INTENT": "true"}
    )
    assert settings.dynamic_session_intent is True


def test_dynamic_session_intent_false_parses() -> None:
    settings = load_settings(
        {"MCPG_DATABASE_URL": _FIXTURE_DB_URL, "MCPG_DYNAMIC_SESSION_INTENT": "false"}
    )
    assert settings.dynamic_session_intent is False
```

(If `_FIXTURE_DB_URL` isn't already module-level in `test_config.py`, use the same fixture DSN as elsewhere in this plan: `"postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -k dynamic_session_intent -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'dynamic_session_intent'`.

- [ ] **Step 3: Add the field, parsing, and wiring**

In `src/mcpg/config.py`, add the field right after `session_intent: tuple[str, ...] = ()` (currently line 245):

```python
    # Opt-in runtime extension of session_intent (roadmap 22): lets a
    # session grow its own visible tool surface at runtime via the
    # list_session_intents / enable_session_intent meta-tools, instead
    # of being fixed to whatever MCPG_SESSION_INTENT resolved at
    # startup. Visibility-only -- see mcpg.dynamic_session_intent's
    # module docstring for why this is not an authorization boundary.
    dynamic_session_intent: bool = False
```

Add parsing right after the existing `session_intent` parsing block (currently lines 1223-1227, ending `session_intent = parse_intent_setting(raw)`):

```python
    dynamic_session_intent = False
    if (raw := env.get("MCPG_DYNAMIC_SESSION_INTENT")) is not None:
        dynamic_session_intent = _parse_bool("MCPG_DYNAMIC_SESSION_INTENT", raw)
```

Add the constructor argument right after `session_intent=session_intent,` (currently line 1319):

```python
        dynamic_session_intent=dynamic_session_intent,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -k dynamic_session_intent -v`
Expected: PASS.

- [ ] **Step 5: Run mypy**

Run: `uv run mypy src/mcpg/config.py`
Expected: `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/mcpg/config.py tests/unit/test_config.py
git commit -m "feat(config): add MCPG_DYNAMIC_SESSION_INTENT setting

Advances roadmap row: 22"
```

---

## Task 5: `dynamic_session_intent.py` — session state + resolution

**Files:**
- Create: `src/mcpg/dynamic_session_intent.py`
- Test: `tests/unit/test_dynamic_session_intent.py`

**Interfaces:**
- Consumes: `mcpg.session_intent.resolve_intent`, `mcpg.session_intent.resolved_tool_names`, `mcpg.session_intent.ALWAYS_KEEP`, `mcpg.session_intent.INTENT_PRESETS`, `mcpg.session_intent._TOOL_NAME_PRESETS` (Task 1); `mcpg.about.BUCKET_IDS`.
- Produces: `DynamicIntentError`; `STDIO_SESSION_KEY: str`; `session_key_from_headers(headers: Mapping[str, str] | None) -> str`; `enabled_intents(session_key: str, *, default_intent: tuple[str, ...]) -> frozenset[str]`; `visible_tool_names(session_key: str, *, default_intent: tuple[str, ...], registered: frozenset[str]) -> frozenset[str]`; `async def enable_intent(session_key: str, name: str) -> None`; `async def enable_intent_and_notify(session_key: str, name: str, *, notify: Callable[[], Awaitable[None]]) -> None` (thin wrapper around `enable_intent` that also fires a caller-supplied notification callback — split out so Task 7's tool can pass the real `ctx.session.send_tool_list_changed` while tests pass a fake, without needing a live MCP session; mirrors this codebase's existing `build_server_info()`/`get_server_info` split where the testable logic is a plain function and the `@server.tool`-decorated closure is a thin wrapper). Task 6 (same file) additionally produces `DynamicSessionIntentMiddleware`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dynamic_session_intent.py`:

```python
"""Tests for the dynamic session-intent runtime layer (roadmap 22)."""

from __future__ import annotations

import pytest

from mcpg.dynamic_session_intent import (
    STDIO_SESSION_KEY,
    DynamicIntentError,
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
    await enable_intent(session_key, "extension_specific")  # any real bucket id not covered by a preset
    assert "extension_specific" in enabled_intents(session_key, default_intent=("core",))


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
    registered = frozenset({"list_tables", "run_select", "run_ddl", "describe_self", "describe_tool"})
    visible = visible_tool_names("session-8", default_intent=("core",), registered=registered)
    # "core" resolves to a tool-name set that includes list_tables and run_select
    # (both are headline tools of schema_introspection/query_execution) but not run_ddl.
    assert "list_tables" in visible
    assert "run_select" in visible
    assert "describe_self" in visible  # ALWAYS_KEEP
    assert "run_ddl" not in visible


@pytest.mark.asyncio
async def test_visible_tool_names_grows_after_enable_intent() -> None:
    session_key = "session-9"
    registered = frozenset({"list_tables", "run_ddl", "describe_self", "describe_tool"})
    before = visible_tool_names(session_key, default_intent=("core",), registered=registered)
    assert "run_ddl" not in before

    await enable_intent(session_key, "migration")  # migration includes query_execution's bucket
    after = visible_tool_names(session_key, default_intent=("core",), registered=registered)
    assert "run_ddl" in after


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_dynamic_session_intent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpg.dynamic_session_intent'`.

- [ ] **Step 3: Implement**

Create `src/mcpg/dynamic_session_intent.py`:

```python
"""Dynamic session-intent — grow a session's visible tool surface at runtime.

Realises roadmap row 22, layered on top of ``mcpg.session_intent``
(roadmap 8.8) rather than duplicating its preset vocabulary. Where
``session_intent`` narrows the tool surface once, at launch, for every
session (via ``MCPG_SESSION_INTENT``, by physically removing tools
from the SDK's registry), this module lets one *individual* session
grow its own view of the surface at runtime, without a restart and
without affecting any other concurrent session.

Response filtering, not registry mutation
==========================================

``MCPServer``'s tool registry is process-wide, not per-session — two
concurrent ``streamable-http`` sessions share one ``MCPServer``
instance. Mutating the registry per session is therefore not an
option (session A's growth would leak into session B's view). Instead
:class:`DynamicSessionIntentMiddleware` lets every tool register
normally (or survive whatever ``session_intent``'s static filter left)
and narrows only the *response* to each session's own ``tools/list``
call, keyed by the transport's own ``Mcp-Session-Id`` header.

Not an authorization boundary
==============================

This is visibility only. A client that already knows a filtered-out
tool's name and schema can still call it directly — ``tools/call`` is
never filtered by anything in this module. The real authorization
boundary is ``MCPG_ACCESS_MODE`` / capability gating in
``mcpg.policy``, untouched by this feature. Contrast with
``session_intent``'s static filter, which *does* achieve true
invisibility (registry removal) — that's why it's launch-time only,
per its own module docstring. This module makes no such claim.

Opt-in
======

Enabled only via ``MCPG_DYNAMIC_SESSION_INTENT``. Off by default:
zero behavior change for existing deployments, with or without
``MCPG_SESSION_INTENT`` also configured.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from mcp_types import ListToolsResult

from mcpg.about import BUCKET_IDS
from mcpg.session_intent import INTENT_PRESETS, _TOOL_NAME_PRESETS, resolve_intent, resolved_tool_names

if TYPE_CHECKING:
    from mcp.server.context import CallNext, ServerRequestContext


class DynamicIntentError(ValueError):
    """Raised when :func:`enable_intent` is given an unrecognized name."""


# stdio is inherently single-session-per-process (no Mcp-Session-Id
# header exists there) — every stdio call shares this one sentinel key,
# consistent with how mcpg.tenancy.current_role also no-ops distinctly
# on stdio.
STDIO_SESSION_KEY = "__stdio__"

# Per-session enabled-intent-name state. A session_key with no entry
# yet behaves identically to one with an empty set (see
# enabled_intents) — there's no separate "initialize" step to get
# wrong. Guarded by _lock since enable_intent can race across
# concurrent requests on the same session.
_session_intents: dict[str, set[str]] = {}
_lock = asyncio.Lock()


def session_key_from_headers(headers: Mapping[str, str] | None) -> str:
    """Resolve the per-session state key from a request's headers.

    ``headers`` is ``None`` on stdio (no HTTP request at all) or when
    a transport's request object carries no headers. A present-but-
    empty header set (the header wasn't sent) also falls back to the
    stdio sentinel rather than raising — an MCP client is expected to
    always send ``Mcp-Session-Id`` after the initial handshake, but a
    missing header degrading to "shared state" rather than crashing is
    the safer failure mode.
    """
    if not headers:
        return STDIO_SESSION_KEY
    session_id = headers.get("mcp-session-id")
    return session_id if session_id else STDIO_SESSION_KEY


def enabled_intents(session_key: str, *, default_intent: tuple[str, ...]) -> frozenset[str]:
    """The intent names currently enabled for ``session_key``.

    A session that hasn't called ``enable_intent`` yet resolves to
    ``default_intent`` — whatever ``MCPG_SESSION_INTENT`` was
    configured with, or ``("core",)`` when that's unset (the caller
    decides which; see ``DynamicSessionIntentMiddleware``).
    """
    enabled = _session_intents.get(session_key)
    return frozenset(enabled) if enabled else frozenset(default_intent)


def visible_tool_names(
    session_key: str,
    *,
    default_intent: tuple[str, ...],
    registered: frozenset[str],
) -> frozenset[str]:
    """The tools ``session_key`` should see, intersected with ``registered``.

    ``registered`` is whatever the SDK's ``MCPServer`` actually still
    has — i.e. whatever ``session_intent``'s static filter left, or
    everything if that wasn't configured. This intersection is what
    makes the static-filter/dynamic-layer ceiling relationship real in
    code: enabling an intent whose tools were never registered reveals
    nothing, rather than erroring or silently exceeding the ceiling.
    """
    names = enabled_intents(session_key, default_intent=default_intent)
    resolution = resolve_intent(tuple(names))
    if resolution is None:
        # "admin" was enabled (or the resolved default was), which is
        # the explicit no-filter sentinel — reveal everything Layer 1
        # left registered.
        return registered
    return resolved_tool_names(resolution, registered)


async def enable_intent(session_key: str, name: str) -> None:
    """Add ``name`` (a preset name or a raw bucket id) to ``session_key``'s
    enabled set. Idempotent. Raises :class:`DynamicIntentError` on an
    unrecognized name.
    """
    normalized = name.strip().lower()
    if not normalized:
        raise DynamicIntentError("intent name must not be blank")
    known = normalized in INTENT_PRESETS or normalized in _TOOL_NAME_PRESETS or normalized in BUCKET_IDS
    if not known:
        raise DynamicIntentError(
            f"{name!r} is not a known session-intent preset or capability-bucket id. "
            "Call list_session_intents() to see the available names."
        )
    async with _lock:
        _session_intents.setdefault(session_key, set()).add(normalized)


async def enable_intent_and_notify(
    session_key: str,
    name: str,
    *,
    notify: Callable[[], Awaitable[None]],
) -> None:
    """``enable_intent``, then invoke ``notify`` (the caller's
    ``tools/list_changed`` notification). Split out from the
    ``@server.tool``-decorated closure that calls this (see
    ``mcpg.tools._register_dynamic_session_intent``) so the
    notify-on-success / no-notify-on-error behavior is unit-testable
    with a fake ``notify`` callback, without needing a live MCP
    session — mirrors this codebase's existing ``build_server_info()``
    / ``get_server_info`` split.
    """
    await enable_intent(session_key, name)
    await notify()


__all__ = [
    "STDIO_SESSION_KEY",
    "DynamicIntentError",
    "enable_intent",
    "enable_intent_and_notify",
    "enabled_intents",
    "session_key_from_headers",
    "visible_tool_names",
]
```

Note: `ListToolsResult` is imported here even though it's used in Task 6's `DynamicSessionIntentMiddleware`, defined in the same file — importing it now avoids a second edit pass. If your editor flags it unused after this task alone, that's expected; Task 6 uses it in the same file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_dynamic_session_intent.py -v`
Expected: all pass.

- [ ] **Step 5: Run mypy**

Run: `uv run mypy src/mcpg/dynamic_session_intent.py`
Expected: `Success: no issues found` (the `ListToolsResult` import will be unused until Task 6 — if mypy or ruff flags it, remove the import in this task and re-add it in Task 6 instead; either ordering is fine, this note just tells you why it might look premature).

- [ ] **Step 6: Commit**

```bash
git add src/mcpg/dynamic_session_intent.py tests/unit/test_dynamic_session_intent.py
git commit -m "feat: add dynamic session-intent state layer

Advances roadmap row: 22"
```

---

## Task 6: `DynamicSessionIntentMiddleware`

**Files:**
- Modify: `src/mcpg/dynamic_session_intent.py`
- Test: `tests/unit/test_dynamic_session_intent.py`

**Interfaces:**
- Consumes: everything from Task 5 in the same module; `mcp.server.context.ServerMiddleware`/`CallNext`/`ServerRequestContext` (the same imports `mcpg.tenancy.TenantRoleContextMiddleware` uses); `mcp_types.ListToolsResult`.
- Produces: `DynamicSessionIntentMiddleware` (a `ServerMiddleware`).

**Design note — deviates from the spec's "Session-id capture (ASGI layer)" component:** the spec (§ Session-id capture) called for a new ASGI middleware that stashes the `mcp-session-id` header onto the ASGI scope, mirroring `_TenantRoleMiddleware`. That pattern exists in `tenancy.py` because `X-MCPG-Role` needs *validation* (identifier regex, allowlist, a 403 response) that must happen at the ASGI layer, before the SDK's own dispatch. `Mcp-Session-Id` needs no such validation — it's already assigned and pattern-checked by the SDK itself (`mcp.server.streamable_http.SESSION_ID_PATTERN`). Tracing `ServerRequestContext.request` through `mcp/server/runner.py:_make_context` → `mcp/server/_streamable_http_modern.py` confirms `ctx.request` is a real Starlette `Request` object with a `.headers` property (a case-insensitive mapping) directly available — no scope-stashing indirection needed. This is a real simplification found during planning, not a spec deviation to defer: it removes an entire planned component with no loss of behavior.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_dynamic_session_intent.py`:

```python
from mcp_types import Tool

from mcpg.dynamic_session_intent import DynamicSessionIntentMiddleware


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_dynamic_session_intent.py -k Middleware -v`
Expected: FAIL — `ImportError: cannot import name 'DynamicSessionIntentMiddleware'`.

- [ ] **Step 3: Implement**

Append to `src/mcpg/dynamic_session_intent.py` (after `enable_intent`, before `__all__`):

```python
class DynamicSessionIntentMiddleware:
    """Filters ``tools/list`` responses to each session's visible surface.

    Registered only when ``MCPG_DYNAMIC_SESSION_INTENT`` is enabled
    (see ``mcpg.server``). All other request kinds pass through
    untouched — this only ever narrows what a ``tools/list`` call
    returns, never what a ``tools/call`` can invoke (see the module
    docstring's "Not an authorization boundary" section).
    """

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> Any:
        result = await call_next(ctx)
        if ctx.method != "tools/list" or not isinstance(result, ListToolsResult):
            return result

        settings = ctx.lifespan_context.settings
        default_intent = settings.session_intent or ("core",)
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        session_key = session_key_from_headers(headers)

        registered = frozenset(tool.name for tool in result.tools)
        visible = visible_tool_names(session_key, default_intent=default_intent, registered=registered)
        return result.model_copy(update={"tools": [tool for tool in result.tools if tool.name in visible]})
```

Update `__all__` to add `"DynamicSessionIntentMiddleware"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_dynamic_session_intent.py -v`
Expected: all pass (both Task 5's and Task 6's tests).

- [ ] **Step 5: Static-conformance check + mypy**

Add this near the bottom of `src/mcpg/dynamic_session_intent.py` (mirrors the exact pattern `tenancy.py` uses for `TenantRoleContextMiddleware` — a compile-time-only structural check that this class satisfies the `ServerMiddleware` protocol, never evaluated at runtime):

```python
if TYPE_CHECKING:
    from mcp.server.context import ServerMiddleware

    _dynamic_intent_middleware_matches_protocol: ServerMiddleware[Any] = DynamicSessionIntentMiddleware()
```

Run: `uv run mypy src/mcpg/dynamic_session_intent.py`
Expected: `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/mcpg/dynamic_session_intent.py tests/unit/test_dynamic_session_intent.py
git commit -m "feat: add DynamicSessionIntentMiddleware for per-session tools/list filtering

Reads Mcp-Session-Id directly off ctx.request.headers rather than the
spec's originally-planned ASGI scope-stashing middleware -- traced
ServerRequestContext.request through mcp/server/runner.py and
_streamable_http_modern.py and confirmed it's a real Starlette
Request with a case-insensitive .headers mapping already available,
so no new ASGI middleware component is needed.

Advances roadmap row: 22"
```

---

## Task 7: Register the two meta-tools

**Files:**
- Modify: `src/mcpg/tools.py`
- Test: `tests/unit/test_tools.py`

**Interfaces:**
- Consumes: `mcpg.dynamic_session_intent.{DynamicIntentError, enable_intent_and_notify, enabled_intents, session_key_from_headers}` (Task 5); `mcpg.session_intent.{INTENT_PRESETS, _TOOL_NAME_PRESETS}` (Task 1); `mcpg.about.classify_tool`.
- Produces: `list_session_intents`, `enable_session_intent` — MCP tools, registered only when `settings.dynamic_session_intent` is true, registered **before** the existing session-intent filter block (currently at `tools.py:7123-7134`, renumbered by Task 2's edit but still the last block in `register_tools`) — ordering matters, see Task 3's `_TOOL_TO_BUCKET_OVERRIDES` and the spec's §4 ordering note: the filter runs last, so these two tools must already be registered by the time it runs for `ALWAYS_KEEP` to matter under a configured `MCPG_SESSION_INTENT`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_tools.py`:

```python
async def test_dynamic_session_intent_tools_not_registered_by_default() -> None:
    settings = load_settings({"MCPG_DATABASE_URL": _FIXTURE_DB_URL})
    server: MCPServer = MCPServer("mcpg-dynamic-off-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" not in names
    assert "enable_session_intent" not in names


async def test_dynamic_session_intent_tools_registered_when_enabled() -> None:
    settings = load_settings(
        {"MCPG_DATABASE_URL": _FIXTURE_DB_URL, "MCPG_DYNAMIC_SESSION_INTENT": "true"}
    )
    server: MCPServer = MCPServer("mcpg-dynamic-on-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" in names
    assert "enable_session_intent" in names


async def test_dynamic_session_intent_tools_survive_a_narrow_static_intent() -> None:
    """The ordering/ALWAYS_KEEP guarantee: under MCPG_SESSION_INTENT=lookup
    (which doesn't cover the observability bucket these tools classify
    into), the two meta-tools must still survive -- otherwise a session
    under a narrow static intent would have no way to discover or grow
    its own surface."""
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_SESSION_INTENT": "lookup",
            "MCPG_DYNAMIC_SESSION_INTENT": "true",
        }
    )
    server: MCPServer = MCPServer("mcpg-dynamic-and-static-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" in names
    assert "enable_session_intent" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tools.py -k dynamic_session_intent_tools -v`
Expected: `test_dynamic_session_intent_tools_not_registered_by_default` PASSes trivially (nothing registered yet); the other two FAIL (`list_session_intents` / `enable_session_intent` absent).

- [ ] **Step 3: Implement**

Add a new registration function to `src/mcpg/tools.py`. Place it near `_register_server_info` (both are observability-bucket, self-description tools — keeps related registration functions co-located, matching how the file already groups them by bucket):

```python
def _register_dynamic_session_intent(server: MCPServer[AppContext], settings: Settings) -> None:
    from mcpg.about import classify_tool
    from mcpg.dynamic_session_intent import (
        DynamicIntentError,
        enable_intent_and_notify,
        enabled_intents,
        session_key_from_headers,
    )
    from mcpg.session_intent import _TOOL_NAME_PRESETS, INTENT_PRESETS

    default_intent = settings.session_intent or ("core",)

    @server.tool(
        name="list_session_intents",
        description=(
            "List every session-intent preset (bucket-based presets like "
            "`lookup`/`migration`/`vector_rag`/`monitor`/`admin`, plus the "
            "finer headline-based `core` preset), each one's resolved tool "
            "count against the currently registered surface, and whether "
            "it's enabled for this session. A raw capability-bucket id is "
            "also accepted by `enable_session_intent` for coverage a preset "
            "doesn't offer. Call this before `enable_session_intent` to see "
            "what's available; the session starts at `core` (or whatever "
            "MCPG_SESSION_INTENT was configured with) until you enable more."
        ),
    )
    async def list_session_intents(ctx: _Ctx) -> dict[str, Any]:
        registered = {t.name for t in await server.list_tools()}
        session_key = session_key_from_headers(ctx.headers)
        enabled = enabled_intents(session_key, default_intent=default_intent)

        entries: list[dict[str, Any]] = []
        for preset_name, buckets in INTENT_PRESETS.items():
            if buckets:
                count = sum(1 for name in registered if classify_tool(name) in buckets)
            else:
                count = len(registered)  # "admin" -- no filter
            entries.append(
                {
                    "name": preset_name,
                    "kind": "bucket_preset",
                    "tool_count": count,
                    "enabled": preset_name in enabled,
                }
            )
        for preset_name, tool_names in _TOOL_NAME_PRESETS.items():
            entries.append(
                {
                    "name": preset_name,
                    "kind": "tool_name_preset",
                    "tool_count": len(tool_names & registered),
                    "enabled": preset_name in enabled,
                }
            )
        return {"intents": entries}

    @server.tool(
        name="enable_session_intent",
        description=(
            "Grow this session's visible tool surface by enabling an "
            "additional session-intent preset or raw capability-bucket id. "
            "Additive and idempotent -- sessions only grow, there's no "
            "disable. Call `list_session_intents()` first to see the "
            "available names. Not an authorization change: a tool that "
            "isn't yet visible via `tools/list` may still exist server-side "
            "if a caller already knows its name."
        ),
    )
    async def enable_session_intent(name: str, ctx: _Ctx) -> dict[str, Any]:
        session_key = session_key_from_headers(ctx.headers)
        try:
            await enable_intent_and_notify(session_key, name, notify=ctx.session.send_tool_list_changed)
        except DynamicIntentError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "enabled": sorted(enabled_intents(session_key, default_intent=default_intent))}
```

Now wire the call into `register_tools`. Find this block (immediately after `_apply_tool_wire_metadata(server, read_only_names)`, currently around line 7123, but locate it by content since Task 2 already edited the lines right after it):

```python
    _apply_tool_wire_metadata(server, read_only_names)
```

Insert the new conditional call **before** this line (i.e., before `_apply_tool_wire_metadata`, so it's grouped with the other `_register_X` calls and is unambiguously registered before the session-intent filter block that follows `_apply_tool_wire_metadata`):

```python
    if settings.dynamic_session_intent:
        _register_dynamic_session_intent(server, settings)

    _apply_tool_wire_metadata(server, read_only_names)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tools.py -k dynamic_session_intent -v`
Expected: all pass, including `test_dynamic_session_intent_tools_survive_a_narrow_static_intent`.

- [ ] **Step 5: Run mypy**

Run: `uv run mypy src/mcpg/tools.py`
Expected: `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/mcpg/tools.py tests/unit/test_tools.py
git commit -m "feat(tools): register list_session_intents / enable_session_intent

Registered before the existing session-intent filter block so
ALWAYS_KEEP membership (Task 1) actually keeps them alive under a
configured MCPG_SESSION_INTENT -- verified via a dedicated test, not
just asserted.

Advances roadmap row: 22"
```

---

## Task 8: Wire the middleware into `server.py`

**Files:**
- Modify: `src/mcpg/server.py:337-343`
- Test: `tests/unit/test_server.py`

**Interfaces:**
- Consumes: `mcpg.dynamic_session_intent.DynamicSessionIntentMiddleware` (Task 6).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_server.py` (adjust the exact server-construction helper to match whatever this test file already uses to build a `Settings`/call `build_server` or equivalent — follow the existing pattern in that file for constructing a test server; the assertion below is what matters):

```python
def test_dynamic_session_intent_middleware_registered_when_enabled() -> None:
    from mcpg.dynamic_session_intent import DynamicSessionIntentMiddleware

    settings = load_settings(
        {"MCPG_DATABASE_URL": _FIXTURE_DB_URL, "MCPG_DYNAMIC_SESSION_INTENT": "true"}
    )
    server = build_server(settings, database=_fake_database())  # match this file's existing fixture helpers
    assert any(isinstance(m, DynamicSessionIntentMiddleware) for m in server.middleware)


def test_dynamic_session_intent_middleware_absent_by_default() -> None:
    from mcpg.dynamic_session_intent import DynamicSessionIntentMiddleware

    settings = load_settings({"MCPG_DATABASE_URL": _FIXTURE_DB_URL})
    server = build_server(settings, database=_fake_database())
    assert not any(isinstance(m, DynamicSessionIntentMiddleware) for m in server.middleware)
```

(Read `tests/unit/test_server.py`'s existing imports and any `_fake_database()`/fixture helper before writing this — reuse whatever pattern the file already has for constructing a server without a live Postgres connection, the same way the rest of that file does. `AuditedMCPServer` exposes its middleware list as `server.middleware` per the `mcp` SDK's `MCPServer` constructor kwarg of the same name.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_server.py -k dynamic_session_intent_middleware -v`
Expected: FAIL — the flag-enabled case has no `DynamicSessionIntentMiddleware` instance in `server.middleware` yet.

- [ ] **Step 3: Implement**

In `src/mcpg/server.py`, change the `AuditedMCPServer` construction (currently lines 337-343):

```python
    server: AuditedMCPServer = AuditedMCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=make_lifespan(settings, db, lm, cm, ar),
        middleware=[TenantRoleContextMiddleware()],
    )
```

to:

```python
    middleware: list[Any] = [TenantRoleContextMiddleware()]
    if settings.dynamic_session_intent:
        from mcpg.dynamic_session_intent import DynamicSessionIntentMiddleware

        middleware.append(DynamicSessionIntentMiddleware())

    server: AuditedMCPServer = AuditedMCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=make_lifespan(settings, db, lm, cm, ar),
        middleware=middleware,
    )
```

(If `Any` isn't already imported in `server.py`, add `from typing import Any` — check the existing import block first; this module likely already imports it given its size.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_server.py -k dynamic_session_intent_middleware -v`
Expected: PASS.

- [ ] **Step 5: Run mypy**

Run: `uv run mypy src/mcpg/server.py`
Expected: `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/mcpg/server.py tests/unit/test_server.py
git commit -m "feat(server): wire DynamicSessionIntentMiddleware behind the opt-in flag

Advances roadmap row: 22"
```

---

## Task 9: Contract-test fixtures + snapshot regeneration

**Files:**
- Modify: `tests/contract/test_tool_surface_snapshot.py`, `tests/unit/test_about.py`
- Modify (generated): `tests/contract/tool_surface.snapshot.json`

**Interfaces:**
- No new production code — this task keeps the "maximal surface" contract fixtures honest now that a new opt-in flag exists.

- [ ] **Step 1: Update the two maximal-server fixtures**

In `tests/contract/test_tool_surface_snapshot.py`, `_build_maximal_server`'s `load_settings` call currently reads:

```python
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
        }
    )
```

Add the new flag:

```python
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
            "MCPG_DYNAMIC_SESSION_INTENT": "true",
        }
    )
```

Make the identical change to `tests/unit/test_about.py`'s `_registered_tool_names` helper (same `load_settings` dict shape) — this is the fixture `test_every_registered_tool_classifies_into_a_bucket` (Task 3's real enforcement mechanism) runs against; without this change, that test would never actually exercise the new tools' classification through its normal completeness sweep (Task 3's dedicated tests already cover it directly, but this keeps the generic completeness check meaningful for future tools too).

Leave `tests/contract/test_describe_tool.py`, `tests/contract/test_mcp_prompts.py`, and `tests/contract/test_mcp_resources.py`'s own separate `_build_maximal_server` copies untouched — they test unrelated concerns (single-tool description shape, prompts, resources) and don't depend on the full possible tool count.

- [ ] **Step 2: Run the about.py completeness test**

Run: `uv run pytest tests/unit/test_about.py -k classifies_into_a_bucket -v`
Expected: PASS (Task 3 already added the overrides these two new tools need).

- [ ] **Step 3: Regenerate the tool-surface snapshot**

Run: `MCPG_REGENERATE_TOOL_SNAPSHOT=1 uv run pytest tests/contract/test_tool_surface_snapshot.py -v`
Expected: the test SKIPs with a message like `Regenerated tool_surface.snapshot.json (256 tools). Commit the diff...`.

- [ ] **Step 4: Verify the diff is exactly the two new tools**

Run: `git diff tests/contract/tool_surface.snapshot.json | grep -E '^\+.*"name"' `
Expected: exactly two added `"name"` lines — `list_session_intents` and `enable_session_intent` — no other tool's schema changed. If anything else changed, stop and investigate before continuing (an unrelated tool-surface drift unrelated to this feature should not be silently folded into this commit).

- [ ] **Step 5: Re-run the snapshot test normally to confirm it now passes**

Run: `uv run pytest tests/contract/test_tool_surface_snapshot.py -v`
Expected: PASS (no `MCPG_REGENERATE_TOOL_SNAPSHOT` env var this time).

- [ ] **Step 6: Commit**

```bash
git add tests/contract/test_tool_surface_snapshot.py tests/unit/test_about.py tests/contract/tool_surface.snapshot.json
git commit -m "test: include MCPG_DYNAMIC_SESSION_INTENT in maximal-surface fixtures

Regenerates tool_surface.snapshot.json: 254 -> 256 tools (the two new
dynamic session-intent meta-tools). Diff verified to contain exactly
those two additions.

Advances roadmap row: 22"
```

---

## Task 10: End-to-end parity, layered-composition, and default-unchanged tests

**Files:**
- Test: `tests/unit/test_tools.py` (or `tests/contract/`, if you judge these belong with the other full-server contract checks — either location is acceptable; keep them together in one file)

**Interfaces:**
- Consumes: `register_tools`, `load_settings` (already used throughout this plan).

- [ ] **Step 1: Write the tests**

Add to `tests/unit/test_tools.py`:

```python
async def test_default_settings_tool_surface_is_unchanged() -> None:
    """Parity test: with every new flag unset, the registered surface must
    be byte-for-byte the same 254 tools as before this feature existed --
    neither of the two new meta-tools, and no change to any existing
    tool's registration."""
    settings = load_settings({"MCPG_DATABASE_URL": _FIXTURE_DB_URL})
    server: MCPServer = MCPServer("mcpg-default-parity-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" not in names
    assert "enable_session_intent" not in names
    assert len(names) == 254


async def test_static_session_intent_only_is_unchanged() -> None:
    """Layer 2 disabled, Layer 1 alone: must match session_intent.py's
    pre-existing, already-tested behavior exactly -- every surviving
    tool classifies into one of `lookup`'s buckets, or is one of the
    two always-kept introspection tools, with no other tool escaping
    the filter."""
    from mcpg.about import classify_tool
    from mcpg.session_intent import INTENT_PRESETS

    settings = load_settings(
        {"MCPG_DATABASE_URL": _FIXTURE_DB_URL, "MCPG_SESSION_INTENT": "lookup"}
    )
    server: MCPServer = MCPServer("mcpg-static-only-fixture")
    register_tools(server, settings)
    names = {t.name for t in await server.list_tools()}
    assert "list_session_intents" not in names  # Layer 2 never enabled
    for name in names:
        assert classify_tool(name) in INTENT_PRESETS["lookup"] or name in {"describe_self", "describe_tool"}


async def test_layered_composition_dynamic_cannot_exceed_static_ceiling() -> None:
    """The test that proves the Layer 1/Layer 2 ceiling relationship
    (spec section 3 and section 6) is real, not just described.

    MCPG_SESSION_INTENT=lookup registers only the lookup-bucket tools
    (plus describe_self/describe_tool/the two meta-tools). Enabling a
    dynamic intent whose tools were never registered under that ceiling
    must reveal nothing new for them via visible_tool_names -- this
    tests the *resolver*, since exercising it through the live
    MCPServer's tools/list dispatch requires a running session; the
    resolver-level test is what Task 5/6 already establish, and this
    test wires it to a real, fully-registered server's tool set to
    confirm the intersection basis is correct end-to-end."""
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_SESSION_INTENT": "lookup",
            "MCPG_DYNAMIC_SESSION_INTENT": "true",
        }
    )
    server: MCPServer = MCPServer("mcpg-layered-fixture")
    register_tools(server, settings)
    registered = frozenset(t.name for t in await server.list_tools())

    from mcpg.dynamic_session_intent import enable_intent, visible_tool_names

    session_key = "layered-test-session"
    await enable_intent(session_key, "vector_rag")  # vector_rag was never registered under lookup
    visible = visible_tool_names(session_key, default_intent=("core",), registered=registered)
    assert visible <= registered  # never exceeds what Layer 1 left
    # vector_rag's own tools (e.g. vector_search) were never in `registered`
    # in the first place under MCPG_SESSION_INTENT=lookup, so they can't
    # appear in `visible` either -- the intersection makes this automatic.
    assert "vector_search" not in visible
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/unit/test_tools.py -k "parity or unchanged or layered_composition" -v`
Expected: all pass. If `test_default_settings_tool_surface_is_unchanged`'s `len(names) == 254` fails with a different number, that means something in this plan changed the default tool count — stop and investigate before proceeding; this is the single most important invariant this whole feature must preserve.

- [ ] **Step 3: Run the complete unit suite**

Run: `uv run pytest tests/unit -v` (this is the same suite the pre-commit hook runs — running it explicitly here first avoids a slow surprise at commit time)
Expected: all pass, 2 new tests added on top of whatever count existed before this plan.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_tools.py
git commit -m "test: parity + layered-composition coverage for dynamic session-intent

Advances roadmap row: 22"
```

---

## Task 11: Documentation

**Files:**
- Modify: `docs/user-guide.md`
- Modify: `docs/feature-shortlist.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add a user-guide section**

Read `docs/user-guide.md`'s existing structure first (find where `MCPG_SESSION_INTENT` is already documented, if it is — search for `session_intent` or `SESSION_INTENT` in that file) and add a new subsection immediately after it, titled something like "Dynamic session intent (opt-in runtime growth)". Cover, plainly:

- What `MCPG_SESSION_INTENT=core` gets you today (the new preset, no new flag needed): ~14 tools instead of 254.
- What `MCPG_DYNAMIC_SESSION_INTENT=1` adds on top: a session starts at `core` (or the configured static intent) and can grow via `list_session_intents()` / `enable_session_intent(name)`, without a restart, without affecting other sessions.
- The security note from spec §7, verbatim in substance: this is visibility only, not an authorization boundary — `MCPG_ACCESS_MODE` is unaffected and remains the real permission gate.
- The Layer 1/Layer 2 relationship: if both are configured, the dynamic layer can only ever reveal tools the static filter already left registered.

- [ ] **Step 2: Add the roadmap section**

In `docs/feature-shortlist.md`, add a new `## 22. Dynamic session intent` section (the next free number, confirmed against the file's current sections during planning) with two rows:

- `22.1` — the `"core"` preset + two-set intent resolution (Layer 1, Tasks 1-3).
- `22.2` — the opt-in runtime-growth layer (Layer 2, Tasks 4-9).

Mark both `✅ Shipped` once this plan's tasks are all merged, following the same status-marking convention section 21 (Docker MCP Registry submission) already uses in this file. Note explicitly that this extends roadmap row 8.8 rather than being a freestanding feature — mirror how section 21 cross-references things it builds on, if it does; otherwise just state it plainly in this section's own text.

- [ ] **Step 3: Add a CHANGELOG entry**

Under `[Unreleased]` in `CHANGELOG.md`, add an entry describing the new `"core"` preset and the `MCPG_DYNAMIC_SESSION_INTENT` flag, matching this file's existing entry style (see the two entries added for the Docker MCP Registry submission as a formatting reference).

- [ ] **Step 4: Commit**

```bash
git add docs/user-guide.md docs/feature-shortlist.md CHANGELOG.md
git commit -m "docs: document dynamic session intent (roadmap 22)

Advances roadmap row: 22"
```

---

## Final check before opening a PR

- [ ] Run the full suite one more time: `uv run pytest tests/unit tests/contract -v` (budget several minutes).
- [ ] Run `uv run ruff check . && uv run ruff format --check .`
- [ ] Run `uv run mypy src/mcpg`
- [ ] Confirm `git log --oneline` shows one commit per task above, each with `Advances roadmap row: 22`.
- [ ] Push via the normal branch → PR flow (squash-merge, per this repo's convention) — do not push directly to `main`.
