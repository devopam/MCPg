# MCPg v0.8.0 — release notes

**Released:** 2026-08-19
**Tool surface:** **254** tools across 19 capability buckets at the
maximal (all flags on) ceiling — unchanged in count from 0.7.1. This
release adds 2 new always-visible meta-tools gated behind the new
`MCPG_DYNAMIC_SESSION_INTENT` flag, which only raise the ceiling to 256
when explicitly enabled; the default (read-only, nothing else set)
surface is unaffected by them.
**Tests:** 2909 passed, 3 skipped in the full unit suite, plus contract
snapshots, on this exact commit ahead of the tag; the full `ci.yml`
matrix (lint, mypy, security audit, PG 14–19 + WarehousePG integration)
passed on GitHub Actions.
**Runtime:** Python 3.12–3.14 (`requires-python >=3.12`; CI/mypy target
3.14)

A **0.7.1 → 0.8.0** release: a MINOR bump — new opt-in behaviour,
defaults unchanged. The headline is **dynamic session intent**
(roadmap 22), which lets a deployment start a session with a small,
focused tool surface and grow it on demand, instead of every session
seeing the full read-only default of 186 tools up front.

## New: the `core` session-intent preset

A sixth `MCPG_SESSION_INTENT` preset, `"core"`, built from `about.py`'s
curated headline tools rather than capability buckets. Setting
`MCPG_SESSION_INTENT=core` on a default read-only deployment narrows the
tool surface from 186 tools down to **12** — the ten headline
schema-introspection and query-execution tools plus the two
always-kept introspection tools. No new flag needed; this extends the
existing, shipped `session_intent.py` static filter (roadmap 8.8)
rather than replacing it.

## New: opt-in dynamic session-intent growth

**`MCPG_DYNAMIC_SESSION_INTENT`** (default `false`): when set, a session
starts at its static intent (`core` by default) and can grow its own
visible tool surface at runtime — without a server restart, and without
affecting any other concurrent session. Two new always-visible
meta-tools:

- `list_session_intents()` — shows which named intents (`lookup`,
  `migration`, `vector_rag`, `monitor`, `admin`) are available and which
  are currently enabled for this session.
- `enable_session_intent(name)` — enables one, growing this session's
  `tools/list` to include everything that intent covers, up to whatever
  the static `MCPG_SESSION_INTENT` filter already left registered.

**Important:** this is a **visibility** filter, not an **authorization**
boundary. A tool absent from `tools/list` was always still callable via
`tools/call` if a client already knew its name — that was true before
this release too. `MCPG_ACCESS_MODE` (`read-only` / `restricted` /
`unrestricted`) and the `MCPG_ALLOW_*` capability gates remain the real
permission boundary, unaffected by either the static or dynamic
session-intent layer. See the user guide's "Dynamic session intent"
section for the full model.

Off by default; adds the 2 meta-tools to the maximal 254-tool ceiling
(→ 256) only when enabled. Lives in `mcpg.session_intent` and the new
`mcpg.dynamic_session_intent`.

## New: Docker MCP Registry submission

Submitted MCPg to the [Docker MCP Registry](https://github.com/docker/mcp-registry/pull/4689)
(roadmap 21). Added `packaging/docker-mcp-registry/` with a
`server.yaml` draft and a generated `tools.json` bypass file (their
registry's own `{name, description, arguments}` shape, not MCP-native
`inputSchema`) derived from and guarded against drift by the tool
surface contract snapshot — needed because MCPg requires a live
`MCPG_DATABASE_URL` to start, which their build sandbox can't supply.

## Also added

- **`server.json` now sets `websiteUrl`**
  (`https://devopam.github.io/MCPg/`), the field the MCP Registry
  surfaces as the server's homepage link.
- **OpenSSF Best Practices badge** added to the README badge row
  (project [13958](https://www.bestpractices.dev/projects/13958),
  `passing`).

## Security hardening

- **Release SBOM + build provenance.** Cutting a release now generates
  a CycloneDX SBOM and attests GitHub-native build provenance for the
  wheel + sdist, complementing the PEP 740 attestations PyPI Trusted
  Publishing already emits. Added `.github/CODEOWNERS`.
- **Pipeline-security tooling**: StepSecurity Harden-Runner (egress
  monitoring on every job), zizmor (static analysis of the Actions
  workflows), and actionlint — all in reporting mode initially.
- **zizmor baseline cleared**: scoped `id-token: write` down to only the
  five OIDC jobs that need it, and set `persist-credentials: false` on
  all checkouts. zizmor now reports no findings.

## Fixed

- **`server.json`'s checked-in version no longer drifts.** It tracked a
  stale release (last hand-bumped at `0.6.8`) because the registry
  publish job only patched a version derived from the tag in the CI
  checkout, never committing it back. Added
  `tools/sync_server_json_version.py` (mirrors the existing `.mcpb`
  bundle sync script), guarded by `tests/unit/test_server_json.py`.

## Upgrade impact

- **No breaking changes.** All new behaviour is opt-in
  (`MCPG_SESSION_INTENT=core`, `MCPG_DYNAMIC_SESSION_INTENT=1`); an
  unconfigured deployment sees the same 186-tool read-only /
  254-tool maximal surface as before.
- Deployments that want a smaller default surface for a given
  connection profile (e.g. a read-only lookup client) can now set
  `MCPG_SESSION_INTENT=core` without waiting for a per-session dynamic
  flow; deployments that want sessions to self-serve a larger surface
  on demand can add `MCPG_DYNAMIC_SESSION_INTENT=1` on top.

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.8.0   # or :latest
```

Or grab `mcpg-0.8.0.mcpb` from this release and double-click it into
Claude Desktop.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.8.0]` for the complete
itemised list.
