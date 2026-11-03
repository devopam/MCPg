# MCPg v0.8.4 — release notes

**Released:** 2026-11-03
**Tool surface:** unchanged from 0.8.3 (**279** tools with every `MCPG_ALLOW_*` gate on; 281 with `MCPG_DYNAMIC_SESSION_INTENT` also enabled).
**Tests:** 3117 passed, 3 skipped (`tests/unit` + `tests/contract`); `ruff check`, `ruff format --check` and `mypy src/mcpg` clean. CI on the merged changes passed on PG 14–19, WarehousePG and TimescaleDB.
**Runtime:** Python 3.12–3.14 (`requires-python >=3.12`)

A **0.8.3 → 0.8.4** patch release. No tool was removed or renamed, and no default changes.

## Security

- `pyasn1` ≥ 0.6.5 in the `gcp` extra (four quadratic-time DoS advisories in BER/CER/DER decoding).
- `mcp` 2.0.0 → 2.2.0 (GHSA-84m7-p3x7-pcfv, CVE-2026-59951: unbounded session accumulation in the StreamableHTTP transport).
- `certifi` ≥ 2026.10.19 (GHSA-6q48-6c7w-9gw2).
- OpenTelemetry `api`/`sdk`/`exporter-otlp-proto-http` ≥ 1.45.1 (GHSA-xcq7-p3pf-p88h).
- Dev-only floors: `virtualenv` ≥ 21.14.9, `filelock` ≥ 4.2.1, `msgpack` ≥ 1.2.3.

## Fixed

- **Tool error messages survive `mcp` 2.2.0.** MCPg's own errors keep their text; unexpected exceptions keep the SDK's generic message.
- **`run_select` keeps the validator's inner verdict** (#364). Errors now name the cause, and disallowed-statement messages no longer include AST dumps that can render literal passwords.

## Changed

- SQL allowlist: `jsonb_exists`, `jsonb_exists_any`, `jsonb_exists_all` (function forms of the already-allowed `?`, `?|`, `?&` operators; #364).

## Added

- Cursor Marketplace plugin sources (`.cursor-plugin/plugin.json`, `packaging/cursor/mcp.json`). The listing itself is a manual Cursor review.
- MCP ServerList badge in the README (#365).

Full details: [CHANGELOG](../CHANGELOG.md).
