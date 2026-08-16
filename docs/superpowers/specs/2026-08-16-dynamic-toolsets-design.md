# Opt-in dynamic tool loading (dynamic session intent)

Status: approved for implementation planning (revised after discovering
prior art — see §0)
Date: 2026-08-16
Roadmap: planned as a new section in `docs/feature-shortlist.md` (next
free number is 22) — added when the implementation plan lands.
Extends: roadmap 8.8 (`src/mcpg/session_intent.py`), not a
freestanding feature.

## 0. Revision note — reconciling with existing prior art

The first pass of this spec proposed a standalone `dynamic_toolsets.py`
module with its own `GROUPS` vocabulary. That was wrong: MCPg already
ships **roadmap 8.8**, `src/mcpg/session_intent.py` —
`MCPG_SESSION_INTENT`, a launch-time, process-wide tool-surface filter
over the same `about.py` bucket taxonomy, with five presets
(`lookup`/`migration`/`vector_rag`/`monitor`/`admin`) and an
always-keep set (`describe_self`, `describe_tool`). A new parallel
module would have meant three overlapping vocabularies (buckets,
intent presets, "groups") over the same 19 ids. Caught before writing
the implementation plan, via `advisor()` — not caught during the
original brainstorming pass, which is itself worth noting: "explore
project context" needs to be more thorough next time a feature touches
tool-surface shaping.

**The discriminating measurement** (computed directly against
`tests/contract/tool_surface.snapshot.json` and the live presets):

