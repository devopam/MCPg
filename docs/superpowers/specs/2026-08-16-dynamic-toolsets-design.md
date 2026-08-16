# Opt-in dynamic tool loading (dynamic toolsets)

Status: approved for implementation planning
Date: 2026-08-16
Roadmap: planned as a new section in `docs/feature-shortlist.md` (next
free number is 22) — added when the implementation plan lands.

## 1. Problem

MCPg registers **254 MCP tools**. Every `tools/list` response — and,
depending on the client, every turn's context — carries the full
schema set: measured directly from
`tests/contract/tool_surface.snapshot.json`, that's **241,770 bytes,
~60,442 tokens**, averaging 952 bytes/tool with several outliers at
2,000-2,900 bytes (`create_pg_search_index`,
`monitor_embedding_drift`, `pg_search_run`, `translate_nl_to_sql`,
among others).

No client or reviewer has actually complained about this (verified,
not assumed — this was checked directly against the live Docker
registry PR before starting this design). This is proactive: the cost
is real and quantified, and other MCP servers (GitHub's, notably) have
already shipped a fix for the same shape of problem, so it's worth
addressing before it becomes a complaint.

### Why not just curate a smaller default and hide the rest?

Rejected explicitly (see prior discussion). That approach treats a
symptom — trims the number shown — without addressing the actual
constraint (a client that wants a capability MCPg has must still be
able to reach it), and it inverts MCPg's existing default-open
posture for no forcing reason. The design below keeps the full
254-tool surface as the unconditional default and makes any
reduction strictly **opt-in**.

## 2. Goals / non-goals

**Goals**
- Let an operator who wants a smaller initial tool surface opt into
  one, per-session, without restarting the server or affecting other
  sessions.
- Zero behavior change for anyone who doesn't opt in — byte-for-byte
  identical `tools/list` output to today.
- Reuse the existing capability-bucket taxonomy (`src/mcpg/about.py`)
  rather than inventing a second classification system.
- Keep the mechanism visibility-only — it must not become a second,
  informally-specified authorization layer alongside
  `MCPG_ACCESS_MODE` / `Capability` gating.

**Non-goals (v1)**
- Changing the default. Full 254-tool surface stays the default for
  every transport.
- A semantic/embedding-based `discover_tools(intent)` meta-tool.
  Rejected earlier in this design's discussion: adds a new dependency,
  non-deterministic retrieval, and isn't needed when ~19 named groups
  already cover the space discretely.
- Splitting MCPg into multiple server binaries/images (`mcpg-core`,
  `mcpg-readonly`, ...). Explicit YAGNI — the single binary plus an
  env var already gives all the flexibility this problem needs.
- Consolidating many tools behind one `action`-routed dispatcher tool
  per domain (evaluated against a Reddit post proposing this pattern
  for REST-wrapper MCP servers). Doesn't reduce MCPg's actual
  quantified cost (schema bytes, not tool count), and trades away the
  per-tool schema precision the SQL-safety kernel's design already
  depends on.

## 3. Architecture

Session-scoped **response filtering** on `tools/list`, not registry
mutation. The full tool set is always registered with the SDK's
`MCPServer`; a middleware narrows what each session's `tools/list`
call actually returns, based on which named groups that session has
enabled. `tools/call` is never filtered — see §7 on why that's
intentional.

This mirrors the repo's existing `TenantRoleContextMiddleware` /
`_TenantRoleMiddleware` pattern (`src/mcpg/tenancy.py`,
`src/mcpg/http_runtime.py`) almost exactly: an ASGI-level middleware
reads a per-connection identifier from the request and stashes it on
the ASGI `scope`; a `ServerMiddleware` running inside the SDK's
per-request dispatch task reads it back and acts on it. The one
structural difference from tenancy: tenancy's `current_role` is
correctly *stateless* per request (the role header is resent every
call, so a fresh `ContextVar.set()`/`.reset()` per request is
sufficient). Dynamic toolsets need to *remember* which groups a
session enabled across multiple subsequent `tools/list` calls, so this
needs actual per-session state, not just a request-scoped
`ContextVar`.

That state is keyed by the transport's own session identifier:
`mcp.server.streamable_http.MCP_SESSION_ID_HEADER`
(`"mcp-session-id"`), the same ID the SDK already assigns and the
client already echoes back on every request per the MCP streamable-http
spec (confirmed by reading `mcp/server/streamable_http.py` directly —
`self.mcp_session_id`, `_get_session_id(request)`). stdio has no such
header; it's inherently single-session-per-process, so it uses one
fixed sentinel key, consistent with how `current_role` already
no-ops distinctly on stdio.

## 4. Components

### `src/mcpg/dynamic_toolsets.py` (new module)

- `GROUPS: dict[str, frozenset[str]]` — maps a group name to the set
  of **tool names** it exposes (one representation for every group,
  so the filter step never has to special-case one kind of group
  against another). Built at import time from two sources:
  - `"core"`: not bucket-derived. The union of `headline_tools` from
    the `schema_introspection` and `query_execution` `Capability`
    entries (~12 tools) — reusing curation that's already
    hand-maintained and contract-tested, rather than picking a new
    list by hand.
  - One entry per bucket id in `about.py`'s `BUCKET_IDS` — all 19,
    including `schema_introspection` and `query_execution` — name
    equal to the bucket id, value equal to every registered tool name
    for which `classify_tool(name) == bucket_id`. This deliberately
    overlaps with `core`: `core` exposes only the headline subset of
    `schema_introspection`/`query_execution` up front, and enabling
    the `schema_introspection` or `query_execution` group later adds
    the rest of that bucket's tools (set union — enabling a group
    already covered by `core` is harmless, not an error). No new
    taxonomy — every group beyond `core` is a thin pass-through over
    what already exists in `about.py`.
  - `core` is always enabled and cannot be disabled.
