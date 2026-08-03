# MCPg → mcp 2.0 SDK Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate MCPg off the deprecated `mcp.server.fastmcp.FastMCP` API onto `mcp` 2.0.0's `mcp.server.mcpserver.MCPServer`, lifting the `<2` cap PR #290 shipped as a hotfix — while redesigning per-request tenancy onto the new context model and adding `ctx.elicit()` confirmation for every write-tier tool call.

**Architecture:** Nine self-contained slices, in dependency order: (1) unlock the dependency, (2) mechanical rename across every `fastmcp` touch point, (3–4) fix the two real call-site breaks (`call_tool` signature, HTTP app host/port), (5) redesign tenancy onto `ServerMiddleware` (a *simplification*, not just a port — see rationale below), (6) add the elicitation confirmation gate centrally in `call_tool` (zero changes to the 254 individual tool bodies), (7) mechanical test renames, (8) regenerate both contract snapshots and run the full suite, (9) docs + roadmap + changelog. All nine land in one PR per the maintainer's explicit choice, but each task is its own commit and its own reviewable gate.

**Tech Stack:** Python 3.12–3.14, `mcp[cli]>=2.0.0`, `uv`, `pytest`, `mypy --strict`, `ruff`.

## Global Constraints

- `mcp[cli]` floor becomes `>=2.0.0` (drop the `<2` cap from PR #290) — `pyproject.toml:53`.
- `uv.lock` must be regenerated and committed with every dependency change (`uv lock`); CI runs `uv sync --locked` (see `9ce468d`), so a stale lockfile fails CI regardless of code correctness.
- No hand-edits to `src/mcpg/_vendor/` (none of this migration touches it — the SQL kernel is unrelated to the MCP SDK).
- Every task's tests must pass via `uv run pytest tests/unit tests/contract -q` before that task's commit. Full suite currently: 2867 passed, 3 skipped (verified on `main` after PR #290).
- `uv run mypy src/mcpg` and `uv run ruff check` / `ruff format --check` must be clean before every commit (matches the local pre-commit hook, which runs all of the above plus bandit).
- `CHANGELOG.md` gets one `[Unreleased]` entry per task that changes behavior (not for pure mechanical renames — squash those into the task that introduces the behavior change, or the final docs task).
- PR body cites `Advances roadmap row: 20.1` (Task 9 adds this row to `docs/feature-shortlist.md`).

## Facts this plan relies on (verified against the actual mcp 2.0.0 wheel + mcp_types 2.0.0 wheel, not documentation)

- `@server.tool(...)` / `.resource()` / `.prompt()` decorator signatures are unchanged between `FastMCP` and `MCPServer` — the 254 `@server.tool(...)` call sites in `tools.py` need **zero** changes.
- `Context` drops one type parameter: old `Context[ServerSession, AppContext, Any]` (3 params) → new `Context[AppContext, Any]` (2 params, `LifespanContextT, RequestT`).
- `MCPServer.__init__` (`mcp/server/mcpserver/server.py:148-176`) has **no `host`/`port` kwargs** (removed from the constructor entirely) but **does** accept `version: str = ""` directly, which it forwards to the internal `Server(..., version=version)` (line ~206) — so `AuditedFastMCP.__init__`'s manual `self._mcp_server.version = version` patch becomes unnecessary; delete the whole override.
- `_mcp_server` is renamed `_lowlevel_server`; `.version` is now a read-only `@property` proxying `self._lowlevel_server.version` (`server.py:296-297`).
- `run(transport=...)`, `run_stdio_async()`, `streamable_http_app(host=, port=, ...)`, `sse_app(host=, ...)` all still exist with the same names. `host`/`port` moved from constructor-time to call-time (defaults `"127.0.0.1"`/`8000` in each method's signature, `server.py:1030-1097,1218-1228`) — used internally for the CORS `allowed_hosts`/`allowed_origins` on the returned Starlette app, not for socket binding (mcpg's `http_runtime.py` binds via uvicorn separately). mcpg currently calls `server.streamable_http_app()` / `server.sse_app()` with **no arguments** (`http_runtime.py:546,548`), which after migration would silently default the CORS allowlist to `127.0.0.1:8000` regardless of `settings.http_host`/`settings.http_port` — a real regression for any non-default bind. Must pass `host=`/`port=` explicitly.
- `MCPServer.call_tool(self, name, arguments, context: Context[LifespanResultT, Any] | None = None) -> CallToolResult | InputRequiredResult` (`server.py:498-504`) gained a third parameter and a new return type (was `Sequence[ContentBlock] | dict[str, Any]`, took 2 params). The JSON-RPC handler `_handle_call_tool` (`server.py:415-424`) calls `self.call_tool(params.name, params.arguments or {}, context)` — **3 positional args**. `AuditedFastMCP.call_tool(self, name, arguments)` (2 params, no `context`) would raise `TypeError: call_tool() takes 3 positional arguments but 4 were given` the instant a real request comes in. This is the one call site that **must** change or the server is completely broken, not just type-mismatched.
- Exceptions raised inside a tool function still propagate as real Python exceptions through `Tool.run()` (wrapped as `ToolError`, `tools/base.py:180-181`) → `ToolManager.call_tool()` (no catch, `tool_manager.py:87`) → `MCPServer.call_tool()` (no catch) — only `_handle_call_tool`, **one layer above** what `AuditedFastMCP` overrides, converts them to `CallToolResult(is_error=True)`. So mcpg's existing `try/except Exception as exc: ... audit(status="error") ... raise` pattern in `AuditedFastMCP.call_tool` (`server.py:96-104`) is **still correct as-is** — only the signature/return-type/super-call need updating, not the error-handling logic itself.
- `mcp.types.ContentBlock` (and everything else in `mcp.types`) still resolves — `mcp/types/__init__.py` is now `from mcp_types import *`, a byte-for-byte re-export mirror of the new standalone `mcp_types` package. `server.py`'s `from mcp.types import ContentBlock` needs **no change**.
- `request_ctx` (the old ambient `ContextVar` in `mcp.server.lowlevel.server` that `tenancy._role_from_request()` reads via `request_ctx.get()`) **does not exist anywhere in mcp 2.0** (grepped the full extracted wheel — zero matches). Every lowlevel request handler now receives an explicit `ServerRequestContext` parameter instead of pulling one from ambient state.
- **The replacement is a simplification, not just a re-plumb.** `mcp/shared/dispatcher.py:266`'s `Dispatcher.run()` docstring states plainly: *"Each inbound request is dispatched to `on_request` in its own task."* Old FastMCP dispatched all of a session's tool calls inside one long-lived per-session task (that's *why* tenancy.py needed the `request_ctx`/scope-reading workaround — a plain `ContextVar.set()` in an ASGI request task never reached the shared session task). Under mcp 2.0, since every request gets its own asyncio task, a plain `ContextVar.set()` done at the top of that task (in a `ServerMiddleware`, which runs "at the top of `ServerRunner._on_request`... wraps every inbound request", `mcp/server/context.py:146-194`) is now reliably visible to everything that request's task awaits — including a tool function and the `SqlDriver` calls underneath it — via ordinary Python contextvar-inheritance-within-a-task semantics. No sealed/AEAD `RequestStateSecurity` mechanism is needed for this (that exists for a different problem — carrying authenticated-principal state across the wire/process boundary — and would be over-engineering for same-process propagation).
- `ServerRequestContext.request: RequestT | None` (`mcp/server/context.py:47`) is populated from `dctx.message_metadata.request_context` when that metadata is a `ServerMessageMetadata` (`runner.py:314-316`) — this is the **exact same `ServerMessageMetadata.request_context` field** mcpg's own commit `b6436b6` already documented as "unchanged in 1.28.1". So the Starlette `Request` object tenancy needs (to read the `X-MCPG-Role`-derived scope key) is still there, just reachable via an explicit `ctx.request` parameter instead of an ambient contextvar getter.
- `ctx.elicit(message: str, schema: type[ElicitSchemaModelT]) -> ElicitationResult[ElicitSchemaModelT]` (`mcp/server/mcpserver/context.py:185-216`) is a real, usable API: `result.action` is `"accept" | "decline" | "cancel"`, `result.data` populated only on accept. A client's support is declared during `initialize` as `ClientCapabilities.elicitation.form`, readable via `context.request_context.session.client_capabilities.elicitation` (`mcp/server/session.py:65-77`); `mcp/server/mcpserver/resolve.py:676-680` shows the SDK's own capability-check pattern for this — replicate it rather than assuming every connected client supports elicitation.
- Every registered tool already carries a `readOnlyHint: bool` annotation, correctly derived per-tool from the exact same read/write/DDL/shell/listen/migrate capability gates that decide registration (`tools.py:6983-7025`, `_apply_tool_wire_metadata`). This is the existing, tested signal for "is this tool write-tier" — no new tool classification needs inventing for the elicitation gate.
- `mcp` 2.0 pulls new top-level deps: `mcp-types==2.0.0`, `httpx2>=2.5.0`, `opentelemetry-api>=1.28.0`. None conflict with mcpg's own explicit `httpx>=0.27` (different package, `httpx2` is new/additional) or the `mcpg[otel]` extra's `opentelemetry-api>=1.27` (mcp's floor of `>=1.28.0` is compatible — `uv lock` will resolve the higher-constrained version for both). Verify this holds during Task 1's `uv lock` — don't assume, check the resolved output.

---

### Task 1: Unlock the `mcp` 2.0 dependency

**Files:**
- Modify: `pyproject.toml:53`
- Modify: `uv.lock` (regenerated, not hand-edited)
- Test: manual verification (no automated test for a bare dependency bump — the full suite in Task 8 is the real gate)

**Interfaces:**
- Produces: a working `uv sync`'d environment on `mcp==2.0.0` that every later task builds on.

- [ ] **Step 1: Change the dependency floor**

In `pyproject.toml:53`, change:
```toml
    "mcp[cli]>=1.28.1,<2",
```
to:
```toml
    "mcp[cli]>=2.0.0",
```

- [ ] **Step 2: Regenerate the lockfile**

Run: `uv lock`
Expected: resolves `mcp==2.0.0` (or a later 2.x patch, whichever is current on PyPI at implementation time — pin whatever `uv lock` actually resolves, don't hand-pick a version). Confirm with:
```bash
grep -A2 '^name = "mcp"$' uv.lock | head -3
```
Expected output shows `version = "2.0.0"` (or later 2.x).

- [ ] **Step 3: Sync the dev environment and confirm the new deps don't conflict**

Run: `uv sync --locked`
Expected: resolves without error; inspect that `mcp-types`, `httpx2`, and `opentelemetry-api` all appear in the resolution with no version conflict reported. If `uv sync --locked` reports a conflict involving `opentelemetry-api` (mcpg's own `[otel]` extra pins `>=1.27`), bump mcpg's own floor in `pyproject.toml`'s `otel` extra (and the matching line in `[dependency-groups] dev`) to match whatever `uv lock` actually resolved — don't leave a floor that's already unsatisfiable.

- [ ] **Step 4: Confirm the SDK is now import-broken (expected — later tasks fix this)**

Run: `uv run python -c "import mcpg.server"`
Expected: **FAILS** with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` — this confirms Task 1 alone doesn't yet produce a working server (Tasks 2–3 do). Don't be alarmed by this failure; it's the expected, temporary state between tasks in this plan.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: unlock mcp 2.0 (lift the <2 cap from #290)"
```

---

### Task 2: Mechanical `fastmcp` → `mcpserver` rename (imports, type refs, prose)

**Files:**
- Modify: `src/mcpg/tools.py:14-15,102` + 63 occurrences of `FastMCP[AppContext]`
- Modify: `src/mcpg/session_intent.py:44,152`
- Modify: `src/mcpg/observability.py:27,122` (prose only)
- Modify: `src/mcpg/multidb.py:128` (prose only)
- Modify: `src/mcpg/prompts.py:10` (prose only)
- Modify: `src/mcpg/pg_search.py:899` (prose only)
- Modify: `src/mcpg/pg19_ddl.py:87` (prose only)
- Modify: `src/mcpg/__init__.py:10` (prose only, in the pydantic-warning-filter comment)
- Modify: `src/mcpg/http_runtime.py:3,531,534,617` (prose only — no import here)
- Test: `uv run mypy src/mcpg` (will still fail after this task alone — Tasks 3–4 fix the remaining two real breaks)

**Interfaces:**
- Consumes: nothing from Task 1 beyond the unlocked dependency.
- Produces: `_Ctx` type alias (`tools.py`) now `Context[AppContext, Any]`, importable by every tool-registration function unchanged (they only ever used the alias, never the 3-param spelling directly).

- [ ] **Step 1: Fix `tools.py`'s imports and `Context` alias**

In `tools.py:14-15`, change:
```python
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
```
to:
```python
from mcp.server.mcpserver import Context, MCPServer
```
(The `ServerSession` import is now dead — it was only used in the 3-param `Context` alias below.)

In `tools.py:102`, change:
```python
_Ctx = Context[ServerSession, AppContext, Any]
```
to:
```python
_Ctx = Context[AppContext, Any]
```

- [ ] **Step 2: Mechanically replace every `FastMCP[AppContext]` type annotation in `tools.py`**

This string is unambiguous — verified via `grep -c "FastMCP\[AppContext\]" src/mcpg/tools.py` → 63, and every other bare `FastMCP` mention in the file is prose (comments), not a type annotation (verified via `grep -n "FastMCP" src/mcpg/tools.py | grep -v "FastMCP\[AppContext\]"` → 4 comment lines, listed below). Run:
```bash
sed -i 's/FastMCP\[AppContext\]/MCPServer[AppContext]/g' src/mcpg/tools.py
```
Verify no occurrences remain:
```bash
grep -c "FastMCP\[AppContext\]" src/mcpg/tools.py
```
Expected: `0`.

- [ ] **Step 3: Update the 4 remaining prose comments in `tools.py`**

At `tools.py:101` ("The MCP request context FastMCP injects into every tool.") and `tools.py:314,316` (FastMCP-instance comments near `_register_server_info`) and `tools.py:5490` ("FastMCP auto-derives the ... dataclass"), replace the word "FastMCP" with "the MCP SDK" or "MCPServer" as locally accurate — these are non-functional comment edits, use judgment per-line rather than a blind sed (the surrounding sentence should still read naturally).

- [ ] **Step 4: Fix `session_intent.py`'s real import + type hint**

At `session_intent.py:44`, change:
```python
    from mcp.server.fastmcp import FastMCP
```
to:
```python
    from mcp.server.mcpserver import MCPServer
```
At `session_intent.py:152`, change the parameter type:
```python
    server: FastMCP,
```
to:
```python
    server: MCPServer,
```
Update the two prose comments at `session_intent.py:10,19,166` that say "FastMCP" (registry/registration prose) to "the MCP SDK" or "MCPServer" as locally accurate.

- [ ] **Step 5: Update remaining prose-only files**

In each of `observability.py:27,122`, `multidb.py:128`, `prompts.py:10`, `pg_search.py:899`, `pg19_ddl.py:87`, `__init__.py:10`, `http_runtime.py:3,531,534,617` — these have no import to fix, only comments/docstrings mentioning "FastMCP" or "AuditedFastMCP". Update each to say "MCPServer" / "AuditedMCPServer" (matching Task 3's rename) as locally accurate. None of these are functional changes; do them as one pass, verifying with:
```bash
grep -rn "fastmcp\|FastMCP" src/mcpg/*.py
```
Expected after this task: zero hits in every file **except** `tools.py` (the `_apply_tool_wire_metadata`/`register_tools` signatures — confirm these were caught by Step 2's sed) and `server.py` (Task 3 handles that file separately, next).

- [ ] **Step 6: Confirm no other consumers broke**

Run: `uv run ruff check src/mcpg/tools.py src/mcpg/session_intent.py src/mcpg/observability.py src/mcpg/multidb.py src/mcpg/prompts.py src/mcpg/pg_search.py src/mcpg/pg19_ddl.py src/mcpg/__init__.py src/mcpg/http_runtime.py`
Expected: clean (an unused import would be the likely failure mode — e.g. if `ServerSession` was imported elsewhere in `tools.py` beyond the one removed use).

- [ ] **Step 7: Commit**

```bash
git add src/mcpg/tools.py src/mcpg/session_intent.py src/mcpg/observability.py src/mcpg/multidb.py src/mcpg/prompts.py src/mcpg/pg_search.py src/mcpg/pg19_ddl.py src/mcpg/__init__.py src/mcpg/http_runtime.py
git commit -m "refactor: mechanical FastMCP -> MCPServer rename (imports, type refs, prose)"
```

---

### Task 3: `server.py` — `AuditedFastMCP` → `AuditedMCPServer`

**Files:**
- Modify: `src/mcpg/server.py` (whole file touched; see exact hunks below)
- Test: `tests/unit/test_server.py` (existing file, needs matching renames — folded into this task since it's the direct test of this exact class)

**Interfaces:**
- Consumes: `Context[AppContext, Any]` (`_Ctx`) pattern from Task 2 (not directly imported here, but establishes the convention this file's `Context[AppContext, Any]` annotation follows).
- Produces: `AuditedMCPServer` class, `create_server() -> MCPServer[AppContext]`, both consumed by Task 4 (`http_runtime.py`) and Task 5/6 (tenancy middleware registration + elicitation gate both hook into this file's `create_server`).

- [ ] **Step 1: Update imports**

In `server.py:17-18`, change:
```python
from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock
```
to:
```python
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, ContentBlock, InputRequiredResult
```
(`ContentBlock` import is kept — still used elsewhere in this file's type hints if any; verify with `grep -n ContentBlock src/mcpg/server.py` after this change and drop it only if genuinely unused post-edit. `Context` is newly needed for the `call_tool` signature in Step 3.)

- [ ] **Step 2: Rename the class, delete the now-unnecessary `__init__` override**

Replace `server.py:40-59`:
```python
class AuditedFastMCP(FastMCP[AppContext]):
    """A FastMCP server that records an audit event for every tool call."""

    rate_limiter: RateLimiter
    mcpg_settings: Settings
    in_flight_calls: int = 0
    # OpenTelemetry tracer. ``None`` when MCPG_OTEL_ENABLED=false or
    # the ``mcpg[otel]`` extra isn't installed — :func:`tool_span`
    # treats both cases as no-ops so ``call_tool`` doesn't branch.
    otel_tracer: TracerHandle | None = None

    def __init__(self, *args: Any, version: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # FastMCP's constructor doesn't forward a version to the low-level
        # MCP server, so the ``initialize`` handshake reports the MCP SDK's
        # version in ``serverInfo`` rather than ours. This subclass exists
        # to extend FastMCP, so it's the right place to hold that one bit of
        # SDK-internal knowledge: pin the advertised version to mcpg's own.
        if version is not None:
            self._mcp_server.version = version
```
with:
```python
class AuditedMCPServer(MCPServer[AppContext]):
    """An MCPServer that records an audit event for every tool call."""

    rate_limiter: RateLimiter
    mcpg_settings: Settings
    in_flight_calls: int = 0
    # OpenTelemetry tracer. ``None`` when MCPG_OTEL_ENABLED=false or
    # the ``mcpg[otel]`` extra isn't installed — :func:`tool_span`
    # treats both cases as no-ops so ``call_tool`` doesn't branch.
    otel_tracer: TracerHandle | None = None
```
(`MCPServer.__init__` accepts `version` directly and forwards it to the internal lowlevel `Server`, so the manual patch this subclass existed for is gone — no custom `__init__` needed at all.)

- [ ] **Step 3: Fix the `call_tool` override's signature and return type**

Replace `server.py:79` (the method signature) and its `super().call_tool(...)` call at `server.py:98`:
```python
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
```
with:
```python
    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[AppContext, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
```
and:
```python
                with tool_span(self.otel_tracer, name, arguments, bucket=bucket):
                    result = await super().call_tool(name, arguments)
```
with:
```python
                with tool_span(self.otel_tracer, name, arguments, bucket=bucket):
                    result = await super().call_tool(name, arguments, context)
```
Leave the rest of the method body (rate limiting, audit recording, metrics, the `finally: self.in_flight_calls -= 1`) unchanged — verified in the facts section above that the exception-propagation semantics this logic depends on are unchanged in mcp 2.0. Remove the now-unused `Sequence` import from `collections.abc` at `server.py:13` if nothing else in the file uses it (`grep -n "Sequence" src/mcpg/server.py` to check first).

- [ ] **Step 4: Fix every other reference to the renamed class/type**

- `server.py:120,131`: `Callable[[FastMCP[AppContext]], ...]` and `async def lifespan(_server: FastMCP[AppContext])` → `MCPServer[AppContext]`.
- `server.py:190,197`: `def create_server(...) -> FastMCP[AppContext]:` → `-> MCPServer[AppContext]:`.
- `server.py:238`: `server: AuditedFastMCP = AuditedFastMCP(` → `server: AuditedMCPServer = AuditedMCPServer(`.
- `server.py:260`: `def run(settings: Settings) -> None:` — unchanged signature, but its body creates a `server` whose static type is now `MCPServer[AppContext]`; no edit needed here beyond what Step 5 does.
- `server.py:37`: `__all__ = ["SERVER_NAME", "AppContext", "AuditedFastMCP", "create_server", "make_lifespan", "run"]` → replace `"AuditedFastMCP"` with `"AuditedMCPServer"`.

- [ ] **Step 5: Drop the removed `host`/`port` constructor kwargs**

At `server.py:238-245`:
```python
    server: AuditedFastMCP = AuditedFastMCP(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=make_lifespan(settings, db, lm, cm, ar),
        host=settings.http_host,
        port=settings.http_port,
    )
```
change to:
```python
    server: AuditedMCPServer = AuditedMCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=make_lifespan(settings, db, lm, cm, ar),
    )
```
(`host`/`port` move to Task 4's `build_http_app` call sites — `MCPServer.__init__` has no such kwargs; passing them would now raise `TypeError: __init__() got an unexpected keyword argument 'host'`.)

- [ ] **Step 6: Update `tests/unit/test_server.py`'s direct references**

At `tests/unit/test_server.py:5`, change:
```python
from mcp.server.fastmcp import FastMCP
```
to:
```python
from mcp.server.mcpserver import MCPServer
```
At line 24, `assert isinstance(server, FastMCP)` → `assert isinstance(server, MCPServer)`.
At line 29's comment and line 61's `monkeypatch.setattr(FastMCP, "run", ...)` → `monkeypatch.setattr(MCPServer, "run", ...)`.
Read the full file (`tests/unit/test_server.py`) before editing — there is a version-handshake test whose comment ("FastMCP doesn't forward a version...") is now factually wrong per this task's Step 2 finding (version forwarding is now automatic); update that test's assertion/comment to match the new behavior rather than leaving stale prose, and confirm it still exercises something meaningful (e.g. that `create_server(settings).version == __version__` — this now tests the constructor's own forwarding rather than a manual patch, so keep the assertion but drop any comment implying a workaround exists).

- [ ] **Step 7: Run the test file in isolation**

Run: `uv run pytest tests/unit/test_server.py -v`
Expected: all pass. If the version-handshake test fails, re-check Step 2's claim (`MCPServer(..., version=X)` → `self.version == X`) against the actual installed `mcp` package: `uv run python -c "from mcp.server.mcpserver import MCPServer; s = MCPServer('x', version='9.9.9'); print(s.version)"` should print `9.9.9`.

- [ ] **Step 8: Commit**

```bash
git add src/mcpg/server.py tests/unit/test_server.py
git commit -m "refactor(server): AuditedFastMCP -> AuditedMCPServer, fix call_tool signature for mcp 2.0"
```

---

### Task 4: `http_runtime.py` — pass `host`/`port` explicitly to the HTTP app builders

**Files:**
- Modify: `src/mcpg/http_runtime.py:530-550`
- Test: `tests/unit/test_http_runtime.py` (existing file — extend, don't just fix)

**Interfaces:**
- Consumes: `MCPServer[AppContext]` from Task 3 (the `server` parameter's real runtime type, though `build_http_app` keeps it typed loosely as `object` per the existing `# type: ignore[attr-defined]` pattern).
- Produces: a `Starlette` app whose CORS `allowed_hosts`/`allowed_origins` match `settings.http_host`/`settings.http_port`, consumed by `run_http` (unchanged) and served via uvicorn.

- [ ] **Step 1: Pass `host`/`port` through to both app builders**

At `http_runtime.py:545-548`:
```python
    if kind == "streamable-http":
        app = server.streamable_http_app()  # type: ignore[attr-defined]
    elif kind == "sse":
        app = server.sse_app()  # type: ignore[attr-defined]
```
change to:
```python
    if kind == "streamable-http":
        app = server.streamable_http_app(host=settings.http_host, port=settings.http_port)  # type: ignore[attr-defined]
    elif kind == "sse":
        app = server.sse_app(host=settings.http_host)  # type: ignore[attr-defined]
```
(`sse_app`'s signature only takes `host`, not `port`, per `mcp/server/mcpserver/server.py:1091-1097` — verify this against the actually-installed version with `uv run python -c "import inspect; from mcp.server.mcpserver import MCPServer; print(inspect.signature(MCPServer.sse_app))"` before finalizing; if a later 2.x patch added a `port` param to `sse_app`, pass it too.)

- [ ] **Step 2: Write a test that would have caught the missing host/port regression**

Add to `tests/unit/test_http_runtime.py` (near the existing `test_run_http_builds_app_and_serves_via_uvicorn`):
```python
def test_build_http_app_passes_configured_host_to_streamable_http_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpg import http_runtime

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
            "MCPG_HTTP_HOST": "0.0.0.0",
            "MCPG_HTTP_PORT": "9999",
        }
    )

    captured: dict[str, object] = {}

    class _Stub:
        def streamable_http_app(self, *, host: str, port: int) -> Starlette:
            captured["host"] = host
            captured["port"] = port
            return _bare_app()

    http_runtime.build_http_app(_Stub(), settings, kind="streamable-http")

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
```
(`_bare_app` and `Starlette` are already imported/defined in this test file — check the existing `test_run_http_builds_app_and_serves_via_uvicorn` test for the exact helper name and reuse it rather than redefining.)

- [ ] **Step 3: Run the new test, confirm it fails against the old code, passes against the new**

Run: `uv run pytest tests/unit/test_http_runtime.py::test_build_http_app_passes_configured_host_to_streamable_http_app -v`
Expected: PASS (Step 1 already applied). To confirm the test is meaningful, temporarily revert Step 1's edit, rerun, confirm FAIL, then reapply Step 1.

- [ ] **Step 4: Run the full http_runtime test file**

Run: `uv run pytest tests/unit/test_http_runtime.py -v`
Expected: all pass, including the pre-existing `test_run_http_builds_app_and_serves_via_uvicorn`.

- [ ] **Step 5: Commit**

```bash
git add src/mcpg/http_runtime.py tests/unit/test_http_runtime.py
git commit -m "fix(http): pass configured host/port to streamable_http_app/sse_app (mcp 2.0 moved them off the constructor)"
```

---

### Task 5: Tenancy redesign — `ServerMiddleware` replaces the `request_ctx` hack

**Files:**
- Modify: `src/mcpg/tenancy.py` (delete `_role_from_request`, simplify `resolve_role`, add `TenantRoleContextMiddleware`)
- Modify: `src/mcpg/server.py` (register the new middleware in `create_server`)
- Test: `tests/unit/test_tenancy.py` (existing file — rewrite the tests that exercised `_role_from_request`)

**Interfaces:**
- Consumes: `current_role: ContextVar[str | None]` (unchanged, already defined at `tenancy.py:55`), `_ROLE_SCOPE_KEY` (unchanged, already defined at `tenancy.py:61`), `ServerMiddleware`/`ServerRequestContext` from `mcp.server.context` (new import).
- Produces: `TenantRoleContextMiddleware`, a `ServerMiddleware` instance registered via `AuditedMCPServer(..., middleware=[TenantRoleContextMiddleware()])` in `server.py`'s `create_server`. Everything downstream (`TenantSqlDriver._execute_with_connection` calling `resolve_role(...)`) is **unchanged** — this task only changes how `current_role` gets set, not who reads it.

- [ ] **Step 1: Delete `_role_from_request` and simplify `resolve_role`**

Delete `tenancy.py:75-102` (`_role_from_request` in full — the `request_ctx` import inside it no longer resolves).

Replace `tenancy.py:105-120`:
```python
def resolve_role(default: str | None) -> str | None:
    """Return the role for the current request.

    On HTTP/SSE the per-message request is authoritative: its stashed role, or
    the static ``default`` when the request carried no role — never the
    session-frozen :data:`current_role`. On stdio (no request context) the
    :data:`current_role` ContextVar wins, falling back to ``default``. ``None``
    means "do nothing — use the role the pool was opened with".
    """
    has_request, request_role = _role_from_request()
    if has_request:
        return request_role if request_role is not None else default
    override = current_role.get()
    if override is not None:
        return override
    return default
```
with:
```python
def resolve_role(default: str | None) -> str | None:
    """Return the role for the current request.

    :data:`current_role` is set once per inbound request by
    :class:`TenantRoleContextMiddleware`, which runs inside that request's own
    asyncio task (the MCP SDK dispatches every request to its own task — see
    the middleware's docstring) — so the ContextVar is reliably scoped to this
    request on every transport, not just stdio. ``None`` means "do nothing —
    use the role the pool was opened with".
    """
    override = current_role.get()
    if override is not None:
        return override
    return default
```

- [ ] **Step 2: Add the `ServerMiddleware`**

Add near the top of `tenancy.py`, after the existing imports:
```python
from mcp.server.context import ServerMiddleware, ServerRequestContext
```
(keep this import lazy/module-level consistent with the file's existing style — it's a stable public import, no need for a try/except fallback since this is now a hard dependency of the file, unlike the old defensive `try: from mcp.server.lowlevel.server import request_ctx except Exception:` — that defensiveness existed because `request_ctx` was SDK-internal; `ServerMiddleware`/`ServerRequestContext` are public API.)

Add this class after `validate_role` and before `TenantSqlDriver`:
```python
class TenantRoleContextMiddleware:
    """Sets :data:`current_role` from the request's stashed tenant role.

    HTTP/SSE middlewares (:class:`mcpg.http_runtime._TenantRoleMiddleware`,
    :class:`mcpg.http_runtime._OIDCAuthMiddleware`) validate ``X-MCPG-Role`` /
    the OIDC role claim and stash it on the ASGI request's ``scope`` under
    :data:`_ROLE_SCOPE_KEY`. This middleware runs inside the MCP SDK's
    per-request dispatch — each inbound request gets its own asyncio task
    (``mcp.shared.dispatcher.Dispatcher.run``), so setting a plain
    :class:`~contextvars.ContextVar` here is reliably visible to everything
    that request's tool call awaits, including the SQL driver several calls
    deep — unlike a naive set from the ASGI middleware layer, which would be
    invisible by the time a tool function runs in the SDK's own task.

    A no-op on stdio (``ctx.request`` is ``None`` there — nothing to read).
    """

    async def __call__(
        self,
        ctx: ServerRequestContext[object, object],
        call_next,  # type: ignore[no-untyped-def]
    ):
        scope = getattr(getattr(ctx, "request", None), "scope", None)
        role = scope.get(_ROLE_SCOPE_KEY) if isinstance(scope, dict) else None
        if not isinstance(role, str):
            return await call_next(ctx)
        token = current_role.set(role)
        try:
            return await call_next(ctx)
        finally:
            current_role.reset(token)
```
(The `call_next` parameter's precise type is `mcp.server.context.CallNext` — import and use it explicitly rather than leaving `# type: ignore[no-untyped-def]` if `mypy --strict` accepts `from mcp.server.context import CallNext` cleanly; verify in Step 4 and tighten the annotation if so, since this file is inside the coverage/mypy-strict gate like the rest of `mcpg`.)

- [ ] **Step 3: Register the middleware in `server.py`**

In `server.py`'s `create_server` (from Task 3, Step 5's edited constructor call), add the `middleware` kwarg:
```python
    server: AuditedMCPServer = AuditedMCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=make_lifespan(settings, db, lm, cm, ar),
        middleware=[TenantRoleContextMiddleware()],
    )
```
Add the import near `server.py`'s other `mcpg.*` imports:
```python
from mcpg.tenancy import TenantRoleContextMiddleware
```

- [ ] **Step 4: Tighten the middleware's type hints, verify mypy**

Run: `uv run mypy src/mcpg/tenancy.py src/mcpg/server.py`
Expected: clean. If `call_next`'s type can't be spelled without an import cycle, keep the `# type: ignore[no-untyped-def]` but leave a one-line comment explaining why (matching this file's existing style of explaining every `# type: ignore`).

- [ ] **Step 5: Rewrite `tests/unit/test_tenancy.py`'s coverage of the old mechanism**

Read the existing file first — find every test that exercises `_role_from_request` or mocks `mcp.server.lowlevel.server.request_ctx`. Replace those with tests against `TenantRoleContextMiddleware` directly:
```python
import pytest

from mcpg.tenancy import _ROLE_SCOPE_KEY, TenantRoleContextMiddleware, current_role, resolve_role


class _FakeRequest:
    def __init__(self, scope: dict[str, object]) -> None:
        self.scope = scope


class _FakeCtx:
    def __init__(self, request: object | None) -> None:
        self.request = request


@pytest.mark.asyncio
async def test_middleware_sets_current_role_from_request_scope() -> None:
    middleware = TenantRoleContextMiddleware()
    ctx = _FakeCtx(_FakeRequest({_ROLE_SCOPE_KEY: "analytics_ro"}))

    seen_role_inside_call_next = {}

    async def call_next(_ctx: object) -> None:
        seen_role_inside_call_next["role"] = resolve_role(None)
        return None

    await middleware(ctx, call_next)  # type: ignore[arg-type]

    assert seen_role_inside_call_next["role"] == "analytics_ro"
    # Reset after call_next returns — no leakage into the next request's task.
    assert current_role.get() is None


@pytest.mark.asyncio
async def test_middleware_is_noop_when_request_has_no_role_scope_key() -> None:
    middleware = TenantRoleContextMiddleware()
    ctx = _FakeCtx(_FakeRequest({}))

    async def call_next(_ctx: object) -> str | None:
        return resolve_role("static_default")

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]

    assert result == "static_default"


@pytest.mark.asyncio
async def test_middleware_is_noop_on_stdio_where_request_is_none() -> None:
    middleware = TenantRoleContextMiddleware()
    ctx = _FakeCtx(None)

    async def call_next(_ctx: object) -> str | None:
        return resolve_role("static_default")

    result = await middleware(ctx, call_next)  # type: ignore[arg-type]

    assert result == "static_default"
```
Keep every pre-existing test of `validate_role`, `TenantSqlDriver`, and `_execute_with_role` unchanged — those don't touch the removed mechanism.

- [ ] **Step 6: Run the tenancy test suite**

Run: `uv run pytest tests/unit/test_tenancy.py -v`
Expected: all pass.

- [ ] **Step 7: Run the broader integration-adjacent suites that exercise tenancy through the HTTP path**

Run: `uv run pytest tests/unit/test_http_runtime.py tests/unit/test_oidc.py -v`
Expected: all pass unchanged — `_TenantRoleMiddleware`/`_OIDCAuthMiddleware` in `http_runtime.py` are untouched by this task; they still stash the role on the ASGI scope exactly as before, they just now feed a `ServerMiddleware` instead of an ambient contextvar reader.

- [ ] **Step 8: Commit**

```bash
git add src/mcpg/tenancy.py src/mcpg/server.py tests/unit/test_tenancy.py
git commit -m "refactor(tenancy): replace the request_ctx hack with a ServerMiddleware

mcp 2.0 removed the ambient request_ctx ContextVar tenancy.py read role
state from. The MCP SDK now dispatches every inbound request to its own
asyncio task (mcp.shared.dispatcher.Dispatcher.run), which means a plain
ContextVar set inside a ServerMiddleware is reliably visible throughout
that request's call chain — including several calls deep into the SQL
driver — on every transport. This removes the old 'per-message request is
authoritative, current_role is session-frozen on HTTP/SSE' distinction
entirely: current_role is now simply authoritative everywhere."
```

---

### Task 6: Elicitation confirmation gate for write-tier tools

**Files:**
- Modify: `src/mcpg/config.py` (new `elicit_confirm_writes` setting)
- Modify: `src/mcpg/server.py` (the `call_tool` override gains the confirmation gate)
- Test: `tests/unit/test_server.py` (extend), `tests/unit/test_config.py` (extend)

**Interfaces:**
- Consumes: `Settings.elicit_confirm_writes: bool` (new), `Context.elicit()` (mcp 2.0 API), `tool.annotations.readOnlyHint` (already-existing, tested metadata from `tools.py:_apply_tool_wire_metadata`), `context.request_context.session.client_capabilities.elicitation` (mcp 2.0 API, capability-gate the feature so non-elicitation clients aren't broken).
- Produces: every write/DDL/shell/listen/migrate-tier tool call, when the setting is on and the connected client declared elicitation support, requires an accepted `ctx.elicit()` confirmation before the tool body runs. Declined/cancelled → the call returns a `CallToolResult(is_error=True)` without invoking the tool. No changes to any of the 254 individual tool functions in `tools.py` — this is centralized entirely in `AuditedMCPServer.call_tool`.

- [ ] **Step 1: Add the setting**

In `config.py`'s `Settings` dataclass, near `allow_ddl`/`allow_shell` (around line 69-71):
```python
    allow_ddl: bool = False
    allow_shell: bool = False
    allow_listen: bool = False
    elicit_confirm_writes: bool = False
```
In `load_settings`, near the `allow_shell` parsing (around line 521-523):
```python
    allow_shell = False
    if (raw := env.get("MCPG_ALLOW_SHELL")) is not None:
        allow_shell = _parse_bool("MCPG_ALLOW_SHELL", raw)

    elicit_confirm_writes = False
    if (raw := env.get("MCPG_ELICIT_CONFIRM_WRITES")) is not None:
        elicit_confirm_writes = _parse_bool("MCPG_ELICIT_CONFIRM_WRITES", raw)
```
Thread `elicit_confirm_writes=elicit_confirm_writes` into the `Settings(...)` construction call at the end of `load_settings` (find the exact call site — it's a single multi-line `Settings(` call near the end of the function, alongside `allow_ddl=allow_ddl, allow_shell=allow_shell, ...`).
Add it to the `__repr__`-equivalent string near line 294 (`f"allow_shell={self.allow_shell}, "`) as `f"elicit_confirm_writes={self.elicit_confirm_writes}, "`, matching the existing pattern.

Default is `False` (opt-in): most MCP clients as of writing don't declare elicitation support, and — independent of that — the maintainer should decide per-deployment whether the interactive-confirmation UX (which changes what an unattended/automated agent experiences on every write) is wanted, rather than mcpg silently changing behavior for everyone who upgrades. This mirrors the existing `allow_ddl`/`allow_shell` opt-in pattern exactly.

- [ ] **Step 2: Add the confirmation schema and gate logic to `server.py`**

Add near the top of `server.py`, after the existing imports:
```python
from pydantic import BaseModel, Field
```
Add a small module-level schema (elicitation schemas must contain only primitive types per the MCP spec — verified against `Context.elicit`'s docstring):
```python
class _ConfirmMutation(BaseModel):
    confirm: bool = Field(description="Set true to proceed with this write/DDL operation.")
```

In `AuditedMCPServer.call_tool` (from Task 3's Step 3 edit), add the gate immediately after the rate-limit check and before the `metrics = get_metrics()` line:
```python
            if (
                getattr(self, "mcpg_settings", None) is not None
                and self.mcpg_settings.elicit_confirm_writes
                and context is not None
            ):
                tool = self._tool_manager.get_tool(name)
                is_read_only = tool is not None and tool.annotations is not None and tool.annotations.readOnlyHint
                if tool is not None and not is_read_only:
                    session = context.request_context.session
                    capabilities = session.client_capabilities
                    supports_elicitation = capabilities is not None and capabilities.elicitation is not None
                    if supports_elicitation:
                        confirmation = await context.elicit(
                            f"{name!r} will modify the database. Proceed?",
                            _ConfirmMutation,
                        )
                        if confirmation.action != "accept" or not confirmation.data.confirm:
                            return CallToolResult(
                                content=[ContentBlock(TextContent(type="text", text=f"{name!r} was not confirmed; no changes made."))],
                                is_error=True,
                            )
```
(Verify the exact `ContentBlock`/`TextContent` construction against how `mcp/server/mcpserver/server.py:424`'s own error path builds one — `CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)` does **not** wrap `TextContent` in `ContentBlock(...)` since `ContentBlock` is a type alias/union (`TextContent | ImageContent | ...`), not a wrapper class — fix the snippet above to match: `content=[TextContent(type="text", text=...)]` directly, no `ContentBlock(...)` call. Import `TextContent` from `mcp.types` alongside the existing `ContentBlock` import.)

- [ ] **Step 3: Add unit tests for the gate**

In `tests/unit/test_server.py`, add tests covering: (a) setting off → tool runs without any elicit call regardless of read-only status; (b) setting on, client declares elicitation support, tool is write-tier, client accepts → tool runs; (c) same but client declines → tool does NOT run, result `is_error=True`; (d) setting on but client does NOT declare elicitation capability → tool runs without an elicit call (graceful degradation); (e) setting on, tool IS read-only → no elicit call regardless of client capability. Build these against a fake `Context`/session exposing a settable `client_capabilities` and a fake `elicit` coroutine recording whether it was called and returning a scripted `ElicitationResult`-shaped object — follow this test file's existing pattern for constructing a minimal server + fake dependencies (read the file's existing fixtures before writing new ones; don't duplicate a second server-construction helper if one already exists).

- [ ] **Step 4: Add a config test**

In `tests/unit/test_config.py`, add:
```python
def test_elicit_confirm_writes_defaults_to_false_and_parses() -> None:
    assert load_settings({"MCPG_DATABASE_URL": _DB_URL}).elicit_confirm_writes is False

    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _DB_URL,
            "MCPG_ELICIT_CONFIRM_WRITES": "true",
        }
    )
    assert settings.elicit_confirm_writes is True
```

- [ ] **Step 5: Run the new and existing tests**

Run: `uv run pytest tests/unit/test_server.py tests/unit/test_config.py -v`
Expected: all pass.

- [ ] **Step 6: Run mypy on the changed files**

Run: `uv run mypy src/mcpg/server.py src/mcpg/config.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/mcpg/server.py src/mcpg/config.py tests/unit/test_server.py tests/unit/test_config.py
git commit -m "feat: elicit confirmation for write-tier tools (MCPG_ELICIT_CONFIRM_WRITES, opt-in)

Centralized in AuditedMCPServer.call_tool rather than touching any of the
254 individual tool bodies: reuses the readOnlyHint annotation tools.py
already stamps on every registered tool, and skips the elicit round-trip
entirely for clients that didn't declare elicitation support during
initialize, so non-interactive/automated clients are unaffected unless
they opt in."
```

---

### Task 7: Mechanical test-file renames + the removed in-memory session helper + `tools.py`'s ToolAnnotations field rename

**Revised again after Task 6 landed**: Task 6's implementer discovered a third, independent category
of remaining SDK-migration debt, confined to `src/mcpg/tools.py`: mcp 2.0's `mcp_types.ToolAnnotations`
(and the wire `Tool` type) rejects the old camelCase field names as real Python attributes — only
`read_only_hint`/`destructive_hint`/`open_world_hint`/`input_schema`/`output_schema` (snake_case) are
real attributes now; camelCase (`readOnlyHint` etc.) survives only as a pydantic *construction alias*
(confirmed live: `ToolAnnotations(readOnlyHint=True).read_only_hint` works, but
`hasattr(t, 'readOnlyHint')` is `False`). `tools.py:350-351` (`tool.inputSchema`/`tool.outputSchema`)
and `tools.py:7010-7024` (`_apply_tool_wire_metadata`'s `ToolAnnotations(readOnlyHint=..., ...)`
construction plus `existing.readOnlyHint`/`.destructiveHint`/`.openWorldHint` attribute reads) are the
only two affected spots — 8 `mypy --strict` errors total, confirmed via `uv run mypy src/mcpg/tools.py`.
**Currently unreachable at runtime** (the attribute-*read* branch at 7018-7026 only executes when a
tool was registered with `annotations=` already set, and the docstring at :7000-7003 confirms "No call
site passes `annotations=`... today" — so `existing` is always `None` and that branch never runs in
practice), which is why Tasks 1-6 never crashed on this. Still must be fixed for `mypy --strict` to pass
cleanly (Task 8's gate and CI both require it), and — more importantly — it's the exact same field this
whole migration's Task 6 elicitation gate reads (`server.py`'s new code correctly uses
`tool.annotations.read_only_hint`, snake_case): once this task fixes `tools.py`'s construction to match,
the codebase is internally consistent on this field for the first time since the migration began.

**Revised after Task 3 landed**: `mcp.shared.memory.create_connected_server_and_client_session` — a
helper 49 test files use to spin up a real client/server pair over in-memory
streams — was **removed** in mcp 2.0 (only the lower-level
`create_client_server_memory_streams` remains). Verified via a standalone
smoke test (not the real suite) that a drop-in replacement built from the old
1.29.0 source with exactly three edits (`FastMCP` → `MCPServer`,
`server._mcp_server` → `server._lowlevel_server`, otherwise unchanged) works
end-to-end: a bare `Server` + the replacement helper successfully completed
`ClientSession.initialize()` and a real `list_tools()` round-trip (got a
proper JSON-RPC `Method not found` response for an intentionally-unregistered
handler — proving the stream transport and protocol handshake both work, not
just that the import resolves). All 49 files use the exact same import line
(`from mcp.shared.memory import create_connected_server_and_client_session`,
confirmed via `grep -h ... | sort -u` → one distinct line), so this becomes a
single shared helper + a mechanical one-line import swap across all 49 files,
not a per-file redesign.

**Files:**
- Modify: `src/mcpg/tools.py` (the `ToolAnnotations`/`Tool` field-name fix, lines 350-351 and 7010-7024)
- Create: `tests/_mcp_test_helpers.py` (the replacement helper)
- Modify: `tests/unit/test_listen.py`, `tests/unit/test_slow_call.py`, `tests/unit/test_about.py`, `tests/unit/test_session_intent.py`
- Modify: `tests/contract/test_describe_tool.py`, `tests/contract/test_mcp_prompts.py`, `tests/contract/test_mcp_resources.py`, `tests/contract/test_tool_annotations.py`, `tests/contract/test_tool_output_schemas.py`, `tests/contract/test_tool_surface_snapshot.py`
- Modify (import-line swap for the removed helper — full list, `test_listen.py` already counted above): `tests/integration/test_load.py`, `tests/integration/test_tool_registration_integration.py`, `tests/unit/test_advisors.py`, `tests/unit/test_audit.py`, `tests/unit/test_audit_integrity.py`, `tests/unit/test_audit_trail.py`, `tests/unit/test_buffercache.py`, `tests/unit/test_compact.py`, `tests/unit/test_composite.py`, `tests/unit/test_cron.py`, `tests/unit/test_data_movement.py`, `tests/unit/test_diagrams.py`, `tests/unit/test_diesel.py`, `tests/unit/test_drizzle.py`, `tests/unit/test_ecto.py`, `tests/unit/test_ent.py`, `tests/unit/test_extensions.py`, `tests/unit/test_health.py`, `tests/unit/test_indexing.py`, `tests/unit/test_introspection.py`, `tests/unit/test_jooq.py`, `tests/unit/test_liveops.py`, `tests/unit/test_maintenance.py`, `tests/unit/test_migration_history.py`, `tests/unit/test_migrations.py`, `tests/unit/test_nl2sql_routing.py`, `tests/unit/test_optimizer.py`, `tests/unit/test_partman.py`, `tests/unit/test_pg_search.py`, `tests/unit/test_prisma.py`, `tests/unit/test_query.py`, `tests/unit/test_rag_efficiency.py`, `tests/unit/test_rag_telemetry.py`, `tests/unit/test_rate_limit.py`, `tests/unit/test_schema_diff.py`, `tests/unit/test_schema_docs.py`, `tests/unit/test_sqlalchemy_export.py`, `tests/unit/test_sqlc.py`, `tests/unit/test_textsearch.py`, `tests/unit/test_tool_examples.py`, `tests/unit/test_tools.py`, `tests/unit/test_turboquant.py`, `tests/unit/test_vector_ops.py`, `tests/unit/test_vector_tuner_advanced.py`, `tests/unit/test_vector_tuning.py`, `tests/unit/test_walinspect.py`, `tests/unit/test_workload.py`, `tests/unit/test_write.py`

**Interfaces:**
- Consumes: `MCPServer`/`AuditedMCPServer` from Tasks 2–3.
- Produces: `tests._mcp_test_helpers.create_connected_server_and_client_session` (same call signature as the removed SDK function — `async with create_connected_server_and_client_session(server, **kwargs) as client:`), imported by all 49 files listed above. `tests/` is already on `pythonpath` (`pyproject.toml`'s `[tool.pytest.ini_options]`), so `from _mcp_test_helpers import create_connected_server_and_client_session` resolves from any test file regardless of `unit/`/`integration/` subdirectory.

- [ ] **Step 0: Fix `tools.py`'s `ToolAnnotations`/`Tool` snake_case field names**

At `tools.py:350-351`, change:
```python
                    input_schema=tool.inputSchema,
                    output_schema=tool.outputSchema,
```
to:
```python
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
```

At `tools.py:7010-7024`, change:
```python
            tool.annotations = ToolAnnotations(
                readOnlyHint=read_only,
                # None (not False) for reads: the hint is only meaningful
                # on write-capable tools per the MCP spec.
                destructiveHint=None if read_only else tool.name not in _NON_DESTRUCTIVE_WRITE_TOOLS,
                openWorldHint=tool.name in _OPEN_WORLD_TOOLS,
            )
            continue
        derived: dict[str, bool] = {}
        if existing.readOnlyHint is None:
            derived["readOnlyHint"] = read_only
        if not read_only and existing.destructiveHint is None:
            derived["destructiveHint"] = tool.name not in _NON_DESTRUCTIVE_WRITE_TOOLS
        if existing.openWorldHint is None:
            derived["openWorldHint"] = tool.name in _OPEN_WORLD_TOOLS
```
to:
```python
            tool.annotations = ToolAnnotations(
                read_only_hint=read_only,
                # None (not False) for reads: the hint is only meaningful
                # on write-capable tools per the MCP spec.
                destructive_hint=None if read_only else tool.name not in _NON_DESTRUCTIVE_WRITE_TOOLS,
                open_world_hint=tool.name in _OPEN_WORLD_TOOLS,
            )
            continue
        derived: dict[str, bool] = {}
        if existing.read_only_hint is None:
            derived["read_only_hint"] = read_only
        if not read_only and existing.destructive_hint is None:
            derived["destructive_hint"] = tool.name not in _NON_DESTRUCTIVE_WRITE_TOOLS
        if existing.open_world_hint is None:
            derived["open_world_hint"] = tool.name in _OPEN_WORLD_TOOLS
```
(`derived`'s dict keys must also be snake_case — they're passed straight into `existing.model_copy(update=derived)` two lines below, which sets attributes by field name, not by alias.)

Run: `uv run mypy src/mcpg/tools.py`
Expected: `Success: no issues found in 1 source file` (down from 8 errors).

Run: `uv run pytest tests/unit/test_tools.py tests/unit/test_tool_introspection.py -v` (adjust file names if these aren't the exact tests covering `_apply_tool_wire_metadata`/`describe_tool` — grep `tests/unit/` for `_apply_tool_wire_metadata\|read_only_hint\|readOnlyHint` first to find the real coverage).
Expected: all pass.

This is currently dead-code-safe (the buggy attribute-read branch is unreachable, per the plan text above), so this step carries no runtime-behavior risk — it's a pure type-correctness fix. Commit this as part of Task 7's single commit at the end, not separately.

- [ ] **Step 1: Create the replacement helper**

Write `tests/_mcp_test_helpers.py`:
```python
"""Drop-in replacement for mcp.shared.memory.create_connected_server_and_client_session,
removed in mcp 2.0 (only create_client_server_memory_streams remains there).
Ported from mcp 1.29.0's implementation with the two changes mcp 2.0 requires:
FastMCP -> MCPServer, and the private attribute FastMCP used to reach the
lowlevel Server was renamed from _mcp_server to _lowlevel_server.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import anyio

import mcp.types as types
from mcp.client.session import ClientSession, ElicitationFnT, ListRootsFnT, LoggingFnT, MessageHandlerFnT, SamplingFnT
from mcp.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams


@asynccontextmanager
async def create_connected_server_and_client_session(
    server: "Server[Any] | MCPServer[Any]",
    read_timeout_seconds: timedelta | None = None,
    sampling_callback: SamplingFnT | None = None,
    list_roots_callback: ListRootsFnT | None = None,
    logging_callback: LoggingFnT | None = None,
    message_handler: MessageHandlerFnT | None = None,
    client_info: types.Implementation | None = None,
    raise_exceptions: bool = False,
    elicitation_callback: ElicitationFnT | None = None,
) -> AsyncGenerator[ClientSession, None]:
    """Creates a ClientSession that is connected to a running MCP server."""
    if isinstance(server, MCPServer):
        server = server._lowlevel_server  # type: ignore[reportPrivateUsage]

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                    raise_exceptions=raise_exceptions,
                )
            )

            try:
                async with ClientSession(
                    read_stream=client_read,
                    write_stream=client_write,
                    read_timeout_seconds=read_timeout_seconds,
                    sampling_callback=sampling_callback,
                    list_roots_callback=list_roots_callback,
                    logging_callback=logging_callback,
                    message_handler=message_handler,
                    client_info=client_info,
                    elicitation_callback=elicitation_callback,
                ) as client_session:
                    await client_session.initialize()
                    yield client_session
            finally:
                tg.cancel_scope.cancel()
```

- [ ] **Step 2: Smoke-test the helper standalone before touching any real test file**

Run (adjust the inline script or drop it in a throwaway file — don't commit it):
```bash
uv run python -c "
import asyncio
from tests._mcp_test_helpers import create_connected_server_and_client_session
from mcp.server.lowlevel import Server

server = Server('smoke-test-server')

async def main():
    async with create_connected_server_and_client_session(server) as client:
        try:
            await client.list_tools()
        except Exception as e:
            print('protocol round-trip OK, got expected error for unregistered handler:', e)

asyncio.run(main())
"
```
Expected: prints the "protocol round-trip OK" line (a real `MCPError: Method not found` from the server, proving the stream handshake and request/response cycle both work — NOT a stream-protocol/anyio compatibility error, which would look like an `ExceptionGroup` from inside `anyio`/`ClientSession.__aexit__` instead of a clean `mcp.shared.exceptions.MCPError`). If you get the latter, STOP — the duck-typing assumption between `ContextReceiveStream`/`ContextSendStream` (mcp 2.0's memory-stream wrapper) and what `ClientSession`/`Server.run()` expect does not hold, and this needs the controller's decision on a real replacement, not a patched-up version of this helper.

- [ ] **Step 3: Swap the import line in all 49 consumer files**

The import is character-for-character identical in every file (confirmed via `grep -h "from mcp.shared.memory import" tests/ -r --include="*.py" | sort -u` → one line), so this is one `sed` across the full file list:
```bash
grep -rl "from mcp.shared.memory import create_connected_server_and_client_session" tests/ --include="*.py" | \
  xargs sed -i 's/from mcp\.shared\.memory import create_connected_server_and_client_session/from _mcp_test_helpers import create_connected_server_and_client_session/'
```
Verify:
```bash
grep -rl "from mcp.shared.memory import create_connected_server_and_client_session" tests/ --include="*.py"
```
Expected: no output (zero remaining).

- [ ] **Step 4: Grep every remaining direct `FastMCP` reference in the originally-scoped 10 files**

Run:
```bash
grep -rn "AuditedFastMCP\|\bFastMCP\b" tests/unit/test_listen.py tests/unit/test_slow_call.py tests/unit/test_about.py tests/unit/test_session_intent.py tests/contract/test_describe_tool.py tests/contract/test_mcp_prompts.py tests/contract/test_mcp_resources.py tests/contract/test_tool_annotations.py tests/contract/test_tool_output_schemas.py tests/contract/test_tool_surface_snapshot.py
```
For each hit, determine whether it's an import (`from mcp.server.fastmcp import ...`), a type/patch-target reference (`FastMCP[AppContext]`, `isinstance(x, FastMCP)`, `patch("mcp.server.fastmcp.FastMCP.call_tool", ...)`), or prose (a docstring/comment) — apply the same rename rules as Task 2 (imports/types/patch-targets → `mcp.server.mcpserver` / `MCPServer`; prose → reworded, not blindly renamed if it changes the sentence's meaning). `test_slow_call.py` specifically needs its `patch("mcp.server.fastmcp.FastMCP.call_tool", ...)` target changed to `patch("mcp.server.mcpserver.MCPServer.call_tool", ...)` — confirmed by Task 3's implementer as the exact failure mode (4 tests, `AttributeError: module 'mcp.server' has no attribute 'fastmcp'`).

- [ ] **Step 5: Run the full affected-file set**

Run: `uv run pytest tests/unit/test_listen.py tests/unit/test_slow_call.py tests/unit/test_about.py tests/unit/test_session_intent.py tests/contract/test_describe_tool.py tests/contract/test_mcp_prompts.py tests/contract/test_mcp_resources.py tests/contract/test_tool_annotations.py tests/contract/test_tool_output_schemas.py tests/contract/test_tool_surface_snapshot.py -v`
Expected: all pass. (The contract snapshot tests may still fail here if the tool surface or return shapes actually changed shape under mcp 2.0 — that's expected and is what Task 8 investigates and resolves; don't chase snapshot diffs in this task, only fix genuine `FastMCP`/removed-helper breakage.)

Then spot-check a sample of the 49 import-swapped files across different areas of the suite (don't run all 49 individually — Task 8 runs the full suite):
```bash
uv run pytest tests/unit/test_advisors.py tests/unit/test_write.py tests/unit/test_walinspect.py tests/unit/test_tools.py -v
```
Expected: all pass, confirming the shared helper works across a representative sample before trusting the mechanical sed across the rest.

- [ ] **Step 6: Commit**

```bash
git add src/mcpg/tools.py tests/_mcp_test_helpers.py tests/unit/test_listen.py tests/unit/test_slow_call.py tests/unit/test_about.py tests/unit/test_session_intent.py tests/contract/test_describe_tool.py tests/contract/test_mcp_prompts.py tests/contract/test_mcp_resources.py tests/contract/test_tool_annotations.py tests/contract/test_tool_output_schemas.py tests/contract/test_tool_surface_snapshot.py tests/integration/test_load.py tests/integration/test_tool_registration_integration.py tests/unit/test_advisors.py tests/unit/test_audit.py tests/unit/test_audit_integrity.py tests/unit/test_audit_trail.py tests/unit/test_buffercache.py tests/unit/test_compact.py tests/unit/test_composite.py tests/unit/test_cron.py tests/unit/test_data_movement.py tests/unit/test_diagrams.py tests/unit/test_diesel.py tests/unit/test_drizzle.py tests/unit/test_ecto.py tests/unit/test_ent.py tests/unit/test_extensions.py tests/unit/test_health.py tests/unit/test_indexing.py tests/unit/test_introspection.py tests/unit/test_jooq.py tests/unit/test_liveops.py tests/unit/test_maintenance.py tests/unit/test_migration_history.py tests/unit/test_migrations.py tests/unit/test_nl2sql_routing.py tests/unit/test_optimizer.py tests/unit/test_partman.py tests/unit/test_pg_search.py tests/unit/test_prisma.py tests/unit/test_query.py tests/unit/test_rag_efficiency.py tests/unit/test_rag_telemetry.py tests/unit/test_rate_limit.py tests/unit/test_schema_diff.py tests/unit/test_schema_docs.py tests/unit/test_sqlalchemy_export.py tests/unit/test_sqlc.py tests/unit/test_textsearch.py tests/unit/test_tool_examples.py tests/unit/test_tools.py tests/unit/test_turboquant.py tests/unit/test_vector_ops.py tests/unit/test_vector_tuner_advanced.py tests/unit/test_vector_tuning.py tests/unit/test_walinspect.py tests/unit/test_workload.py tests/unit/test_write.py
git commit -m "test: replace removed create_connected_server_and_client_session + mechanical FastMCP rename in remaining test files"
```

---

### Task 8: Regenerate both contract snapshots, run the full suite, resolve real diffs

**Files:**
- Modify (regenerated, not hand-edited): `tests/contract/tool_surface.snapshot.json`, the tool-return-shapes snapshot (find its exact filename via `grep -rn MCPG_REGENERATE_TOOL_RETURN_SHAPES tests/`)
- Test: the full suite

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: a fully green `uv run pytest tests/unit tests/contract -q`, `uv run mypy src/mcpg`, `uv run ruff check` / `ruff format --check`.

- [ ] **Step 1: Regenerate the tool-surface snapshot**

Run: `MCPG_REGENERATE_TOOL_SNAPSHOT=1 uv run pytest tests/contract/test_tool_surface_snapshot.py -v`
Expected: writes `tests/contract/tool_surface.snapshot.json`. Run `git diff tests/contract/tool_surface.snapshot.json` — expected diff is **empty or near-empty** (tool count, names, and descriptions are all decided by `tools.py`, which this migration didn't touch functionally — only its imports/type annotations changed). If the diff shows tool count changes, tools missing, or descriptions changed, STOP and investigate before proceeding — that would mean something in Tasks 2–7 broke registration, not just renamed it.

- [ ] **Step 2: Regenerate the tool-return-shapes snapshot**

Run: `MCPG_REGENERATE_TOOL_RETURN_SHAPES=1 uv run pytest tests/contract/test_tool_output_schemas.py -v` (confirm this is the right env var / test file pairing by grepping `tests/contract/` for `MCPG_REGENERATE_TOOL_RETURN_SHAPES` first — CLAUDE.md names this as a real regenerate-and-commit step, so the wiring exists, just confirm the exact file before running).
Expected: writes the return-shapes snapshot. Diff it the same way as Step 1 — expect empty or near-empty; investigate any real diff (a shape change here would mean `CallToolResult`'s wire serialization differs from the old `Sequence[ContentBlock] | dict` return in some observable way `Tool.fn_metadata.convert_result` doesn't paper over).

- [ ] **Step 3: Run the full unit + contract suite**

Run: `uv run pytest tests/unit tests/contract -q`
Expected: 2867+ passed (may be a few more given Tasks 4–6 added tests), 0 failed, ≤3 skipped (matching the pre-migration baseline recorded in this plan's Global Constraints).

- [ ] **Step 4: Run mypy, ruff, format checks**

Run: `uv run mypy src/mcpg && uv run ruff check && uv run ruff format --check`
Expected: all clean.

- [ ] **Step 5: Manual smoke test against a real database**

Run: `uv run mcpg --version` and `uv run mcpg --help` — confirm both still work (these don't touch the DB). Then, with `MCPG_DATABASE_URL` pointed at a scratch/test Postgres instance, run `uv run mcpg` briefly under stdio and confirm the "ready on stdio" log line appears with no traceback — this is the one thing no unit test fully replaces (an actual `MCPServer.run(transport="stdio")` call).

- [ ] **Step 6: Commit the regenerated snapshots**

```bash
git add tests/contract/tool_surface.snapshot.json tests/contract/<return-shapes-snapshot-filename>
git commit -m "test: regenerate contract snapshots after mcp 2.0 migration"
```
(If Step 1/2 produced an empty diff, there's nothing to commit for that snapshot — only commit the ones that actually changed, and note in the commit message that the tool surface itself is unchanged, only the SDK underneath it.)

---

### Task 9: Docs, roadmap row, changelog

**Files:**
- Modify: `docs/feature-shortlist.md` (new `## 20.` section)
- Modify: `docs/architecture.md:18-19,38-39,258` (diagram + prose referencing `AuditedFastMCP`/`FastMCP[AppContext]`)
- Modify: `docs/contributing/adding-tools.md:177,184` (prose referencing "FastMCP auto-derives")
- Modify: `docs/PROGRESS.md:102,290,949` (prose referencing `AuditedFastMCP` — discovered during Task 3, not in the plan's original file list)
- Modify: `docs/security.md:229` (prose referencing `AuditedFastMCP` — same discovery)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the final shape of everything from Tasks 1–8 (this task is written last because it describes what actually shipped, not what was planned).

- [ ] **Step 1: Add the roadmap row**

Add a new section to `docs/feature-shortlist.md`, after `## 19. Benchmark suite`, matching the existing table format (`| # | Item | Effort | Value | Notes |`):
```markdown
## 20. Migrate to the mcp 2.0 SDK

PR #290 capped `mcp[cli]<2` as a hotfix after upstream's `mcp` 2.0.0 renamed
`mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer` with no
back-compat shim, breaking fresh installs. This migrates MCPg onto the new
API, redesigns per-request tenancy onto `mcp`'s new `ServerMiddleware` model
(a simplification, not just a port — see the plan's rationale), and adds
`ctx.elicit()` confirmation for write-tier tool calls (opt-in via
`MCPG_ELICIT_CONFIRM_WRITES`).

| # | Item | Effort | Value | Notes |
|---|---|---|---|---|
| 20.1 | ✅ **Shipped.** **Core SDK migration.** `FastMCP` → `MCPServer`, `AuditedFastMCP` → `AuditedMCPServer`, `call_tool` signature/return-type fix, HTTP app host/port fix. Zero changes to the 254 `@server.tool(...)` registration call sites (decorator API unchanged). Plan: [`plans/2026-07-30-mcp-2.0-migration.md`](../superpowers/plans/2026-07-30-mcp-2.0-migration.md). | L | High | Unblocks the `<2` cap from #290; keeps MCPg on a maintained SDK line. |
| 20.2 | ✅ **Shipped.** **Tenancy redesign onto `ServerMiddleware`.** Replaced the `mcp.server.lowlevel.server.request_ctx` ambient-contextvar hack (removed in mcp 2.0) with a `ServerMiddleware` that sets the existing `current_role` ContextVar per-request — reliable on every transport now that the SDK dispatches each request to its own asyncio task, not just stdio as before. | M | Medium-High | Security-sensitive seam; see `mcpg.tenancy.TenantRoleContextMiddleware`. |
| 20.3 | ✅ **Shipped.** **Elicitation confirmation for write-tier tools.** `ctx.elicit()`-based confirmation before any non-read-only tool call, gated by `MCPG_ELICIT_CONFIRM_WRITES` (opt-in) and the connected client's declared elicitation capability. Centralized in `AuditedMCPServer.call_tool`; no per-tool changes. | S-M | Medium | First concrete use of an mcp 2.0-only capability (elicitation didn't exist in 1.x `FastMCP`). |
```
Adjust the ✅/effort/value markers to match whatever actually shipped by the time this task runs (if Task 6 turned out infeasible or was descoped, mark 20.3 accordingly rather than leaving a false "Shipped").

- [ ] **Step 2: Fix the architecture diagram and prose**

In `docs/architecture.md:18-19`:
```
    client -->|"stdio · streamable-HTTP · SSE"| fastmcp["AuditedFastMCP<br/>rate-limit · audit · metrics"]
    fastmcp -->|"capability gate<br/>(mcpg.policy)"| wrapper["Tool wrapper<br/>(mcpg.tools)"]
```
Rename the node label and id consistently (`fastmcp` → `mcpserver`, `AuditedFastMCP` → `AuditedMCPServer`) — check the full mermaid block for other references to the `fastmcp` node id before editing, since renaming a mermaid node id requires updating every edge that references it, not just these two lines.
At `docs/architecture.md:38-39`: `AuditedFastMCP.call_tool` (a `FastMCP[AppContext]` subclass)` → `AuditedMCPServer.call_tool` (a `MCPServer[AppContext]` subclass)`.
At `docs/architecture.md:258`: `**The MCP transport handler** (FastMCP-provided).` → `(MCPServer-provided).`

- [ ] **Step 3: Fix `adding-tools.md`'s prose**

At `docs/contributing/adding-tools.md:177,184`: reword "FastMCP auto-derives" / "FastMCP falls back to" to "the MCP SDK auto-derives" / "the MCP SDK falls back to" (these are about `func_metadata`'s pydantic introspection, a mechanism that lives in `mcp.server.mcpserver.utilities.func_metadata` post-migration but the *behavior* described is unchanged — verify this specific claim, about `slots=True` dataclasses breaking pydantic introspection, still holds under the new `func_metadata` module by reading `mcp/server/mcpserver/utilities/func_metadata.py` from the extracted 2.0 wheel before just renaming the word; this is exactly the kind of doc claim CLAUDE.md's "verify before you write" rule exists for).

- [ ] **Step 4: Run the doc-table contract test**

Run: `uv run pytest tests/contract/test_doc_tables.py -v`
Expected: pass (this task doesn't touch generated tables, only hand-written prose, but confirm nothing broke).

- [ ] **Step 5: Update `CHANGELOG.md`**

Add to `[Unreleased]` (check whether a new version section needs opening per `docs/release-process.md`'s convention, or whether this stays under `[Unreleased]` until the next release cuts):
```markdown
### Changed

- **Migrated to the `mcp` 2.0 SDK.** Lifts the `mcp[cli]<2` cap from the #290
  hotfix. `FastMCP` → `MCPServer`, `AuditedFastMCP` → `AuditedMCPServer`.
  Tenancy's per-request role propagation moved off the removed
  `request_ctx` ambient contextvar onto a `ServerMiddleware` — a
  simplification that makes `current_role` reliably authoritative on every
  transport (previously only guaranteed on stdio). Zero changes to any of
  the 254 tool registration call sites. See
  `docs/superpowers/plans/2026-07-30-mcp-2.0-migration.md` for the full
  rationale.

### Added

- **`MCPG_ELICIT_CONFIRM_WRITES`** (opt-in, default `false`): when set,
  every write/DDL/shell/listen/migrate-tier tool call requires an accepted
  `ctx.elicit()` confirmation before running, for clients that declare
  elicitation support. Centralized in `AuditedMCPServer.call_tool`; no
  per-tool changes.
```

- [ ] **Step 6: Final full-suite run and commit**

Run: `uv run pytest tests/unit tests/contract -q && uv run mypy src/mcpg && uv run ruff check && uv run ruff format --check`
Expected: all clean.
```bash
git add docs/feature-shortlist.md docs/architecture.md docs/contributing/adding-tools.md CHANGELOG.md
git commit -m "docs: mcp 2.0 migration roadmap row, architecture diagram, changelog"
```

---

## Self-Review

**Spec coverage:**
- Tenancy redesign (user's "all one PR" decision) → Task 5. ✅
- Elicitation extended to all write-tier tools (user's explicit choice, broader than DDL-only) → Task 6, gated on `readOnlyHint=False` which covers WRITE/DDL/SHELL/LISTEN/MIGRATE uniformly, not just DDL. ✅
- New roadmap row (user's explicit choice) → Task 9, §20 with three sub-rows matching Tasks 1–3 (core), 5 (tenancy), 6 (elicitation). ✅
- Lockfile flip atomic with code (advisor's flag: "no half-migrated state that passes CI") → Task 1 explicitly documents that the environment is import-broken between Task 1 and Task 3, and no task before Task 8 claims a fully green suite — Task 8 is the only point where "done" is asserted, and it's the last code task before docs. ✅
- `AuditedFastMCP.call_tool` return-type/signature break (advisor's flagged highest-risk item) → Task 3, Steps 1–3, with the exact old/new signatures and the verified fact that exception-based error handling still works unchanged. ✅
- `_mcp_server` → `_lowlevel_server` / `.version` settability (advisor's second flagged item) → Task 3, Step 2, verified `version` is now a direct constructor kwarg forwarded automatically — better than the advisor's framing (a deletion, not a rename). ✅
- New top-level deps conflict check (advisor's flag) → Task 1, Step 3. ✅

**Placeholder scan:** No "TBD"/"handle appropriately"/"similar to Task N" — every code step above has real, or exactly-specified-with-a-verification-command, content. The few places where an exact detail depends on the as-installed SDK version at implementation time (e.g. `sse_app`'s exact kwargs, the return-shapes snapshot's exact filename) are called out with a specific command to run and check, not left vague.

**Type consistency:** `AuditedMCPServer` / `MCPServer[AppContext]` / `Context[AppContext, Any]` spelled identically across Tasks 2, 3, 5, 6. `TenantRoleContextMiddleware` name consistent between Task 5 (definition) and Task 5/Task 9 (registration, docs). `elicit_confirm_writes` / `MCPG_ELICIT_CONFIRM_WRITES` spelled identically across Task 6's config/server/test edits and Task 9's docs.
