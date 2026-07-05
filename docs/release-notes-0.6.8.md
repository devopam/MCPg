# MCPg v0.6.8 — release notes

**Released:** 2026-07-05
**Tool surface:** **252** tools across 19 capability buckets
**Tests:** 2746 pass (PG 14 / 15 / 16 / 17 / 18 / 19 / WarehousePG)
**Runtime:** Python 3.14

This is a **patch-level bump (0.6.7 → 0.6.8)** focused entirely on
adoption: installing MCPg into *any* MCP client is now a one-click or
one-snippet affair. No tool signatures changed; everything is
backward-compatible.

## Headline: one-click installs, everywhere

- **Claude Desktop** — this release attaches the first
  **`mcpg-<version>.mcpb`** desktop-extension bundle: download,
  double-click, paste your connection URL (stored in the OS keychain),
  done. The bundle is ~2 kB and works on every platform/architecture —
  it uses the MCPB `uv` server type, so Claude Desktop resolves the
  pinned `mcpg` release from PyPI at install time. Access mode
  defaults to read-only.
- **Cursor** and **VS Code** — one-click install badges in the README,
  via the official HTTPS deeplink endpoints. The VS Code install
  prompts for the connection URL as a **masked input**, so it never
  lands in plain-text settings.
- **Everything else** — the new
  [client integrations guide](integrations.md) covers 14 clients with
  copy-paste configs: Windsurf, JetBrains AI Assistant, Cline/Roo
  Code, Zed, Google Antigravity (+ Gemini CLI), Qwen Code, Perplexity,
  ChatGPT (remote connectors), Microsoft Copilot Studio, Continue,
  Claude Code CLI, and generic streamable-HTTP clients — plus a
  straight answer on Aider (no native MCP support upstream yet) and
  DeepSeek (a model provider whose models drive MCPg *through* these
  clients).

## Directory-ready metadata

Rounding out v0.6.7's `readOnlyHint` work, every tool now also carries:

- a **human-readable title**, auto-derived from the tool name with an
  acronym/product-name table (`recommend_ivfflat_probes` →
  "Recommend IVFFlat probes"), and
- an explicit **`destructiveHint`** on all 67 write-capable tools,
  from a curated destructive (18) / non-destructive (49) partition. A
  contract test requires the partition to cover the write surface
  exactly, so a new write tool cannot ship unclassified.

Together with the new **`PRIVACY.md`** (self-hosted, no telemetry, one
documented external call), MCPg now meets the published metadata
requirements for connector-directory listings.

## Also fixed

- **GitHub Release bodies** had shipped the "See CHANGELOG.md" stub on
  every release since automation was added — an awk range-pattern bug
  in the extraction step. This release is the first with the fix in
  effect; the body you're (hopefully) reading it in came from the
  changelog.

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.6.8   # or :latest
```

Or grab `mcpg-0.6.8.mcpb` from this release and double-click it into
Claude Desktop. No configuration changes required.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.8]` for the complete
itemised list.
