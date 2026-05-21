# MCPg Tool Reference

The tools MCPg exposes over MCP, grouped by category. Availability depends on
the configured access mode (see [`usage.md`](usage.md)).

## Server

### `get_server_info`
Returns the MCPg version, access mode, transport, and database connection
status. *Available in every mode.*

## Introspection (read)

### `list_schemas`
Lists database schemas. Parameter: `include_system` (bool, default `false`) —
include PostgreSQL's own schemas.

### `list_tables`
Lists the tables and views in a schema. Parameter: `schema` (string).

### `describe_table`
Describes a table's columns in ordinal order — name, data type, nullability,
default. Parameters: `schema`, `table` (strings).

### `list_indexes`
Lists the indexes on a table, each with its access method (`btree`, `gin`,
`gist`, `brin`, `hash`, `spgist`). Parameters: `schema`, `table` (strings).

### `list_extensions`
Lists the extensions installed in the database.

### `list_available_extensions`
Lists every extension available to the database — name, default version,
installed version, and whether it is `installed`.

## Query (read)

### `run_select`
Validates and runs a read-only SQL query. The statement is parsed and checked
against a safety allowlist; writes, DDL, and statement stacking are rejected.
Parameters: `sql` (string), `max_rows` (int, default 1000). Returns columns,
rows, `row_count`, and `truncated`.

### `explain_query`
Returns a query's `EXPLAIN (FORMAT JSON)` execution plan without running it.
Parameter: `sql` (string).

### `analyze_query_plan`
Summarises a query's execution plan — total cost, estimated rows, node types,
and sequentially-scanned tables. Parameter: `sql` (string).

## Health & tuning (read)

### `check_database_health`
Runs health checks: connection utilisation, buffer cache hit ratio, tables
needing vacuum, and invalid indexes. Returns an overall `status` plus
per-check results.

### `analyze_workload`
Returns the slowest queries by mean execution time, via the
`pg_stat_statements` extension. Parameter: `limit` (int, default 10). Reports
`available: false` if the extension is not installed.

### `recommend_indexes`
Flags large tables read mostly by sequential scan, and for each suggests
per-column index types from the column's data type — GIN for `jsonb` and
array columns, trigram GIN for text columns. Parameter: `min_live_tuples`
(int, default 10000).

### `fuzzy_search`
Ranks a text column's values by trigram similarity to a search term, via the
`pg_trgm` extension. Parameters: `schema`, `table`, `column`, `term`
(strings), `limit` (int, default 10), `threshold` (float, default 0.3).
Reports `available: false` if `pg_trgm` is not installed.

### `full_text_search`
Ranks a text column's documents against a full-text query using PostgreSQL's
built-in `tsvector`/`tsquery` (no extension required). The query accepts
web-search syntax (quoted phrases, `or`, `-` exclusion). Parameters:
`schema`, `table`, `column`, `search_query` (strings), `config` (string,
default `english`), `limit` (int, default 10).

## Write (unrestricted mode only)

### `run_write`
Executes a single `INSERT`, `UPDATE`, or `DELETE` in a read-write transaction
committed on success. Multiple statements and non-DML are rejected. Add a
`RETURNING` clause to receive affected rows. Parameter: `sql` (string).

### `run_ddl`
Executes a single DDL statement (`CREATE`/`ALTER`/`DROP` and related).
Requires `unrestricted` mode **and** `MCPG_ALLOW_DDL=true`. Parameter: `sql`
(string).

### `enable_extension`
Enables a known PostgreSQL extension (`CREATE EXTENSION IF NOT EXISTS`). Only
allowlisted extensions (`pg_trgm`, `vector`, `citext`, `postgis`, ...) may be
enabled. Requires `unrestricted` mode **and** `MCPG_ALLOW_DDL=true`.
Parameter: `name` (string).

## Errors

Tools reject unsafe or invalid input before it reaches the database. Rejected
calls return an MCP error result; the message explains the cause (unsafe
statement, parse failure, non-positive `max_rows`, etc.). Every call —
success or failure — is recorded to the `mcpg.audit` logger.
