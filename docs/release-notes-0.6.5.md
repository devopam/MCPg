# MCPg v0.6.5 — release notes

**Released:** 2026-06-30
**Tool surface:** **246** tools across 19 capability buckets
**Tests:** 2552 pass (PG 14 / 15 / 16 / 17 / 18 / 19)
**CI:** PG 14-19 + an experimental WarehousePG lane

This is a **patch-level bump (0.6.4 → 0.6.5)** carrying a single
headline feature — the multi-database selector (roadmap 13.1). Every
addition is backward-compatible: the primary-only path is byte-for-byte
unchanged when the new env var is unset, so existing deployments upgrade
with no configuration changes.

## Headline: one server, multiple databases (read-only secondaries)

A single MCPg server can now serve **multiple databases**. The primary
(`MCPG_DATABASE_URL`) stays the default target of every tool; additional
named, **read-only** secondaries are configured via the new
`MCPG_SECONDARY_DATABASE_URLS` env var — comma- or newline-separated
`name=dsn` entries, e.g.
`analytics=postgresql://…,reporting=postgresql://…`.

Every **read-capable tool** gains an optional `database` argument that
selects a secondary by name; omitting it targets the primary. The new
**`list_databases`** READ tool discovers every configured database id,
which one is primary, each `read_only` flag, and a live `SELECT 1`
reachability probe.

### Read-only is enforced by PostgreSQL, not by convention

Secondaries are read-only at the server level: every query against a
secondary runs inside a `BEGIN TRANSACTION READ ONLY`, so a stray write
fails closed with SQLSTATE 25006 even if the calling tool forgot to mark
itself read-only. This is the design boundary that keeps the feature
safe — it sidesteps per-database write/DDL/LISTEN gating entirely:

- **Write / DDL / shell / listen / migrate tools always target the
  primary** — they never carry the `database` argument.
- The global `MCPG_ACCESS_MODE` / `MCPG_ALLOW_DDL` / `MCPG_ALLOW_SHELL`
  / `MCPG_ALLOW_LISTEN` gates continue to apply to the primary exactly
  as before; secondaries simply can't be written to.

Operational niceties: secondary DSNs get the same TLS enforcement as the
primary; names must be simple identifiers (`[a-z0-9_]+`), unique, and
not the reserved `primary`; and a secondary that fails to open at
startup is marked unavailable (surfaced by `list_databases`) rather than
aborting startup — mirroring the existing read-replica tolerance.

## Upgrade

```bash
pip install --upgrade mcpg   # once published to PyPI
```

No configuration changes required. Multi-database support is opt-in —
leave `MCPG_SECONDARY_DATABASE_URLS` unset and the server behaves
exactly as in 0.6.4.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.5]` for the complete
itemised list.
