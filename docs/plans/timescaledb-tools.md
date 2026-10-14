# TimescaleDB tool family — design

Status: **implemented** in the PR for issue #353 (2026-10-14); the open
questions below were answered by the Phase 0 spike (see "Phase 0 results").
Spawned by issue #353 and the ALTER SYSTEM tools (PR #352).

## Why now

MCPg's TimescaleDB support (`src/mcpg/timescaledb.py`, shipped long ago)
is five tools: `list_hypertables`, `list_chunks` (read) and
`create_hypertable`, `add_compression_policy`, `add_retention_policy`
(DDL-gated). Two things have changed:

1. Issue #353 shows users want to *query* with Timescale functions
   (`time_bucket`, `first`/`last`, size/stats functions). The SQL-safety
   allowlist change for the read-only ones is a separate small PR.
2. Everything else Timescale offers (drop chunks, manual compression,
   jobs, continuous-aggregate refresh, telemetry) is reachable only via
   `run_ddl`/`run_sql`, which cannot run most of it.

**Finding while preparing this doc:** there is no TimescaleDB lane in CI
and no Timescale integration test (`tests/integration` and
`.github/workflows` mention it nowhere). The five existing tools are
covered by unit tests with a mocked driver only. Building more on that
without a real instance is the main risk, hence Phase 0 below.

## Verified facts vs. open questions

Verified against the vendor API reference (www.tigerdata.com/docs/api/latest,
fetched 2026-10-14):

- `drop_chunks(relation, older_than, newer_than, verbose, created_before,
  created_after)` returns one row per dropped chunk (text). Whole chunks
  only. `created_*` cannot be combined with `older_than`/`newer_than`.
  Interval arguments assume the past.
- `refresh_continuous_aggregate` is a **procedure** (`CALL`), returns void;
  args `continuous_aggregate`, `window_start`, `window_end`, `force`
  (default false), `options` (jsonb: `buckets_per_batch`,
  `max_batches_per_execution`, `refresh_newest_first`). Each batch runs in
  its own transaction.
- `run_job(job_id)` is a procedure; "cannot be executed inside a SELECT
  query but has to be executed with `CALL`".
- `get_telemetry_report()` returns one `telemetry_report` jsonb column:
  the report that would be sent to the vendor; works with telemetry off.

**Not verified (docs silent) — must be settled by Phase 0 on a real
instance:**

- Whether the procedures (`refresh_continuous_aggregate`, `run_job`,
  `convert_to_columnstore`, `merge_chunks`, `split_chunk`, ...) refuse to
  run inside a transaction block. Determines whether they need the
  autocommit path (`Database.run_unmanaged`, used by ALTER SYSTEM/VACUUM)
  or can use the normal transactional write path.
- Whether calling `get_telemetry_report()` itself transmits anything.
  (The vendor page describes the *report* as sent in the background, not
  the function.) Check with `timescaledb.telemetry_level = off` plus a
  network capture or source read before exposing it.
