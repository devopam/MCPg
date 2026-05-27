# MCPg agent cookbook

Practical recipes for common workflows. Each recipe is a sequence of
MCPg tool calls in the order an agent should issue them, with notes
on why and what to expect back. Pair with [`tour.md`](tour.md) for
discovery and [`tools.md`](tools.md) for the full reference.

These recipes assume an MCP client (Claude Desktop, Claude Code, an
in-house agent) already has MCPg connected and can call tools by name.
Tool args are shown as `name(arg=value, ...)` — what you'd put in a
tool-call JSON payload.

---

## 1. "I just got handed a new database. What's in it?"

Walk the catalog top-down, starting at schemas and drilling into the
biggest / hottest tables.

```text
list_schemas(include_system=false)              # what schemas exist
list_tables(schema="app")                       # tables + views per schema
list_extensions()                                # is pgvector / PostGIS / TimescaleDB in play?
list_roles(include_system=false)                # who can do what
```

Once you know which schema matters:

```text
list_foreign_keys(schema="app")                  # relationships at a glance
generate_schema_diagram(schema="app")            # Mermaid ER diagram to paste
generate_fk_cascade_graph(schema="app")          # blast-radius graph of ON DELETE CASCADE chains
```

For a specific table:

```text
summarize_table(schema="app", table="orders")    # one call: columns + PK/FK + indexes + storage + sample
```

`summarize_table` replaces four-to-five round trips (`describe_table` +
`list_indexes` + `list_constraints` + stats). Use it instead of
piecing the view together by hand.

---

## 2. "Why is THIS query slow?"

```text
why_is_this_slow(sql="SELECT ... FROM orders WHERE ...")
```

That's the headline call. It does NOT execute the query — it walks
`EXPLAIN (FORMAT JSON)`, snapshots concurrent backends + blocking
pairs, reads the cluster cache-hit ratio, and returns a categorised
diagnosis.

When you want to look deeper:

```text
analyze_query_plan(sql="...")                    # raw plan tree + node-type summary
list_active_queries()                            # what else is running right now
find_blocking_chains()                           # who's waiting on whom (pg_blocking_pids)
list_locks(limit=50)                             # ordered with WAITERS first
read_pg_stat_io()                                # PG16+ I/O stats (degrades on PG 14/15)
```

For workload-level investigation (the slow patterns, not one query):

```text
analyze_workload(limit=10)                       # top by mean_exec_time (pg_stat_statements)
detect_n_plus_one(min_calls=100)                 # classic ORM lazy-load loop detector
```

---

## 3. "I need to recommend / test indexes."

```text
recommend_indexes(min_live_tuples=10000)         # tables with heavy seq scans
find_unused_objects(schema="app")                # zero-scan indexes + cold tables to drop
```

Cross-reference. Drop nothing without re-reading `find_unused_objects`
after the database has been hot for a meaningful period — fresh stats
produce false positives.

`recommend_vector_quantization()` flags `vector(N)` columns that could
halve their storage with `halfvec(N)` on pgvector ≥ 0.7.

---

## 4. "Safely test a migration before applying."

The shadow-schema workflow. Two tools and a diff:

```text
prepare_migration(name="add_orders_index",
                  target_schema="app",
                  candidate_sql="CREATE INDEX idx_orders_user ON orders(user_id);",
                  ttl_minutes=60)
# → { id, shadow_schema, ttl_expires_at, diff: { tables_added, ... } }
```

Read the `diff` — that's what `complete_migration` will land on the
target. To check the candidate against real-shape data BEFORE applying:

```text
validate_migration(target_schema="app",
                   candidate_sql="ALTER TABLE orders ALTER COLUMN total SET NOT NULL;",
                   sample_rows_per_table=500)
# → { table_stats, candidate_applied, error }
```

Catches the failure modes a pure structural diff misses: NOT NULL
added to a column with existing NULLs, CHECK constraints violated by
live rows, type narrowings that fail on real values, triggers that
error against actual data. `validate_migration` runs on a TRANSIENT
shadow that's dropped before returning — no persistent state.

When the diff + validation both look good:

```text
complete_migration(migration_id="<from prepare>")  # apply on target
# or
cancel_migration(migration_id="<from prepare>")    # drop the shadow
```

`list_pending_migrations()` shows everything still in the
`prepared` window.

Requires unrestricted mode + `MCPG_ALLOW_DDL=true`.

---

## 5. "Stream a very large result set."

Server-side cursors. Open, fetch in batches, close.