- `_session_groups: dict[str, set[str]]` plus an `asyncio.Lock` —
  per-session enabled-group state. Entries are evicted when the SDK
  signals session teardown (see Testing — needs confirming which SDK
  hook fires this; if none exists cleanly, fall back to a bounded
  LRU/TTL cache rather than an unbounded dict, to avoid a slow memory
  leak from abandoned sessions on long-lived `streamable-http`
  deployments).
- `visible_tool_names(session_key: str) -> frozenset[str]` — union of
  `GROUPS["core"]` (unconditionally) and `GROUPS[g]` for every group
  in that session's `_session_groups` entry. A session_key with no
  entry yet (brand new session) behaves identically to one with an
  empty set — `core` is implicit, never stored, so there's no
  "initialize to `{core}`" step to get wrong.
- `enable_group(session_key: str, group: str) -> None` — validates
  `group in GROUPS`, mutates the session's set. Raises a
  `DynamicToolsetError` (clear, user-facing message) on an unknown
  group name.

### Session-id capture (ASGI layer)

A small ASGI middleware, added next to `_TenantRoleMiddleware` in
`src/mcpg/http_runtime.py`, reads the `mcp-session-id` request header
and stashes it on `scope` under a new scope key — same shape as
`_ROLE_SCOPE_KEY`. Only registered when `MCPG_DYNAMIC_TOOLSETS` is
set; zero cost otherwise.

### `DynamicToolsetsMiddleware(ServerMiddleware)`

