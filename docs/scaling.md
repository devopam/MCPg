# MCPg scaling characteristics

How MCPg behaves under load, the levers you have to tune it, and
the bottlenecks to plan around.

---

## Execution model

- MCPg is a single-process `asyncio` server. Tool calls are
  coroutines; CPU work (SQL parsing / validation) runs on a single
  event-loop thread.
- Database work is I/O-bound and runs concurrently up to the
  connection pool's `max_size`.
- Each `run_select` / `explain_query` / NL→SQL execution
  constructs a `SafeSqlDriver` and parses the SQL with `pglast`
  before execution — a small, bounded CPU cost per call.
- For HTTP transports, Starlette + the FastMCP transport handler
  share the same event loop; metrics / health endpoints are
  separately cheap.

---

## Connection pool sizing

The pool is bounded by `MCPG_POOL_MIN_SIZE` / `MCPG_POOL_MAX_SIZE`
(defaults `1` / `5`).

- **`max_size`** is the ceiling on concurrent in-flight database
  queries. Set it to the expected peak concurrency of the agent(s)
  using the server.
- Do not exceed what the PostgreSQL server can accommodate
  (`max_connections`, shared across all clients). The
  `check_database_health` tool reports current connection use.
- **`min_size`** keeps warm connections ready; `1` is fine for
  light use, raise it for latency-sensitive workloads to skip the
  cold-start round-trip on first request.
- Each checkout sets per-session `statement_timeout`
  (`MCPG_STATEMENT_TIMEOUT_MS`, default 30 s) and `lock_timeout`
  (`MCPG_LOCK_TIMEOUT_MS`, default 5 s) once and caches the fact;
  subsequent checkouts on the same connection skip the `SET`.

### Cursor pool overhead

Each open server-side cursor holds a **dedicated connection**
outside the main pool — for the cursor's lifetime. Long-lived
cursors don't starve the main pool but do count against the
PostgreSQL server's `max_connections`. The 5-minute idle TTL
caps drift; `list_cursors()` makes the population visible.

### Replica pool overhead

Each entry in `MCPG_REPLICA_URLS` gets its own pool sized
identically to the primary (`MCPG_POOL_MIN_SIZE` /
`MCPG_POOL_MAX_SIZE`). Plan for `(1 + N_replicas) ×
MCPG_POOL_MAX_SIZE` peak connections across the database fleet.

---

## Read-replica routing

When `MCPG_REPLICA_URLS` is non-empty, every
`force_readonly=True` query is round-robin-routed to a healthy
replica. Failures fall back to the primary once and mark the
replica degraded for 30 s.

Capacity guidance:

- `force_readonly=True` covers the majority of read tools
  (`run_select`, introspection, search, health checks). Writes
  always go to the primary.
- Adding replicas linearly scales read capacity until the primary
  becomes the bottleneck on write throughput or replication
  bandwidth.
- Routing decisions land in Prometheus
  (`mcpg_tool_calls_total{tool="__replica_route",bucket="unknown",status="…"}`)
  with statuses `primary` / `primary_no_healthy` / `fallback` /
  `replica_<n>`.

Monitor `list_replicas()` for the degraded flag, last error, and
seconds-until-retry per replica.

---

## Result-size bounds

- `run_select` caps rows at `max_rows` (default 1000) and reports
  `truncated`. Bounds memory and payload size per call.
- `run_select_parallel` enforces `parallel_limit` (default 8) on
  concurrent statements.
- For result sets larger than `max_rows`, use **server-side
  cursors** — `open_cursor` / `fetch_cursor(batch_size=…)` /
  `close_cursor`. Each cursor's dedicated connection means even
  million-row scans don't tie up the main pool, and `batch_size`
  gives the agent backpressure.
- Bulk exports (`export_query` / `export_table`) hit the same
  `max_rows`-style limit (param `limit`, default 10000).
- Bulk loads (`import_csv` / `import_json`) use `COPY FROM STDIN`
  + parametrised `executemany`; throughput is database-bound.

---

## Subprocess workloads

`dump_database` / `restore_database` / `copy_table_between_databases`
spawn `pg_dump` / `psql` / `pg_restore` with:

- **Wall-clock cap**: `MCPG_SHELL_TIMEOUT_SEC` (default 60 s).
- **Output cap**: `MCPG_SHELL_MAX_OUTPUT_BYTES` (default 64 MiB).

Subprocess CPU runs outside the event loop, so it doesn't block
other tool calls — but the captured output sits in memory until
the subprocess completes. Plan accordingly for big dumps.

---

## Rate limiting

`MCPG_RATE_LIMIT_ENABLED=true` activates a token-bucket per-tool
limiter:

- **Global quota.** `MCPG_RATE_LIMIT_MAX_REQUESTS` per
  `MCPG_RATE_LIMIT_WINDOW_SECONDS` across all tools (defaults
  60 / 60 s).
- **Heavy quota.** `MCPG_RATE_LIMIT_HEAVY_MAX` per
  `MCPG_RATE_LIMIT_HEAVY_WINDOW` for the computationally heavy tools
  (`analyze_workload`, `audit_database`, `generate_test_data`,
  `export_table`, `export_query`) (defaults 5 / 60 s).

Rate-limited calls return an error immediately rather than
queueing.

---

## Measured baseline

A baseline from `benchmarks/bench.py` — 2000 `run_select` calls
of `SELECT 1`, concurrency 16, against a loopback PostgreSQL 16:

| Metric | Value |
|---|---|
| Throughput | ~2,200 req/s |
| Latency p50 | ~6.8 ms |
| Latency p95 | ~11 ms |

This measures the MCPg query path (safety validation + pool +
round-trip) against a trivial query on the same host — not the MCP
transport, not network latency, not query complexity. Real
workloads vary; re-run against your own environment:

```bash
uv run python benchmarks/bench.py --requests 2000 --concurrency 16 \
    --database-url postgresql://...
```

---

## Observability for capacity planning

The HTTP transport exposes Prometheus metrics at `GET /metrics`:

- `mcpg_tool_calls_total{tool,bucket,status}` — counter; one of the
  best signals for "is X being called too often / failing too
  often". `status` ∈ `ok` / `error` plus the replica-routing
  statuses noted above; `bucket` is the capability bucket the tool
  routes into (aggregate with `sum by (bucket) (…)`).
- `mcpg_tool_duration_seconds_*` — histogram per tool; bucket
  defaults are SDK-shaped (0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
  0.5, 1, 2.5, 5, 10, 30, 60 s).

For stdio deployments, `get_metrics_exposition()` returns the same
Prometheus payload as a string the agent can hand to a scrape
target.

Together with `check_database_health` and `audit_database`, these
cover the three axes you need:

- **MCPg-side load** — tool calls per second, latency percentiles
  (Prometheus).
- **Database-side load** — connections in use, cache hit ratio,
  bloat, lock waits (`check_database_health` /
  `audit_database`).
- **Workload composition** — slow query templates from
  `pg_stat_statements` (`analyze_workload`, `detect_n_plus_one`).

---

## Bottlenecks & guidance

- **Database round-trip latency dominates** for simple queries —
  co-locate MCPg with the database, or accept the network RTT in
  the latency figure.
- **Pool saturation.** If concurrency exceeds `max_size`, calls
  queue on the pool. Raise `MCPG_POOL_MAX_SIZE` (within
  `max_connections`), or add replicas if reads dominate.
- **Single event loop.** Very high call rates are bounded by
  single-thread CPU for SQL parsing. For very high throughput,
  run multiple MCPg instances behind the streamable-HTTP transport
  with a load balancer.
- **Large result sets.** Use server-side cursors instead of
  bumping `max_rows`. They give the agent backpressure and don't
  buffer the whole result in MCPg memory.
- **Heavy DBA tools** (`audit_database`, `analyze_workload`,
  `generate_test_data`, `export_table`, `export_query`) are
  I/O-heavy on the database. Rate-limit them via
  `MCPG_RATE_LIMIT_HEAVY_*` if an agent calls them on a tight loop.

---

## Horizontal scale-out shape

For deployments needing more capacity than a single MCPg process
can provide:

1. **HTTP transport in front of a load balancer.** MCPg is
   stateless aside from the audit logger and the open-cursor
   table — a sticky session is needed only if your workflow
   depends on a single client holding a cursor across calls
   (otherwise round-robin works).
2. **Replica pool for read fan-out.** `MCPG_REPLICA_URLS` is the
   first lever for read-heavy workloads.
3. **Tenant-per-database isolation.** When tenants run in
   separate databases, run one MCPg instance per tenant with a
   tenant-specific `MCPG_DATABASE_URL`. When tenants share a
   database, the single-process `SET LOCAL ROLE` workflow
   (`MCPG_DEFAULT_ROLE` / OIDC role claim / `X-MCPG-Role`) is
   typically a better fit.

---

## See also

- [README → Configuration](../README.md#configuration) for the
  full env-var reference.
- [`installation.md`](installation.md) for the install / deploy
  paths.
- [`user-guide.md`](user-guide.md) for the feature-by-feature
  walkthrough, including replicas / cursors / rate limiting.
- [`architecture.md`](architecture.md) for the module map and
  the request lifecycle.