| Surface | Tools | Bytes | ~Tokens |
|---|---|---|---|
| Full (today's default) | 254 | 250,958 | ~62,740 |
| `MCPG_SESSION_INTENT=lookup` (shipped, zero new code) | 56 | 47,168 | ~11,792 |
| Headline-tools-based ("core", new) | 14 | 12,819 | ~3,204 |

Two conclusions from this, both load-bearing for the design below:

1. The existing presets already deliver a substantial (81%) reduction
   with a one-line env var and no new code. Any new work must justify
   itself against that baseline, not against the unfiltered 254.
2. The gap between preset-granularity (bucket-level, 56-128 tools) and
   headline-granularity (~14 tools) is real — about 4x tighter — so
   there is still a genuine, non-trivial win available. It just isn't
   big enough to justify a parallel system; it's a **new preset**
   plus a **runtime extension of the existing mechanism**.

Decision (user-approved): build both, unified into the existing
`session_intent` vocabulary. No `dynamic_toolsets.py`, no `GROUPS`, no
new preset concept — a new preset entry and a companion module that
extends the existing one.

## 1. Problem

Same as before: MCPg's 254-tool surface costs ~60K tokens on every
`tools/list`. §0 above narrows the actual delta this feature needs to
close: from the shipped ~12K-token `lookup` baseline down to the
~3K-token headline baseline, plus the ability to grow that surface
mid-session without a restart (the one thing `session_intent` cannot
do — it's launch-time by explicit design, per its own roadmap note:
removing tools from the registry before the first `tools/list` is
"the only way to make them truly invisible," a security argument for
*that* feature which does not bind this one — see §7).

### Why not just curate a smaller default and hide the rest?

Still rejected, same reasoning as before: treats a symptom, inverts
MCPg's default-open posture for no forcing reason. Full 254-tool
surface stays the unconditional default.

## 2. Goals / non-goals

**Goals**
- Add a `"core"` preset to the existing `INTENT_PRESETS` vocabulary —
  headline-tools-based, ~14 tools — usable statically via
  `MCPG_SESSION_INTENT=core` today, no new module required for this
  part alone.
- On top of that, let an operator opt a session into **runtime
  growth**: start at `core` (or whatever `MCPG_SESSION_INTENT`
  resolved to), grow to additional presets/buckets mid-session,
  without a restart, without affecting other concurrent sessions.
- Reuse `session_intent.py`'s presets, resolution logic
  (`resolve_intent`), and always-keep set. Do not invent a second
  classification vocabulary.
- Keep the runtime piece visibility-only — not a second, informally
  specified authorization layer alongside `MCPG_ACCESS_MODE` /
  `Capability` gating.

**Non-goals (v1)**
- Changing the default. Full 254-tool surface stays the default.
- Changing `session_intent.py`'s existing launch-time behavior for
  anyone not opting into the new runtime layer.
- A semantic/embedding-based `discover_tools(intent)` meta-tool —
  rejected earlier: new dependency, non-deterministic retrieval, not
  needed when named presets already cover the space discretely.
- Splitting MCPg into multiple server binaries/images. YAGNI — env
  vars already give this flexibility.
- Per-domain `action`-routed dispatcher-tool consolidation (evaluated
  against a Reddit post). Doesn't address the actual quantified cost
  and trades away schema precision the SQL-safety kernel depends on.

## 3. Architecture

Two independent layers, composed:

**Layer 1 — static (extends shipped code).** Add `"core"` to
`session_intent.INTENT_PRESETS`: the union of `headline_tools` from
the `schema_introspection` and `query_execution` `Capability` entries
in `about.py`, plus the module's existing always-keep set. This alone
is usable today via `MCPG_SESSION_INTENT=core` — no new module, no new
middleware, ships independently of Layer 2.

**Layer 2 — dynamic (new, opt-in, additive).** Session-scoped
**response filtering** on `tools/list` — not registry mutation. A new
`ServerMiddleware`, enabled only via `MCPG_DYNAMIC_SESSION_INTENT`,
narrows what each session's `tools/list` returns based on which
presets/buckets that session has enabled at runtime, starting from
whatever `session_intent`'s static filter already left registered.
Because `filter_server_tools` (Layer 1, if also configured) physically
removes tools from the SDK's registry before any session connects,
Layer 2 can only ever reveal tools that survived Layer 1 — a clean,
intentional ceiling: Layer 1 is the (optional) security-relevant hard
limit for the whole deployment, Layer 2 is a per-session, cost-driven
convenience within it. When Layer 1 isn't configured, Layer 2's
ceiling is the full 254-tool registry, same as today.

This mirrors the repo's existing `TenantRoleContextMiddleware` /
`_TenantRoleMiddleware` pattern (`src/mcpg/tenancy.py`,
`src/mcpg/http_runtime.py`): an ASGI-level middleware reads a
per-connection identifier from the request and stashes it on the ASGI
`scope`; a `ServerMiddleware` running inside the SDK's per-request
dispatch task reads it back and acts on it. The one structural
difference from tenancy: tenancy's `current_role` is correctly
*stateless* per request (the role header is resent every call, so a
fresh `ContextVar.set()`/`.reset()` per request suffices). Layer 2
needs to *remember* which presets a session enabled across multiple
subsequent `tools/list` calls, so it needs actual per-session state,
not just a request-scoped `ContextVar`.

That state is keyed by the transport's own session identifier:
`mcp.server.streamable_http.MCP_SESSION_ID_HEADER`
(`"mcp-session-id"`), the ID the SDK already assigns and the client
already echoes back on every request (confirmed by reading
`mcp/server/streamable_http.py` directly — `self.mcp_session_id`,
`_get_session_id(request)`). stdio has no such header; it's
inherently single-session-per-process, so it uses one fixed sentinel
key, consistent with how `current_role` already no-ops distinctly on
stdio.

## 4. Components

### `src/mcpg/session_intent.py` (existing module, additive changes)

**Corrected from an earlier draft of this section** — verified by
actually running the existing `filter_server_tools` logic against a
`core`-shaped bucket set before writing this: passing a tool-name set
through the current bucket-only keep-check
(`classify_tool(name) in allowed_buckets`) keeps **0 of the 14
expected tools**, not 14 — no tool's *bucket* is literally named
`"list_tables"` or `"run_select"`, so nothing matches. An `or` bolted
onto one predicate, as an earlier draft proposed, is not sufficient;
this needs two separate resolved sets, checked separately, never
unioned:

- New `_TOOL_NAME_PRESETS: dict[str, frozenset[str]]` — a second,
  small dict, kept deliberately separate from `INTENT_PRESETS` rather
  than widening that dict's value type. This is additive and
  non-breaking: `INTENT_PRESETS`'s existing five entries, its
  `dict[str, frozenset[str]]` public shape, and every existing caller
  are untouched. One entry: `"core"` → the union of `headline_tools`
  for the `schema_introspection` and `query_execution` `Capability`
  entries in `about.py` (~12 tool names).
- New `IntentResolution` (`typing.NamedTuple`, two fields:
  `buckets: frozenset[str]`, `tool_names: frozenset[str]`) and
  `resolve_intent(intent_values) -> IntentResolution | None` —
  resolves each entry against `INTENT_PRESETS` (bucket presets, as
  today, including the `admin` no-filter sentinel), then
  `_TOOL_NAME_PRESETS` (tool-name presets), then falls back to
  treating an unrecognized name as a raw bucket id (unchanged
  fallback behavior). `resolve_intent_to_buckets` stays exactly as it
  is today, unchanged, for any other caller that only wants the
  bucket half — it is not touched or reimplemented in terms of the
  new function, so its existing tests keep passing against unchanged
  code.
- `filter_server_tools` gains one new keyword parameter:
  `allowed_tool_names: frozenset[str] = frozenset()`. Keep-check
  becomes `name in always_keep or name in allowed_tool_names or
  classify_tool(name) in allowed_buckets` — three separate checks,
  not a merged set, so there is no cross-namespace ambiguity to reason
  about (an earlier draft of this section flagged a `vector_search`
  bucket-id/tool-name string collision as "checked, harmless" — with
  buckets and tool names never unioned into one set, that collision
  can't arise in the first place; that whole paragraph is gone, not
  just resolved). The default keeps every existing call site
  (including any test that calls `filter_server_tools(server,
  allowed)` positionally) behaviorally identical.
- The **one production call site**
  (`tools.py:7129-7134`) switches from `resolve_intent_to_buckets` +
  `filter_server_tools(server, allowed)` to `resolve_intent` +
  `filter_server_tools(server, resolution.buckets,
  allowed_tool_names=resolution.tool_names)`. This is the only place
  in the codebase that needs to change to make
  `MCPG_SESSION_INTENT=core` actually work.
- Export `_ALWAYS_KEEP` publicly as `ALWAYS_KEEP` (add to `__all__`;
  keep `_ALWAYS_KEEP` as an internal alias anywhere the module still
  refers to the private name, to avoid a churny rename). Layer 2
  reuses this directly rather than defining its own always-visible
  set — no more `get_server_info` vs. `describe_tool` divergence
  between static and dynamic paths.
- `about.py` needs one small addition: classify the two new
  meta-tools below into the `observability` bucket via
  `_TOOL_TO_BUCKET_OVERRIDES` (the existing contract test asserts
  every registered tool classifies to a bucket — these are new tools,
  so they need an entry). Add both names to `session_intent.ALWAYS_KEEP`
  too, so they survive even when a narrow preset (that doesn't include
  `observability`) is active — **verified this matters**: the static
  filter block runs *last* in `register_tools()`
  (`tools.py:7123-7134`, "Runs LAST so every tool that would otherwise
  be registered has already been added"), so as long as the plan
  registers the two new meta-tools *before* that block (grouped with
  the other `_register_X(server)` calls, not appended after), they are
  genuinely subject to the filter and `ALWAYS_KEEP` membership is
  what keeps them alive under e.g. `MCPG_SESSION_INTENT=lookup` — not
  a hypothetical, an ordering requirement the plan must state
  explicitly as a task constraint.

### `src/mcpg/dynamic_session_intent.py` (new, small companion module)

Deliberately a separate file from `session_intent.py`, not merged
into it: the existing module's responsibility is launch-time registry
mutation (synchronous, runs once at startup); this one's is
per-session runtime state and middleware (async, runs per request).
Different lifecycles, different failure modes — keeping them separate
files keeps each one holding one clear responsibility, per this
project's file-structure convention. This module imports
`resolve_intent` (not the older `resolve_intent_to_buckets`, which
only resolves the bucket half and would silently drop `"core"`'s
tools) and `ALWAYS_KEEP` from `session_intent` rather than redefining
anything.

- `_session_presets: dict[str, set[str]]` plus an `asyncio.Lock` —
  per-session enabled-preset-name state. Entries are evicted when the
  SDK signals session teardown; if no clean hook exists (needs
  confirming against `mcp.server.streamable_http_manager` during
  implementation), fall back to a bounded LRU/TTL cache rather than
  an unbounded dict, to avoid a slow memory leak on long-lived
  `streamable-http` deployments.
- `visible_tool_names(session_key: str, *, registered: frozenset[str]) -> frozenset[str]`
  — resolves the session's enabled preset names (defaulting to
  whatever `MCPG_SESSION_INTENT` was configured with, or `"core"` if
  that was unset, when the session has enabled nothing yet) via
  `resolve_intent` (both the bucket and tool-name halves) +
  `ALWAYS_KEEP`, same as the static path, then intersects with
  `registered` (the tool names the SDK's
  `MCPServer` actually still has — i.e. whatever Layer 1 left, or
  everything if Layer 1 wasn't configured). The intersection is what
  makes the Layer 1/Layer 2 ceiling relationship in §3 actually true
  in code, not just in the architecture description.
- `enable_intent(session_key: str, name: str) -> None` — validates
  `name` is a known bucket preset, a known tool-name preset, or a raw
  bucket id present in `about.BUCKET_IDS` (mirroring `resolve_intent`'s
  existing fallback for unrecognized names — an agent that needs a
  bucket with no
  matching preset, e.g. one not covered by any of the five presets,
  can still reach it directly). Mutates the session's set. Raises
  `DynamicIntentError` (clear, user-facing message) on an unrecognized
  name.
- `DynamicSessionIntentMiddleware(ServerMiddleware)` — reads the
  session key off `ctx.request.scope` (stdio sentinel when
  `ctx.request` is `None`, matching `TenantRoleContextMiddleware`'s
  stdio branch). For `tools/list` specifically: calls `call_next(ctx)`
  to get the (possibly already Layer-1-filtered) result, then filters
  `result.tools` to `visible_tool_names(session_key, registered=...)`.
  All other request kinds pass through untouched.

### Session-id capture (ASGI layer)

A small ASGI middleware, added next to `_TenantRoleMiddleware` in
`src/mcpg/http_runtime.py`, reads the `mcp-session-id` request header
and stashes it on `scope` under a new scope key — same shape as
`_ROLE_SCOPE_KEY`. Only registered when `MCPG_DYNAMIC_SESSION_INTENT`
is set; zero cost otherwise.

### Two new meta-tools

Registered by `register_tools()` only when `MCPG_DYNAMIC_SESSION_INTENT`
is enabled (same conditional-registration pattern the ~30 existing
`_register_X(server)` sub-functions already use):

- `list_session_intents()` — returns every preset name (including
  `"core"`), a one-line description, resolved tool count, and whether
  it's currently enabled for this session. Also notes that a raw
  bucket id (`about.BUCKET_IDS`) is accepted anywhere a preset name
  is.
- `enable_session_intent(name: str)` — calls
  `dynamic_session_intent.enable_intent(session_key, name)`, then
  fires `notifications/tools/list_changed` via
  `ctx.session.send_tool_list_changed()` (confirmed real:
  `mcp.server.session.ServerSession.send_tool_list_changed`, reachable
  off `ServerRequestContext.session`). Idempotent: enabling an
  already-enabled name is a no-op, not an error.

No `disable_session_intent` in v1 — sessions only grow their visible
surface. Simpler, and matches GitHub's own dynamic-toolsets UX (no
disable path there either).

### Config

`MCPG_DYNAMIC_SESSION_INTENT` — boolean env var, default unset/false,
documented in `src/mcpg/config.py` next to `MCPG_SESSION_INTENT` and
the `MCPG_ALLOW_*` flags. Unlike `MCPG_ALLOW_*`, this doesn't gate a
`Capability` — it's a presentation-layer switch, not an authorization
one (see §7).

## 5. Data flow

1. Operator sets `MCPG_DYNAMIC_SESSION_INTENT=1` (optionally alongside
   `MCPG_SESSION_INTENT=...` for a static ceiling; if unset, the
   ceiling is the full 254-tool registry).
2. Client connects. Session starts with `core` resolved (or whatever
   `MCPG_SESSION_INTENT` was set to, if set) — nothing new enabled
   yet.
3. Client calls `tools/list` → sees `core`'s ~14 tools +
   `list_session_intents` + `enable_session_intent` (~16 total, vs.
   254 today, vs. ~56 with only the static `lookup` preset).
4. Client calls `list_session_intents()`, then
   `enable_session_intent("vector_rag")`.
5. Server fires `notifications/tools/list_changed`.
6. Client re-issues `tools/list` → now also sees every tool in the
   `vector_rag` preset's buckets (intersected with whatever Layer 1
   left registered, if Layer 1 is configured).