- Behaviour/availability of the `*_columnstore_*` names across Timescale
  2.x versions (issue #353 is from 2.30.2; older versions lack them).
- Privilege requirements per function (owner vs. superuser vs. job owner).

## Scope

In scope: read tools beyond hypertable/chunk listing; gated mutating
tools; procedure calls; telemetry read; CI coverage.
Out of scope: Timescale Cloud/Tiger-specific APIs, multi-node
(removed upstream), `timescaledb_pre_restore`/`post_restore` (they flip
global restore state; belongs to a dump/restore runbook, not an agent tool).

## Proposed surface

All tools take `schema`/`table` as separate validated identifiers (existing
`_check_identifier`/`_regclass_literal` helpers), validate intervals with
the existing `_check_interval`, return typed dataclasses, and report
`available=false` when the extension is missing — same conventions as
today's tools.

### Tier R — reads (READ gate)

| Tool | Wraps |
|---|---|
| `get_hypertable_stats` | `hypertable_detailed_size`, `hypertable_compression_stats` / `hypertable_columnstore_stats` (version-detected), `approximate_row_count` |
| `list_timescale_jobs` | `timescaledb_information.jobs` + `job_stats` (next start, last status, failures) |
| `list_continuous_aggregates` | `timescaledb_information.continuous_aggregates` |
| `get_timescaledb_telemetry_report` | `get_telemetry_report()` — only after the Phase 0 check; otherwise dropped |

### Tier A — additive/reversible (DDL gate, as today)

`add_dimension`, `set_chunk_time_interval`, `enable_chunk_skipping`,
`add_continuous_aggregate_policy`, `add_reorder_policy`, and the matching
`remove_*_policy` tools. Same gate as `create_hypertable`:
unrestricted mode + `MCPG_ALLOW_DDL`.

### Tier B — destructive or heavy (separate gate + confirm)

`drop_chunks`, `compress_chunk`, `decompress_chunk`, `reorder_chunk`,
`move_chunk`, `alter_job`, `delete_job`, `run_job`,
`refresh_continuous_aggregate`, and the columnstore/merge/split
procedures once verified.

Design follows the ALTER SYSTEM tools (see `docs/alter-system.md` on PR #352):

- Structured arguments only, no free-form SQL.
- `dry_run=true` returns the plan. For `drop_chunks` it lists the chunks
  that *would* be dropped (via `show_chunks` with the same bounds) so the
  agent can show the user real names and a count.
- Without `confirm=true` the tool returns `status=confirmation_required`
  plus a warning to show the user; no change is made.
- Gate: unrestricted mode + `MCPG_ALLOW_DDL` + new `MCPG_ALLOW_TIMESCALEDB_ADMIN`.
  Open decision: a new flag (more config, clearer blast-radius) vs. reuse
  `MCPG_ALLOW_DDL` with confirm only. Recommendation: new flag, because
  `drop_chunks` deletes data and DDL-allow today implies "structure, not
  data".
- Procedures use `CALL` through the execution path Phase 0 shows is
  required; if autocommit is needed, reuse `run_unmanaged`.

### SQL-safety allowlist (separate, small PR)

Read-only function names go in `sql/allowlist.py` (`ALLOWED_FUNCTIONS`);
mutating functions and procedures are never added there, since the safe
driver is the read-only path. Done on branch
`fix/timescaledb-readonly-functions-353`.

## Phasing

0. **Spike + CI lane.** Add a TimescaleDB integration lane (image with the
   extension, `continue-on-error` initially like PG 19/WarehousePG), first
   tests covering today's five tools. Answer the open questions above and
   record the results here.
1. Tier R tools (+ telemetry if cleared).
2. Tier A tools.
3. Tier B tools with confirm flow and gate flag.
Each phase: module code, registration, bucket in `about.py`, tests,
both contract snapshots, doc tables regenerated, CHANGELOG
(`docs/contributing/adding-tools.md`).

## Risks

- Behaviour differs across Timescale 2.x; tools must detect version/function
  presence and degrade (`available=false` / clear message), not assume.
- `drop_chunks` and retention interplay with continuous aggregates can
  silently lose raw data that aggregates still depend on; warnings must say so.
- Tool-count growth (each phase adds tools); keep names/buckets disciplined.

## Phase 0 results (TimescaleDB 2.30.2, PG 17, official image)

Run against a throwaway container, not from the vendor docs:

| Question | Result |
|---|---|
| Procedures in a transaction block? | **Cannot**: `refresh_continuous_aggregate`, `merge_chunks_concurrently`, and the function `reorder_chunk` fail with "cannot run inside a transaction block". **Can**: `run_job`, `convert_to_rowstore`/`columnstore`, `merge_chunks`, `detach_chunk`, `split_chunk`, `drop_chunks` (rolls back cleanly), `compress_chunk`, `alter_job`, `delete_job`, `add_job`, `add_dimension`, `set_chunk_time_interval`, the `add_*_policy` functions. |
| `get_telemetry_report()` sends data? | No. It returns the locally built report with the server's network disconnected. Uploading is the separate `policy_telemetry` job (job 1), governed by `timescaledb.telemetry_level` (default `basic`). The report includes `db_uuid`. |
| `*_columnstore_*` on 2.30.2? | Present, alongside the `*_compression_*` names; the stats functions return **no row** for a hypertable without compression enabled. |
| Privileges | A table owner (non-superuser) can create hypertables, add/alter policies, `run_job`, `drop_chunks` and read `get_telemetry_report()` for their own tables; another owner's hypertable gives "must be owner"; deleting another owner's job gives "insufficient permissions". `pg_file_settings` is superuser-only (relevant to ALTER SYSTEM `RESET ALL`, which fails closed). |
| Other findings | `enable_chunk_skipping` errors unless `timescaledb.enable_chunk_skipping` is on; `recompress_chunk` is deprecated in favour of `compress_chunk`; user/policy job ids start at 1000, system jobs are below; `continuous_aggregates.finalized` does not exist in 2.30.2. |

## Final scope

- Reads: `get_hypertable_stats`, `list_timescale_jobs`, `list_continuous_aggregates`, `get_timescaledb_telemetry_report`.
- Additive DDL (`MCPG_ALLOW_DDL`): `add_dimension`, `set_chunk_time_interval`, `add_continuous_aggregate_policy`, `add_reorder_policy`, four `remove_*_policy` tools.
- Admin (`MCPG_ALLOW_TIMESCALEDB_ADMIN`, decided: separate flag): `drop_chunks`, `compress_chunk`, `decompress_chunk`, `reorder_chunk`, `merge_chunks` (`concurrently` option), `refresh_continuous_aggregate`, `run_job`, `alter_job`, `delete_job`. Autocommit paths (`run_unmanaged`) only where the spike showed they are required, and they are refused under tenancy.
- Telemetry is a normal read tool (verified local-only).
- Excluded: `enable_chunk_skipping`, `move_chunk`, `split_chunk`, `attach_chunk`/`detach_chunk`, `convert_to_*`, `add_columnstore_policy`, `add_job` (lets an agent schedule an arbitrary procedure), `recompress_chunk` (deprecated), `timescaledb_pre/post_restore`.
- CI: a `TimescaleDB` job (experimental) runs `tests/integration/test_timescaledb_integration.py` against `timescale/timescaledb:latest-pg17`.

User-facing reference: [docs/timescaledb.md](../timescaledb.md).
