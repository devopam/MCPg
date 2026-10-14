# TimescaleDB tools

MCPg's TimescaleDB support, in three tiers with three gates. Everything
degrades to `available=false` when the `timescaledb` extension is not
installed. Verified against TimescaleDB 2.30.2 (see
[the design doc](plans/timescaledb-tools.md) for the evidence).

| Tier | Gate | Tools |
|---|---|---|
| Reads | READ | `list_hypertables`, `list_chunks`, `get_hypertable_stats`, `list_timescale_jobs`, `list_continuous_aggregates`, `get_timescaledb_telemetry_report` |
| Additive DDL | unrestricted + `MCPG_ALLOW_DDL` | `create_hypertable`, `add_compression_policy`, `add_retention_policy`, `add_dimension`, `set_chunk_time_interval`, `add_continuous_aggregate_policy`, `add_reorder_policy`, `remove_retention_policy`, `remove_compression_policy`, `remove_continuous_aggregate_policy`, `remove_reorder_policy` |
| Admin (destructive / heavy) | unrestricted + `MCPG_ALLOW_DDL` + `MCPG_ALLOW_TIMESCALEDB_ADMIN` | `drop_chunks`, `compress_chunk`, `decompress_chunk`, `reorder_chunk`, `merge_chunks`, `refresh_continuous_aggregate`, `run_job`, `alter_job`, `delete_job` |

## Querying with Timescale functions

`run_query` and the other read paths accept the read-only Timescale
functions (`time_bucket`, `time_bucket_gapfill`, `first`/`last`, `locf`,
`interpolate`, `histogram`, the `hypertable_*_size` and `*_stats` family,
`show_chunks`, `by_range`/`by_hash`, the uuidv7 helpers,
`timescaledb_experimental.show_policies`; list: `ALLOWED_FUNCTIONS` in
`src/mcpg/sql/allowlist.py`). Mutating functions, procedures,
`timescaledb_pre_restore` / `timescaledb_post_restore` and
`get_telemetry_report` are deliberately not on that list — use the tools
above instead.

## Admin tools: the confirmation flow

1. Call with `dry_run=true`. You get `planned_action`, the `affected`
   objects (for `drop_chunks`, the exact chunks that would be dropped), a
   `warning` to show the user and a `confirmation_token`.
2. After the user agrees, call again with `confirm=true` and that exact
   `confirmation_token`.

The token is a stateless digest of the planned statement and affected set,
so a changed plan (another chunk aged out, a different bound) asks for
confirmation again. Without a matching token nothing is executed.

Other behaviour worth knowing:

- `drop_chunks` needs at least one bound (`older_than`, `newer_than`,
  `created_before`, `created_after`; intervals like `'30 days'`).
  `created_*` cannot be combined with `older_than`/`newer_than`. Whole
  chunks only; it returns `no_op` when nothing matches.
- Jobs below id 1000 are TimescaleDB system jobs (telemetry reporter, job
  history retention); `run_job`, `alter_job` and `delete_job` refuse them.
  `alter_job` cannot change a job's `config` or procedure.
- `reorder_chunk`, `refresh_continuous_aggregate` and
  `merge_chunks(concurrently=true)` refuse to run inside a transaction block
  (checked on 2.30.2), so they run on an autocommit connection: no rollback,
  and a refresh can be left partly done if it fails. They are unavailable
  when `MCPG_DEFAULT_ROLE` / `MCPG_ALLOWED_ROLES` is set, because the raw
  connection bypasses per-role privilege separation. The other admin tools run
  in a transaction and honour the tenant role.
- Privileges are TimescaleDB's: the connection role must own the hypertable
  (or be a superuser) to manage it, and must own a job to alter/delete it.
- `get_timescaledb_telemetry_report` returns the report TimescaleDB builds
  locally (it works with the server's network disconnected). Sending is a
  separate background job (`policy_telemetry`) governed by
  `timescaledb.telemetry_level`. The report includes the database UUID, so
  treat it as sensitive.

## Not covered (yet)

`enable_chunk_skipping` (needs the `timescaledb.enable_chunk_skipping` GUC),
`move_chunk` (needs tablespaces), `split_chunk`, `attach_chunk` /
`detach_chunk`, `convert_to_columnstore` / `convert_to_rowstore`,
`add_columnstore_policy`, and `add_job` (it would let an agent schedule an
arbitrary procedure). `recompress_chunk` is deprecated upstream in favour of
`compress_chunk`.
