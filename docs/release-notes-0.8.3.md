# MCPg v0.8.3 — release notes

**Released:** 2026-10-14
**Tool surface:** **279** tools with every `MCPG_ALLOW_*` gate on (281 with
`MCPG_DYNAMIC_SESSION_INTENT` also enabled) — up from 254 / 256 in 0.8.2.
The default read-only surface grows by five read tools (186 → 191); every
other new tool needs an explicit opt-in flag.
**Tests:** 3105 passed, 3 skipped (`tests/unit` + `tests/contract`, snapshots regenerated and committed); plus 7 TimescaleDB integration tests against a live TimescaleDB 2.30.2; lint, `ruff format`, `mypy src/mcpg`, and `bandit` clean.
**Runtime:** Python 3.12–3.14 (`requires-python >=3.12`; CI/mypy target
3.14)

A **0.8.2 → 0.8.3** release. It adds two tool families (`ALTER SYSTEM`
configuration tools and a much larger TimescaleDB surface), answers
[#353](https://github.com/devopam/MCPg/issues/353), and bumps `pglast`. No
tool was removed or renamed, and no existing default changes.

## Added: `ALTER SYSTEM` configuration tools (#352)

`inspect_guc` (always available, read-only), plus `alter_system`,
`alter_system_reset_all` and `reload_config` behind unrestricted mode and
`MCPG_ALLOW_ALTER_SYSTEM`. They run on an autocommit connection (`ALTER SYSTEM`
cannot run in a transaction) and follow a `dry_run` → `confirm=true` +
`confirmation_token` flow, where the token is derived from the exact
statement. Hardened during review: values are quoted safely regardless of
`standard_conforming_strings`; connectivity, preload and command-running
settings (`archive_command`, `restore_command`, `primary_conninfo`, …) are
denied by default; `RESET ALL` applies the allow/deny lists to every current
override and fails closed; `inspect_guc` redacts secret-bearing values;
reload success reflects what `pg_reload_conf()` returns; the tools refuse to
run when `MCPG_DEFAULT_ROLE` / `MCPG_ALLOWED_ROLES` is set. Reference:
[`alter-system.md`](alter-system.md).

## Added: TimescaleDB (#353, #354)

- **SQL-safety allowlist:** the read-only functions from the issue
  (`time_bucket`, `time_bucket_gapfill`, `first`/`last`, `locf`, `interpolate`,
  `histogram`, the size/stats family, `show_chunks`, `by_range`/`by_hash`, the
  uuidv7 helpers, `timescaledb_experimental.show_policies`) now pass the
  read-only SQL path. Mutating functions, procedures, the restore helpers and
  `get_telemetry_report` stay blocked.
- **21 new tools:** reads (`get_hypertable_stats`, `list_timescale_jobs`,
  `list_continuous_aggregates`, `get_timescaledb_telemetry_report`); additive
  DDL under `MCPG_ALLOW_DDL` (`add_dimension`, `set_chunk_time_interval`,
  `add_continuous_aggregate_policy`, `add_reorder_policy`, four
  `remove_*_policy`); and admin tools behind the new
  `MCPG_ALLOW_TIMESCALEDB_ADMIN` flag with the same `dry_run` / `confirm` +
  `confirmation_token` flow (`drop_chunks`, `compress_chunk`,
  `decompress_chunk`, `reorder_chunk`, `merge_chunks`,
  `refresh_continuous_aggregate`, `run_job`, `alter_job`, `delete_job`).
- **Verified on TimescaleDB 2.30.2.** `refresh_continuous_aggregate`,
  `merge_chunks_concurrently` and `reorder_chunk` cannot run in a transaction
  block, so they use an autocommit connection and are refused under tenancy;
  `get_telemetry_report()` builds its report locally. A new experimental
  `TimescaleDB` CI job runs the integration tests. Reference:
  [`timescaledb.md`](timescaledb.md); design and test evidence:
  [`plans/timescaledb-tools.md`](plans/timescaledb-tools.md).

## Changed

- `pglast` 8.4 → 8.5 (#350). The SQL-safety kernel's adversarial and fuzz
  suites pass on 8.5.
- The contract fixtures now enable `MCPG_ALLOW_ALTER_SYSTEM` and
  `MCPG_ALLOW_TIMESCALEDB_ADMIN`, so the tool-surface snapshot (281 tools),
  annotations and return-shape contracts cover the new write tools.

## Fixed

- The `docs/tools.md` tool-index heading and the hand-written tool counts
  (README, tour, user guide, project notes) were stale and are regenerated.

## Upgrading

Nothing to do: the new write and admin tools stay off unless you set
`MCPG_ALLOW_ALTER_SYSTEM` or `MCPG_ALLOW_TIMESCALEDB_ADMIN` (the latter also
needs unrestricted mode and `MCPG_ALLOW_DDL`).