```text
open_cursor(sql="SELECT * FROM events WHERE created_at > '2026-01-01'")
# → { cursor_id: "mcpg_e3a91f", ... }

fetch_cursor(cursor_id="mcpg_e3a91f", batch_size=500)
# → { rows: [...], exhausted: false }

fetch_cursor(cursor_id="mcpg_e3a91f", batch_size=500)
# → { rows: [...], exhausted: true }   ← stop polling here

close_cursor(cursor_id="mcpg_e3a91f")
```

Each open cursor holds a dedicated connection (NOT a pool checkout)
so it can't starve other tools. Hard cap of 16 concurrent cursors per
server; default 5-min idle TTL. `list_cursors()` shows every open
cursor with `rows_returned` so far.

---

## 6. "Run several independent queries in parallel."

Dashboard fan-out: many small SELECTs for counters / aggregates.

```text
run_select_parallel(statements=[
    "SELECT count(*) FROM orders WHERE status = 'pending'",
    "SELECT count(*) FROM orders WHERE status = 'shipped'",
    "SELECT count(*) FROM users WHERE last_login > now() - interval '7 days'",
    "SELECT sum(amount) FROM payments WHERE created_at > now() - interval '24 hours'",
])
# → { outcomes: [{ index, success, result, error }, ...] }
```

Each statement is validated by the same safety allowlist as
`run_select`; one bad query does not abort the others. Default
concurrency limit is 8.

---

## 7. "Find sensitive data before sharing the schema."

```text
find_sensitive_columns(schema="app")
# → { columns: [{ table, column, categories: ["credential" | "pii" | ...], confidence, reasons }] }
```

Categories: `credential` (passwords / tokens / API keys),
`financial` (card numbers / IBAN), `contact` (email / phone),
`identifier` (DOB / name), `health` (HIPAA-scope), `government_id`
(SSN / passport), `location` (postal / IP).

Confidence: `high` (very specific name, e.g. `password_hash`),
`medium` (common pattern, e.g. `email_address`), `low` (broad
pattern). Filter by `high` first for an initial review pass.

This is a SIGNAL, not a verdict — a column named `email_template_id`
matches the email pattern but isn't itself an email address.

---

## 8. "What can a specific role actually read?"

Row-level security debug.

```text
test_rls_for_role(schema="app", table="orders", role="readonly_app",
                  sample_size=10)
# → { rls_enabled, active_policies: [{ name, command, using_expression, ... }],
#     rows_visible, sample: [...] }
```

Runs as the target role inside a `READ ONLY` transaction with
`SET LOCAL ROLE` — no writes can leak. The `active_policies` list
is filtered to policies that actually apply to the given role (so
PUBLIC-targeted policies + role-specific ones, not unrelated ones).

---

## 9. "Hook up Prometheus."

Two paths.

**Network scrape** (preferred): MCPg's HTTP transport exposes
`/metrics` on the same port as MCP. Configure your scraper:

```yaml
- job_name: mcpg
  static_configs: [{ targets: ['mcpg-host:8000'] }]
```

The bearer-token middleware (if `MCPG_HTTP_AUTH_TOKEN` is set) exempts
`/metrics`, so the scraper doesn't need the MCP credential.

**Over the MCP protocol** (when running stdio):

```text
get_metrics_exposition()
# → "# HELP mcpg_tool_calls_total ...\n..."
```

Returns the exact same text-exposition format. Three series:
`mcpg_tool_calls_total{tool,status}`, `mcpg_tool_duration_seconds_bucket`,
and the matching `_sum` / `_count`.

---

## 10. "Multi-tenant — serve N roles from one MCPg."

Static config, applies to every query:

```bash
MCPG_DEFAULT_ROLE=app_tenant_42
```

Per-request, HTTP transport only:

```http
X-MCPG-Role: app_tenant_42
Authorization: Bearer <MCPG_HTTP_AUTH_TOKEN>
```

The gateway maps an authenticated user to a PG role and forwards the
request with that header. MCPg validates the role identifier against
`[A-Za-z_][A-Za-z0-9_]*`, checks it against the optional
`MCPG_ALLOWED_ROLES` allowlist, and issues `SET LOCAL ROLE "<role>"`
inside every query's transaction. `SET LOCAL` auto-resets at txn end
— no state leaks back into the pool.

**With OIDC**: when `MCPG_AUTH_MODE=oidc` and `MCPG_OIDC_ROLE_CLAIM`
is set, the role is read from the JWT claim instead of the
`X-MCPG-Role` header. The OIDC issuer becomes the single source of
truth — clients send only `Authorization: Bearer <JWT>`.

