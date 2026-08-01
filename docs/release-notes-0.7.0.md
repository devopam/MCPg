# MCPg v0.7.0 — release notes

**Released:** 2026-08-01
**Tool surface:** **254** tools across 19 capability buckets (read-only
mode exposes a subset) — unchanged from 0.6.12; this release ships zero
tool additions or removals.
**Tests:** full unit + contract suite green locally on this branch. The
integration matrix (PG 14–18 required, PG 19 + WarehousePG experimental)
runs via `ci.yml` on push to `main`/`claude/**` or on an opened PR — not
yet exercised for this exact commit, since it hasn't been pushed.
**Runtime:** Python 3.12–3.14 (`requires-python >=3.12`; CI/mypy target
3.14)

A **0.6.12 → 0.7.0** release: the `mcp` 2.0 SDK migration that was
capped off in 0.6.12 (`mcp[cli]<2`, PR #290) is now complete. This is a
**breaking** release for anyone pinned to `mcp` 1.x — see "Upgrade
impact" below before updating.

## Migrated to the `mcp` 2.0 SDK

Lifts the `mcp[cli]<2` interim cap. The upstream SDK's `2.0.0` renamed
`mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer` and
restructured the module tree around it, with no back-compat shim. MCPg
now depends on `mcp>=2.0.0` and its own server class,
`mcpg.server.AuditedFastMCP`, is renamed to `mcpg.server.AuditedMCPServer`
(also exported in `__all__`) to match — **no backward-compatibility
alias** is provided for the old name. Zero changes to any of the 254
tool registration call sites; the SDK's own HTTP entry point
(`streamable_http_app`/`sse_app`) moved its `host` argument off the
constructor, and MCPg's `http_runtime` module was updated to pass it at
call time instead. See `docs/plans/mcp-2.0-migration.md` for the full
migration rationale and task-by-task history.

## Tenancy redesigned onto `ServerMiddleware`

Per-request role propagation moved off the removed `request_ctx` ambient
contextvar hack onto a proper `ServerMiddleware`. This is a
simplification, not just a port: `current_role` is now reliably
authoritative on **every** transport (previously only guaranteed on
stdio), closing a gap where HTTP/SSE requests could see a stale or
default role under the old contextvar approach.

## New: opt-in write confirmation via elicitation

**`MCPG_ELICIT_CONFIRM_WRITES`** (default `false`): when set, every
write/DDL/shell/listen/migrate-tier tool call requires an accepted
`ctx.elicit()` confirmation before running, for MCP clients that declare
elicitation support. Centralized in `AuditedMCPServer.call_tool` — no
per-tool changes were needed to support it. Off by default; existing
deployments see no behaviour change unless they opt in.

## Fixed: README "Listed On" rendering on the published docs site

The GitHub Pages site (Jekyll + kramdown, built from `README.md`) was
rendering the "Glama" bullet in the "📍 Listed On" list as an oversized
`<h2>` heading instead of a plain bulleted link, while the same section
looked correct on GitHub.com. Root cause: the bullet list ended directly
against the `---` horizontal-rule divider with no blank line separating
them; GitHub's cmark renderer reads `---` after a list item as a
thematic break, but kramdown reads it as a Setext-heading underline for
the immediately preceding line when there's no blank line first. Fixed
by inserting the blank line. A repo-wide grep for the same precondition
(a non-blank line immediately followed by a bare `---`/`***`/`___`
line, excluding legitimate YAML front-matter closes) turned up no other
instances.

## Upgrade impact

- **Dependency floor moves to `mcp>=2.0.0`.** This is a hard break for
  any deployment pinned to `mcp` 1.x — the resolved `mcp` dependency no
  longer offers the 1.x `mcp.server.fastmcp.FastMCP` API. Upgrade `mcp`
  alongside MCPg; there is no dual-support window.
- **`mcpg.server.AuditedFastMCP` → `mcpg.server.AuditedMCPServer`**, with
  **no backward-compatibility alias**. If anything imports
  `AuditedFastMCP` directly (it was exported in `__all__`), update the
  import name to `AuditedMCPServer`.
- Tool behaviour, tool signatures, and the 254-tool surface are
  otherwise unchanged — this is an SDK/internals migration, not a
  tool-surface change.

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.7.0   # or :latest
```

Or grab `mcpg-0.7.0.mcpb` from this release and double-click it into
Claude Desktop.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.7.0]` for the complete
itemised list.