7. Every step is scoped to that session's key. A second, concurrent
   session on the same process starts fresh and is unaffected —
   verified against GitHub's own dynamic-toolsets docs: "Enabled
   state is per session and does not leak across sessions or
   integrations," the same guarantee this design targets.

## 6. Testing

- **Parity test (the most important one):** with
  `MCPG_DYNAMIC_SESSION_INTENT` unset, `tools/list` output is
  byte-identical to the current `tool_surface.snapshot.json` baseline
  — and, separately, with only `MCPG_SESSION_INTENT` set (Layer 1
  alone), output matches today's already-tested `filter_server_tools`
  behavior unchanged. Two parity tests, not one — Layer 2 must not
  perturb Layer 1's existing, shipped behavior.
- `resolve_intent(("core",))` returns an `IntentResolution` whose
  `tool_names` half is exactly the expected 12 headline-derived names
  and whose `buckets` half is empty (locks the headline-derived set so
  a future `headline_tools` edit in `about.py` is a visible, deliberate
  diff here, not a silent surface change) — and
  `filter_server_tools(server, resolution.buckets,
  allowed_tool_names=resolution.tool_names)` against a fully
  registered server keeps exactly those 12 plus `ALWAYS_KEEP`'s 2 (14
  total), not 0. This is the regression test for the bug caught in
  §4 — it must fail against the earlier, unpatched keep-check.