---

## 11. "Spread reads across replicas."

Configure replica DSNs at startup:

```bash
MCPG_REPLICA_URLS=postgresql://reader:pw@replica-1/app,postgresql://reader:pw@replica-2/app
```

MCPg now keeps a dedicated pool per replica alongside the primary
pool. Every `force_readonly=True` query (catalog reads, `run_select`,
the safety-driver path) is round-robin routed to a healthy replica;
writes always go to the primary. Composes with multi-tenancy —
`SET LOCAL ROLE` applies per-replica.

Diagnose:

```text
list_replicas()
# → [{ index, dsn, degraded, last_error, seconds_until_retry }]
```

A replica that fails a query is marked degraded for 30s, skipped
from the round-robin, then re-probed. When every replica is degraded,
reads fall back to the primary — the routing layer never blocks the
tool layer because the replicas are unavailable.

Routing decisions land in the Prometheus metrics under
`mcpg_tool_calls_total{tool="__replica_route", status=...}` with
status values `primary` / `primary_no_healthy` / `fallback` /
`replica_<n>`.

---

## 12. "OIDC bearer-token validation."

```bash
MCPG_AUTH_MODE=oidc
MCPG_OIDC_ISSUER=https://accounts.example.com
MCPG_OIDC_AUDIENCE=mcpg
MCPG_OIDC_ROLE_CLAIM=pg_role         # optional; maps claim → PG role
```

Replaces the static-token compare path with full JWT validation. On
first request MCPg fetches `<issuer>/.well-known/openid-configuration`,
caches the `jwks_uri`, then validates every subsequent JWT against
the JWKS: signature (RS256/RS384/RS512 + ES256/ES384/ES512 only —
HS-family is excluded), expiry, issuer, audience, with 30s clock
leeway. JWKS keys cache for 1 hour.

When `MCPG_OIDC_ROLE_CLAIM` is configured AND the JWT carries that
claim, the value is validated as a safe PG identifier and stashed
into the same ContextVar `SET ROLE` uses — so the tenanted driver
issues `SET LOCAL ROLE "<role-from-claim>"` for the request. The
`X-MCPG-Role` header path is skipped in OIDC mode: the issuer is the
single source of truth.

Override the JWKS URL when discovery isn't reachable (e.g. issuer
behind a private network):

```bash
MCPG_OIDC_JWKS_URL=https://public.example/keys
```

---

## 13. "Natural language to SQL."

```text
translate_nl_to_sql(question="how many orders were shipped last week?",
                    schema="app",
                    execute=true,
                    table_filter=["orders"])
# → { sql, explanation, executed, rows, columns, row_count }
```

Requires `MCPG_NL2SQL_PROVIDER` + API key set at startup
(anthropic / openai / gemini). The tool sends a compact schema brief
(tables, columns, FKs) to the configured LLM, parses the JSON
response, and — when `execute=true` — passes the generated SQL
through `SafeSqlDriver`'s allowlist before running it. A model that
hallucinates a `DELETE` is rejected at the safety layer, not run.

`table_filter` narrows the schema brief to a known subset when the
question is clearly scoped — useful on large schemas.

For a quick review pattern: call with `execute=false`, read the
`sql`, then call `run_select` if it looks right.

---

## 14. "Bring data in / out."

**Export a query as CSV / JSON**:

```text
export_query(sql="SELECT id, name FROM widget WHERE active = true",
             format="csv")
# → { bytes_written, payload (base64), format }
```

**Export a whole table**:

```text
export_table(schema="app", table="orders", format="json")
```

**Import CSV** (unrestricted mode):

```text
import_csv(schema="app", table="staging_events", payload_base64="...")
# → { rows_inserted, ... }
```

**Cross-database copy** — pipeline between two libpq URIs:

```text
copy_table_between_databases(
    source_database_url="postgresql://src/...",
    target_database_url="postgresql://dst/...",
    schema="app", table="orders", format="csv")
```

For full database snapshots, prefer the subprocess tools
(`dump_database` / `restore_database`); they shell out to `pg_dump`
and `pg_restore` so binary objects and roles survive the trip. Both
gated under `MCPG_ALLOW_SHELL=true`.

---

## 15. "Emit ORM models from the live schema."

Eight catalog → DSL exporters:

```text
generate_prisma_schema(schema="app")
generate_drizzle_schema(schema="app")
generate_sqlalchemy_models(schema="app")
generate_sqlc_schema(schema="app")
generate_diesel_schema(schema="app")
generate_jooq_config(schema="app")
generate_ent_schemas(schema="app")
generate_ecto_schemas(schema="app")
```

