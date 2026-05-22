# MCPg Tool Reference

The tools MCPg exposes over MCP, grouped by category. Availability depends on
the configured access mode (see the [User Guide](user-guide.md)).

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
default, and (for `pgvector` `vector(N)` columns) the vector dimension.
Parameters: `schema`, `table` (strings).

### `list_indexes`
Lists the indexes on a table, each with its access method — a built-in one
(`btree`, `gin`, `gist`, `brin`, `hash`, `spgist`) or an extension's (e.g.
`hnsw`/`ivfflat` from `pgvector`). Parameters: `schema`, `table` (strings).

### `list_constraints`
Lists a table's constraints — each with its `type` (`primary_key`,
`foreign_key`, `unique`, `check`, `exclusion`, or `other`) and definition.
Parameters: `schema`, `table` (strings).

### `list_views`
Lists the views and materialized views in a schema — each with a
`materialized` flag and its definition. Parameter: `schema` (string).

### `list_functions`
Lists the functions and procedures in a schema — each with its `kind`
(`function`, `procedure`, `aggregate`, `window`, `other`), arguments, return
type, and language. Parameter: `schema` (string).

### `list_triggers`
Lists the user-defined triggers on a table — each with the function it calls
and its definition. Internal (constraint-enforcement) triggers are excluded.
Parameters: `schema`, `table` (strings).

### `list_partitions`
Describes how a table is partitioned and lists its partitions. Returns
`partitioned` (bool), `strategy` (`range`, `list`, `hash`, or `null`), and
`partitions` — each with its `name` and `bounds` expression. Parameters:
`schema`, `table` (strings).

### `list_sequences`
Lists the sequences defined in a schema — each with its data type, start
value, range (`min_value`/`max_value`), increment, `cycle` flag, and
`last_value` (`null` if unused or not readable). Parameter: `schema` (string).

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
Ranks a text column's values by `pg_trgm` trigram similarity to a search
term. Parameters: `schema`, `table`, `column`, `term` (strings), `mode`
(`word` — default — matches fragments within longer text; `full` compares
whole strings), `limit` (int, default 10), `threshold` (float, default 0.3).
Reports `available: false` if `pg_trgm` is not installed.

### `full_text_search`
Ranks a text column's documents against a full-text query using PostgreSQL's
built-in `tsvector`/`tsquery` (no extension required). The query accepts
web-search syntax (quoted phrases, `or`, `-` exclusion). Parameters:
`schema`, `table`, `column`, `search_query` (strings), `config` (string,
default `english`), `limit` (int, default 10).

### `vector_search`
Finds the rows nearest to a query vector by `pgvector` distance. Parameters:
`schema`, `table`, `column` (strings), `query_vector` (array of numbers),
`metric` (`l2`, `cosine`, or `inner_product`; default `l2`), `limit` (int,
default 10). Each match is the row (excluding the embedding column) plus its
`distance`. Reports `available: false` if `pgvector` is not installed.

### `geo_search`
Finds the rows nearest to a lon/lat point by PostGIS distance. Parameters:
`schema`, `table`, `column` (strings), `longitude`, `latitude` (numbers),
`limit` (int, default 10). Each match is the row (excluding the geometry
column) plus its `distance`. Reports `available: false` if `postgis` is not
installed.

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