- Every existing preset (`lookup`/`migration`/`vector_rag`/`monitor`/
  `admin`) still resolves and filters identically to before this
  change — `resolve_intent_to_buckets` and `filter_server_tools`
  called without `allowed_tool_names` are behaviorally untouched.
- Enabled-mode initial `tools/list` returns exactly `core` (or the
  configured static intent) + the two meta-tools + `ALWAYS_KEEP` — no
  more, no less.
- `enable_session_intent` expands the visible set on the next
  `tools/list` and fires `list_changed` exactly once per call.
- `enable_session_intent` accepts a raw bucket id, not just a preset
  name (mirrors `resolve_intent`'s existing unrecognized-name
  fallback).
- Unrecognized name → clean `DynamicIntentError`, not a stack trace.
- **Layered composition:** `MCPG_SESSION_INTENT=lookup` +
  `MCPG_DYNAMIC_SESSION_INTENT=1` together — session starts at `core`
  intersected with `lookup`'s registered set, and
  `enable_session_intent("vector_rag")` reveals nothing (since
  `vector_rag`'s buckets were never registered under the `lookup`
  ceiling) rather than erroring or silently exceeding the ceiling.
  This is the test that proves the §3 ceiling claim is real, not
  aspirational.
- Two concurrent sessions (two different `Mcp-Session-Id` values
  against one running server) have independent enabled-preset state —
  the concurrency/isolation property this design exists to get right.
