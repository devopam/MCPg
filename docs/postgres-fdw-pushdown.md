# `postgres_fdw` pushdown coverage

Realises audit row **#20** of `docs/plans/pg19-readiness.md` ("postgres_fdw
pushdown coverage doc + tests") under roadmap row **3.4**.

## Status

`postgres_fdw` is on MCPg's [`ENABLEABLE_EXTENSIONS`](../src/mcpg/extensions.py)
allowlist — operators can run `enable_extension('postgres_fdw')` once the
extension is available on the cluster. **MCPg ships no dedicated tool that
creates / queries foreign servers**; the workflow today is "operator builds
the FDW topology by hand, then queries it through the regular `run_select` /
`run_write` paths." That stays the supported shape under this doc; the
audit was about characterisation, not net-new tooling.

## What PG 19 changes for pushdown

Three pushdown surfaces became broader in PG 19. None requires an MCPg
code change — they're planner-side gains that benefit any FDW query
issued through MCPg's existing query path. Documented here so operators
can validate during the GA-day-0 sweep.

### 1. Array operator pushdown

Predicates of the shape `WHERE col = ANY ($array)` and `WHERE col <@ $array`
now reach the remote planner when the foreign table's `extensions` /
`use_remote_estimate` settings allow it. Verify by checking the remote
plan column on the foreign-table's `EXPLAIN (VERBOSE)`:

```sql
EXPLAIN (VERBOSE) SELECT * FROM remote_orders WHERE region = ANY (ARRAY['us', 'eu']);
-- Look for: "Remote SQL: ... WHERE ((region = ANY (...)))" in the
-- Foreign Scan node — the predicate landed at the remote side.
```

If the remote SQL string omits the predicate, the planner couldn't push
it; check `OPTIONS (use_remote_estimate 'on')` on the foreign server.

### 2. Extended statistics pushdown

PG 19's planner now considers extended statistics objects when costing
remote scans. Set `OPTIONS (analyze_sampling 'auto')` on the foreign server
and `OPTIONS (use_remote_estimate 'on')` on each foreign table for the
remote planner to feed real cardinality back. Without these, the planner
falls back to the 0.005 default selectivity and over-estimates fan-out.

### 3. `MERGE` pushdown

`MERGE` targeting a foreign table compiles to a single remote `MERGE`
statement (PG ≤ 18 unspun a `MERGE` into row-by-row `INSERT` / `UPDATE`
remote calls). The MCPg surface for `MERGE` is the regular `run_write`
path; there's no special wiring needed — `MERGE INTO foreign_t USING …`
just works on PG 19+.

## Characterisation test

The companion test
[`tests/contract/test_pg19_sql_characterisation.py`](../tests/contract/test_pg19_sql_characterisation.py)
asserts the catalogue SELECTs used across the PG 19 modules parse on
the pinned `pglast`, and pins the expected PG 19-only DDL grammar.
`postgres_fdw` itself doesn't appear in that catalogue because MCPg
doesn't emit any FDW-specific SQL — every FDW query rides the generic
`run_select` / `run_write` path which gets covered by the existing
`safe_sql` characterisation.

## Operator recipe — minimal pushdown smoke test

```sql
CREATE EXTENSION IF NOT EXISTS postgres_fdw;
CREATE SERVER remote_pg
  FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host 'remote.example.com', dbname 'app', port '5432');
CREATE USER MAPPING FOR CURRENT_USER
  SERVER remote_pg
  OPTIONS (user 'reader', password 'secret');
ALTER SERVER remote_pg OPTIONS (use_remote_estimate 'on');
ALTER SERVER remote_pg OPTIONS (analyze_sampling 'auto');
CREATE FOREIGN TABLE remote_orders (
  id   bigint,
  region text,
  amount numeric
) SERVER remote_pg OPTIONS (schema_name 'public', table_name 'orders');

-- Pushdown smoke check — should print a Remote SQL line with the
-- WHERE clause embedded.
EXPLAIN (VERBOSE)
SELECT region, sum(amount)
  FROM remote_orders
 WHERE region = ANY (ARRAY['us', 'eu'])
 GROUP BY region;
```

If the `Remote SQL:` line lacks the `WHERE` and `GROUP BY` clauses, the
planner is round-tripping every row — re-check `use_remote_estimate`
and statistic-collection options.

## What MCPg specifically does not do

- **No automatic foreign-server provisioning.** The `CREATE SERVER` /
  `CREATE USER MAPPING` DDL has too many security-sensitive option
  surfaces (password, channel binding, certificate paths) to wrap in a
  generic tool. Operators run those by hand and we trust the
  configured DSN.
- **No cross-cluster query rewrite.** Queries against foreign tables
  go through MCPg's `safe_sql` allowlist exactly like local queries.
  A foreign-table `SELECT` looks identical to a local `SELECT` to the
  policy gate; the planner does the rest.
- **No pushdown observability tool.** Operators rely on
  `EXPLAIN (VERBOSE)`'s `Remote SQL:` line to verify pushdown. A
  dedicated MCPg tool would duplicate the planner's own output without
  adding signal.

## Return conditions

A dedicated FDW surface in MCPg would be justified by:

1. Concrete demand for cross-cluster query advisors (e.g. "predict whether
   this query will push down").
2. PG ≥ 20 changes that broaden pushdown enough to warrant a
   `recommend_remote_estimate` advisor.
3. A multi-database deployment shape (roadmap row **13.1**) that bundles
   foreign-server setup with the main connection.

Until then, the operator-managed shape covers the audience.