Each returns a string of ORM-specific DSL ready to paste into a
project. They read the catalog only — your database stays untouched.

---

## 16. "Listen / notify bridge."

Postgres `LISTEN`/`NOTIFY` adapted to the MCP poll model. Requires
`MCPG_ALLOW_LISTEN=true`.

```text
subscribe_channel(channel="orders_placed")
poll_notifications(channel="orders_placed")          # pulls queued messages
poll_notifications(channel="orders_placed", timeout_sec=5)   # blocks up to 5s
unsubscribe_channel(channel="orders_placed")
list_notification_subscriptions()                     # current state
```

The listener owns a single dedicated PG connection that auto-reconnects
on drop; subscriptions survive transient outages.

---

## 17. "Inspect TimescaleDB hypertables."

When the `timescaledb` extension is installed:

```text
list_hypertables()
list_chunks(schema="app", table="metrics")
create_hypertable(schema="app", table="metrics", time_column="ts")
add_compression_policy(schema="app", table="metrics", compress_after="14 days")
add_retention_policy(schema="app", table="metrics", drop_after="90 days")
```

All write tools gated under unrestricted mode + `MCPG_ALLOW_DDL`.
Identifiers and interval expressions are allowlist-validated before
inlining into SQL.

---

## 18. "Generate synthetic data for staging."

```text
generate_test_data(schema="app", table="widget", rows=100, seed=42)
# → { statements: ["INSERT INTO ... VALUES (...);", ...], skipped_columns: [...] }
```

Returns INSERT statements — does NOT execute. The agent reviews and
runs via `run_write` if desired. Foreign keys are NOT resolved
(documented limitation): pre-seed referenced rows or drop the FK
temporarily. Deterministic with a seed; supports numeric / text /
boolean / date / timestamp / json / uuid types. Unsupported types
(geometry, hstore, vector, ...) are listed in `skipped_columns`.

---

## 19. "Lint the schema."

```text
run_advisors(schema="app")                       # PK / FK / duplicate-index / nullable-tstz
lint_naming_conventions(schema="app")            # case-style + index prefix
find_sensitive_columns(schema="app")             # PII / secrets
find_unused_objects(schema="app")                # cold tables + dead indexes
```

Combine these for a one-shot schema-health report an agent can
present to a reviewer.

---

## 20. "Work with a property graph" (Apache AGE)

When the `age` extension is loaded on the target database, MCPg
exposes the AGE / Cypher surface alongside the relational tools.

```text
list_graphs()
# → [{ name: "social", node_count, edge_count }, ...]

describe_graph(graph_name="social")
# → { labels: ["Person", "Company"], edges: [...], property_stats: {...} }

run_cypher(graph_name="social",
           cypher="MATCH (p:Person)-[:WORKS_AT]->(c:Company) RETURN p.name, c.name LIMIT 25")
# → { rows: [{ "p.name": "Alice", "c.name": "Acme" }, ...], parsed_agtype: true }
```

Visualise the schema of the graph (Mermaid):

```text
generate_graph_diagram(graph_name="social", max_labels=50)
```

DDL (gated under unrestricted + `MCPG_ALLOW_DDL`):

```text
create_graph(graph_name="my_new_graph")
drop_graph(graph_name="my_new_graph", cascade=true)
```

`run_cypher` validates Cypher input parameters against the same
identifier-safety rules MCPg uses elsewhere; `agtype` results are
parsed back into native Python values (objects, lists, numbers,
strings, booleans, nulls) before reaching the agent.

---

## Tool-call ordering tips

* **Read before write.** Every write-class tool (`run_write`,
  `run_ddl`, the migration family, `import_*`, `add_*_policy`) has a
  read sibling that lets the agent confirm shape first.
* **Use the composites first.** `summarize_table`, `why_is_this_slow`,
  `find_unused_objects` collapse what would otherwise be 4-5 round
  trips. Reach for those before piecing the answer together by hand.
* **Filter, then drill.** Schema-level tools take a `schema=` arg;
  large catalogs respond faster when you scope. The NL→SQL helper
  takes `table_filter=` for the same reason.
* **Capability gates fail closed.** A tool isn't `available=false` —
  it isn't listed at all. If `run_write` doesn't appear, the active
  access mode forbids it. Restart MCPg with a wider mode (and the
  matching `MCPG_ALLOW_*` opt-in) when you need it.