- The two new meta-tools survive `filter_server_tools` under every
  existing preset (the `ALWAYS_KEEP` addition from §4 actually works).

## 7. Security note (must be documented, not just built)

Layer 2 is a **visibility** filter, not an **authorization** boundary.
A client that already knows a filtered-out tool's name and schema can
still call it directly — `tools/call` is intentionally not filtered by
`DynamicSessionIntentMiddleware`. This is unchanged from Layer 1's own
framing, except Layer 1 additionally achieves true invisibility via
registry removal (its explicit reason for being launch-time only, per
its roadmap note) — Layer 2 cannot make that claim, and must not be
described as if it could. The real authorization boundary remains
`MCPG_ACCESS_MODE` / `Capability` gating in `src/mcpg/policy.py`,
untouched by either layer. Document both properties plainly in the
user-guide section this feature adds, so nobody mistakes "smaller tool
list" for "narrower permissions," and nobody mistakes Layer 2 for
Layer 1's stronger guarantee.

## 8. Parallel, independent workstream: description trimming

Unchanged from the original spec. Regardless of whether/when this
feature ships, the heaviest tool descriptions (2,000-2,900 bytes:
`create_pg_search_index`, `monitor_embedding_drift`,
`record_efficiency_observation`, `pg_search_more_like_this`,
`translate_nl_to_sql`, `hybrid_bm25_vector_search`,
`detect_vector_outliers`, `pg_search_run`, `retrieve_with_context`,
`generate_graph_projection`) are worth trimming unconditionally — it
lowers the ~60K-token baseline for every deployment, including ones
that never touch either layer of this feature. Separate roadmap row,
separate PR, no ordering dependency.

## 9. Rollout

1. Land the `_TOOL_NAME_PRESETS["core"]` entry, the new
   `IntentResolution` / `resolve_intent` (additive — the existing
   `resolve_intent_to_buckets` is untouched), the new
   `allowed_tool_names` parameter on `filter_server_tools` (additive,
   defaulted, existing callers unaffected), the one production
   call-site switch at `tools.py:7129-7134`, and the `ALWAYS_KEEP`
   export. Usable immediately via `MCPG_SESSION_INTENT=core`. Every
   piece here is additive to shipped code except the one call-site
   switch — can ship on its own.
2. Land `dynamic_session_intent.py` + middleware + meta-tools + config
   flag, behind `MCPG_DYNAMIC_SESSION_INTENT`, default off. Parity
   tests (§6) prove zero behavior change for both existing-default and
   existing-`MCPG_SESSION_INTENT` users.
3. Document in `docs/user-guide.md`: the new preset, the flag, the
   Layer 1/Layer 2 relationship, the §7 security note.
4. Add the roadmap section (`docs/feature-shortlist.md` §22, noting it
   extends 8.8) and a `CHANGELOG.md [Unreleased]` entry when the
   implementation PR opens.
5. Description-trimming (§8) proceeds independently, whenever
   convenient.