Registered only when the feature is enabled. On every request:
reads the session key off `ctx.request.scope` (falling back to the
stdio sentinel when `ctx.request` is `None`, matching
`TenantRoleContextMiddleware`'s stdio branch). For `tools/list`
specifically: calls `call_next(ctx)` to get the full result from the
SDK, then filters `result.tools` to those whose name is in
`visible_tool_names(session_key)`, **plus** an always-visible
allowlist: the two new meta-tools below, `get_server_info`, and
`describe_self` (an agent must always be able to ask "what else is
there" and "what does mcpg do"). All other request kinds pass through
untouched.

### Two new meta-tools

Registered by `register_tools()` only when `MCPG_DYNAMIC_TOOLSETS` is
enabled (same conditional-registration pattern the ~30 existing
`_register_X(server)` sub-functions already use):

- `list_tool_groups()` — returns each group's name, one-line
  description (reusing `Capability.summary` for bucket-backed groups),
  tool count, and whether it's currently enabled for this session.
- `enable_tool_group(group: str)` — calls
  `dynamic_toolsets.enable_group(session_key, group)`, then fires
  `notifications/tools/list_changed` via
  `ctx.session.send_tool_list_changed()` (confirmed real:
  `mcp.server.session.ServerSession.send_tool_list_changed`, reachable
  off `ServerRequestContext.session`). Idempotent: enabling an
  already-enabled group is a no-op, not an error.

There is no `disable_tool_group` in v1 — sessions only grow their
visible surface. Simpler, and matches GitHub's own dynamic-toolsets
UX (their docs don't offer a disable path either).

### Config

`MCPG_DYNAMIC_TOOLSETS` — boolean env var, default unset/false,
documented in `src/mcpg/config.py` next to the existing
`MCPG_ALLOW_*` flags. Unlike those, this doesn't gate a
`Capability` — it's a presentation-layer switch, not an authorization
one (see §7), so it lives in config as its own concern.

## 5. Data flow

1. Operator sets `MCPG_DYNAMIC_TOOLSETS=1` and starts MCPg.
2. Client connects (streamable-http/sse: gets an `Mcp-Session-Id`;
   stdio: uses the sentinel key). Session starts with only `core`
   enabled.
3. Client calls `tools/list` → sees `core`'s ~12 tools +
   `list_tool_groups` + `enable_tool_group` + `get_server_info` +
   `describe_self` (~16 tools total, vs. 254 today).
4. Client calls `list_tool_groups()` to see what else exists, then
   `enable_tool_group("vector_search")`.
5. Server fires `notifications/tools/list_changed`.
6. Client re-issues `tools/list` → now also sees every tool in the
   `vector_search` bucket.
7. Every step above is scoped to that one session's key. A second,
   concurrent session on the same server process starts fresh at
   `core` and is unaffected — this is the property that made the
   `ServerMiddleware` approach viable at all (verified against
   GitHub's own dynamic-toolsets docs, which state explicitly:
   "Enabled state is per session and does not leak across sessions or
   integrations" — the same guarantee this design targets).

## 6. Testing

- **Parity test (the most important one):** with
  `MCPG_DYNAMIC_TOOLSETS` unset, `tools/list` output is byte-identical
  to the current `tool_surface.snapshot.json` baseline. This is what
  makes "default unchanged" a checked fact, not a claim.
- Enabled-mode initial `tools/list` returns exactly the core set +
  meta-tools + always-visible tools — no more, no less.
- `enable_tool_group` expands the visible set on the next `tools/list`
  and fires `list_changed` exactly once per call.
- Unknown group name → clean `DynamicToolsetError`, not a stack trace.
- Two concurrent sessions (two different `Mcp-Session-Id` values
  against one running server) have independent enabled-group state —
  the concurrency/isolation property this whole design exists to get
  right, so it needs a real test, not just an architectural argument.
- Every tool in `about.py`'s `BUCKET_IDS` is reachable through exactly
  one `GROUPS` entry (a completeness check — reuses the existing
  `classify_tool` contract test's guarantee that every tool has a
  bucket, extended to confirm every bucket has a group).

## 7. Security note (must be documented, not just built)

This is a **visibility** filter, not an **authorization** boundary.
A client that already knows a filtered-out tool's name and schema
(from a prior session, from reading MCPg's docs, from the Docker
registry's `tools.json`) can still call it directly — `tools/call` is
intentionally not filtered by this middleware. The real authorization
boundary remains what it already is: `MCPG_ACCESS_MODE` /
`Capability` gating in `src/mcpg/policy.py`, which this feature does
not touch and must not be confused with. This mirrors how GitHub
frames their own `--dynamic-toolsets` mode and is the correct
framing here too — document it plainly in the user-guide section this
feature adds, so nobody mistakes "smaller tool list" for "narrower
permissions."

## 8. Parallel, independent workstream: description trimming

Regardless of whether/when dynamic toolsets ships, the heaviest tool
descriptions (2,000-2,900 bytes: `create_pg_search_index`,
`monitor_embedding_drift`, `record_efficiency_observation`,
`pg_search_more_like_this`, `translate_nl_to_sql`,
`hybrid_bm25_vector_search`, `detect_vector_outliers`, `pg_search_run`,
`retrieve_with_context`, `generate_graph_projection`) are worth
trimming unconditionally — it lowers the ~60K-token baseline cost for
every deployment, including ones that never opt into dynamic
toolsets, with no architectural risk. Track as a separate roadmap row
and a separate PR from the dynamic-toolsets work above; don't block
either on the other.

## 9. Rollout

1. Land `dynamic_toolsets.py` + middleware + meta-tools + config flag,
   behind `MCPG_DYNAMIC_TOOLSETS`, default off. Parity test proves
   zero behavior change for existing users.
2. Document in `docs/user-guide.md`: what the flag does, the group
   list, the §7 security note.
3. Add the roadmap section (`docs/feature-shortlist.md` §22) and a
   `CHANGELOG.md [Unreleased]` entry when the implementation PR opens.
4. Description-trimming (§8) proceeds independently, whenever
   convenient — no ordering dependency on 1-3.
