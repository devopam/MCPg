# MCPg Scaling Characteristics

How MCPg behaves under load, and how to size it. Updated as the project
evolves.

## Execution model

- MCPg is a single-process `asyncio` server. Tool calls are coroutines; CPU
  work (SQL parsing/validation) runs on one event-loop thread.
- Database work is I/O-bound and runs concurrently up to the connection
  pool's `max_size`.
- Each `run_select` / `explain_query` call constructs a `SafeSqlDriver` and
  parses the SQL with `pglast` before execution — a small, bounded CPU cost
  per call.

## Connection pool sizing

The pool is bounded by `MCPG_POOL_MIN_SIZE` / `MCPG_POOL_MAX_SIZE`
(defaults `1` / `5`).

- `max_size` is the ceiling on concurrent in-flight database queries. Set it
  to the expected peak concurrency of the agent(s) using the server.
- Do not exceed what the PostgreSQL server can accommodate
  (`max_connections`, shared across all clients). The `check_database_health`
  tool reports connection utilisation.
- `min_size` keeps warm connections ready; `1` is fine for light use, raise
  it for latency-sensitive workloads.

## Result-size bounds

- `run_select` caps rows at `max_rows` (default 1000) and reports
  `truncated`. This bounds memory and payload size per call.
- Large reads should paginate with SQL `LIMIT`/`OFFSET`. Streaming via
  server-side cursors is a planned post-1.0 enhancement.

## Measured baseline

A baseline from `benchmarks/bench.py` — 2000 `run_select` calls of
`SELECT 1`, concurrency 16, against a loopback PostgreSQL 16:

| Metric        | Value       |
|---------------|-------------|
| Throughput    | ~2,200 req/s |
| Latency p50   | ~6.8 ms     |
| Latency p95   | ~11 ms      |

This measures the MCPg query path (safety validation + pool + round-trip),
not the MCP transport, against a trivial query on the same host. Real
workloads with network latency and heavier queries will differ — re-run the
benchmark against your own environment:

```bash
uv run python benchmarks/bench.py --requests 2000 --concurrency 16 \
    --database-url postgresql://...
```

## Bottlenecks & guidance

- **Database round-trip latency dominates** for simple queries — co-locate
  MCPg with the database, or accept network RTT in the latency figure.
- **Pool saturation:** if concurrency exceeds `max_size`, calls queue. Raise
  `MCPG_POOL_MAX_SIZE` (within `max_connections`).
- **Large result sets:** rely on `max_rows` and SQL `LIMIT`; do not fetch
  unbounded results.
- **One event loop:** extremely high call rates are bounded by single-thread
  CPU for SQL parsing. For very high throughput, run multiple MCPg instances
  behind the streamable-HTTP transport.

## Planned (post-1.0)

Server-side cursors for large reads, read-replica routing for read scaling,
and a soak-test profile — see `PLAN.md` Phase 6.
