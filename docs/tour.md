# MCPg tool tour

A compact one-page tour of every tool MCPg exposes, organised by
**what an agent typically wants to do**. Use this as a discovery
surface; the full reference is in [`tools.md`](tools.md), and
task-oriented recipes live in [`cookbook.md`](cookbook.md).

**141 tools** as of trunk. Each line shows the tool name + how its
parameters land (required first, common defaults after). Capability
gates are noted in section titles where they apply.

---

## "What's in this database?"

Catalog introspection — all read-only, available in every access mode.

```
list_schemas(include_system=false)
list_tables(schema)
describe_table(schema, table)
list_indexes(schema, table)
list_constraints(schema, table)
list_views(schema)
list_functions(schema)
list_triggers(schema, table)
list_partitions(schema, table)
list_foreign_keys(schema)
list_roles(include_system=false)
list_grants(schema, table)
list_policies(schema, table)         # row-level security
list_sequences(schema)
list_enums(schema)
list_domains(schema)
list_composite_types(schema)
list_foreign_data_wrappers()         # FDW infrastructure
list_foreign_servers()
list_foreign_tables(schema)
list_user_mappings()
list_publications()                  # logical replication
list_subscriptions()
list_extensions()
list_available_extensions()
list_generated_columns(schema)       # GENERATED ALWAYS AS … STORED columns
```

## "Show me the shape"

Visualisation + structural diff.

```
generate_schema_diagram(schema)                      # Mermaid ER text
generate_schema_docs(schema, include_samples=false)  # rich Markdown catalog documentation reference
generate_fk_cascade_graph(schema, include_all=false) # Mermaid graph of CASCADE / SET NULL / SET DEFAULT FKs
compare_schemas(left_schema, right_schema)           # added / removed / changed
```

## "Lint the schema"

```
run_advisors(schema)                                 # PK / unindexed-FK / dup-index / nullable-tstz / graph indices
lint_naming_conventions(schema)                      # snake_case vs camelCase outliers + index prefix rule
find_sensitive_columns(schema)                       # PII / secret heuristic
find_unused_objects(schema)                          # zero-scan tables and user indexes
```

## "Test row-level security as a specific role"

```
test_rls_for_role(schema, table, role, sample_size=25)  # runs as that role inside READ ONLY + SET LOCAL ROLE
```

## "Run a query / explain a plan"

```
run_select(sql, max_rows=1000)
run_select_parallel(statements, parallel_limit=8)    # concurrent fan-out; one bad query doesn't abort the rest
explain_query(sql, format="json")
analyze_query_plan(sql)                              # walks the EXPLAIN tree
translate_nl_to_sql(question, schema, provider=null, execute=false)
                                                     # NL → SQL; provider routes per call across configured Anthropic / OpenAI / Gemini
```

## "Stream a huge result set" (server-side cursors)

Each cursor owns a dedicated connection so long-lived cursors can't
starve the main pool. 5-minute idle TTL.

```
open_cursor(sql)                                     # validates via the run_select allowlist
fetch_cursor(cursor_id, batch_size=100)              # exhausted=true → stop polling
close_cursor(cursor_id)                              # idempotent
list_cursors()                                       # show every open cursor
```

## "Is this database healthy?"

```
check_database_health()                              # connections + cache + dead tuples + invalid indexes + replication lag + bloat
audit_database(schema, log_table=null)               # comprehensive 5-category DBA report with scores + top issues + recommendations
analyze_workload(top_n=10)                           # slow queries from pg_stat_statements
recommend_indexes()                                  # missing-index heuristics
recommend_index_drops(schema=null, min_index_size_bytes=1000000, low_scan_ratio=0.01)
                                                     # which existing indexes to consider dropping
                                                     # (never_used / scan_no_fetch / rarely_used)
list_active_queries()                                # who's running what right now
verify_connection_encryption()                       # is MCPg's own link TLS-encrypted? protocol + cipher + cluster tally
list_locks(limit=100)                                # pg_locks joined with pg_stat_activity (waiters first)
find_blocking_chains(limit=50)                       # (blocked, blocking) pairs via pg_blocking_pids
walk_blocking_chains(limit=50)                       # walks the lock graph, detects cycles, and renders a Mermaid flowchart
read_pg_stat_io()                                    # PG16+ I/O stats; available=false on PG 14/15
read_pg_buffercache_summary()                        # high-level shared buffer cache usage summary
read_pg_buffercache_relations(schema=null, limit=100) # relations taking up the most space in shared buffer cache
read_pg_wal_records(start_lsn, end_lsn='FFFFFFFF/FFFFFFFF', limit=100) # WAL record details over LSN range
read_pg_wal_stats(start_lsn, end_lsn='FFFFFFFF/FFFFFFFF', per_record=false) # aggregated WAL stats over LSN range
detect_n_plus_one(min_calls=100)                     # pg_stat_statements walker for ORM lazy-load loops
list_replicas()                                      # health of every read-replica (when MCPG_REPLICA_URLS set)
monitor_index_build()                                # active CREATE INDEX progress (phase + blocks/tuples %); PG12+
```

## "Tell me everything about this table / why is this query slow"

Composite tools — one call replaces 5–10.

```
summarize_table(schema, table, sample_rows=5)        # columns + PK + FKs + indexes + stats + sample
why_is_this_slow(sql)                                # EXPLAIN + plan analysis + locks + cache + index suggestions
```

## "Search for something"

```
fuzzy_search(schema, table, column, query)                                # pg_trgm trigram
full_text_search(schema, table, column, query)                            # tsvector / tsquery, web-search syntax
vector_search(schema, table, column, query_vector, k=10, operator="<->")  # pgvector k-NN
vector_range_search(schema, table, column, query_vector, max_distance)    # pgvector threshold
mmr_search(schema, table, column, query_vector, k=10, fetch_k=null, lambda_mult=0.5)
                                                                          # diversity re-rank (Maximal Marginal Relevance)
hybrid_search(schema, table, vector_col, text_col, query_vector, text_query)
                                                                          # vector + FTS fused via RRF
geo_search(schema, table, column, lon, lat, k=10)                         # PostGIS k-NN
```

## "Tune pgvector"

```
recommend_vector_index(schema, table, column)                             # HNSW vs IVFFlat heuristics
recommend_vector_quantization(schema)                                     # vector → halfvec / bit storage advisor
migrate_vector_to_halfvec(schema, table, column)                          # DDL plan: vector(N) → halfvec(N) (drop/alter/recreate
                                                                          # indexes; emits rollback_sql too — does not execute)
analyze_vector_search(schema, table, column, query_vector)
analyze_vector_table(schema, table)
analyze_distance_metric(schema, table, column, sample_size=1000)          # cosine / l2 / inner_product picker
                                                                          # from the magnitude distribution
cross_table_similarity(source_schema, source_table, source_embedding_column,
                       source_id_column, source_id_value,
                       target_schema, target_table, target_embedding_column, k=10)
                                                                          # given a row in A, k-NN against B
cluster_vectors(schema, table, embedding_column, k, id_column=null,
                sample_size=5000, max_iterations=20, metric="l2", seed=42)
                                                                          # k-means clustering of an embedding column
detect_vector_outliers(schema, table, embedding_column, id_column=null,
                       k=8, zscore_threshold=3.0, sample_size=5000, metric="l2",
                       seed=42, max_results=100)
                                                                          # flag rows weird-for-their-cluster (per-cluster z-score)
monitor_embedding_drift(schema, table, embedding_column, timestamp_column,
                        baseline_start, baseline_end, current_start, current_end,
                        sample_size=5000, drift_threshold=0.05)
                                                                          # centroid cosine drift + norm-distribution change
                                                                          # between two time windows
```

## "Inspect a pg_turboquant ANN index" (`pg_turboquant` installed)

```
list_turboquant_indexes()                          # every turboquant index + tq_index_metadata
get_turboquant_index_metadata(schema, index)       # algorithm_version, quantizer_family, capability_flags, …
get_turboquant_heap_stats(schema, index)           # exact heap row count from tq_index_heap_stats
get_turboquant_last_scan_stats()                   # most recent backend-local scan diagnostics
recommend_turboquant_maintenance()                 # advisor — prerequisites_unmet, format_v1_reindex_needed,
                                                   # maintenance_due, fast_path_ineligible (also feeds audit_database)
```

## "Move data in / out"

**Read** (no opt-in needed):
```
export_query(sql, format="csv", limit=10000)
export_table(schema, table, format="csv", limit=10000)
```

**Write** (`unrestricted` mode):
```
import_csv(schema, table, content, header=true, delimiter=",", columns=null)
import_json(schema, table, content, columns=null)
import_vectors(schema, table, embedding_column, content, format="json", id_column=null)
                                                     # pgvector vector(N) loader — validates every row's dim against the column
```

**Subprocess** (`unrestricted` + `MCPG_ALLOW_SHELL=true`):
```
dump_database(format="plain", schema_only=false)
restore_database(content, format="plain")
copy_table_between_databases(source_url, schema, table, include_schema, include_data)
```

## "React to database events" (`unrestricted` + `MCPG_ALLOW_LISTEN=true`)

```
subscribe_channel(channel)                           # returns subscription_id
poll_notifications(subscription_id, timeout_ms=0, max_messages=100)
unsubscribe_channel(subscription_id)
list_notification_subscriptions()
```

## "Stage a migration with review" (`unrestricted` + `MCPG_ALLOW_DDL=true`)

```
prepare_migration(name, target_schema, candidate_sql, ttl_minutes=60)
                                                     # returns id + shadow + diff
validate_migration(target_schema, candidate_sql, sample_rows_per_table=100)
                                                     # applies to a transient shadow with sampled real data
validate_migration_schema(target_schema, reference_schema, candidate_sql)
                                                     # applies to transient shadow and diffs against reference
complete_migration(migration_id)                     # applies to target
cancel_migration(migration_id)                       # drops shadow
list_pending_migrations()
read_migration_history(schema=None)                  # read applied migrations (Alembic/Flyway/etc.); read-only
generate_test_data(schema, table, rows=10, seed=42)  # synthetic INSERT statements; does NOT execute
seed_table_with_sample_data(schema, table, rows=10, seed=42)
                                                     # generates and executes synthetic INSERTs; WRITE-gated
```

## "Generate code for my ORM"

All read-only; pick the one your project uses.

```
generate_prisma_schema(schema)                       # .prisma (TypeScript)
generate_drizzle_schema(schema)                      # drizzle-orm/pg-core (TypeScript)
generate_sqlalchemy_models(schema)                   # SQLAlchemy 2.0 (Python)
generate_sqlc_schema(schema)                         # plain schema.sql for sqlc (Go)
generate_diesel_schema(schema)                       # Diesel schema.rs (Rust)
generate_jooq_config(schema, target_package, target_directory)
                                                     # jooq-codegen config XML (Java)
generate_ent_schemas(schema)                         # one .go per table (Go)
generate_ecto_schemas(schema, app_module="MyApp")    # one .ex per table (Elixir)
```

All eight cover: base tables, columns, primary keys, single-column
intra-schema FKs, and enums. Cross-schema and composite FKs are
documented gaps.

## "Write to the database" (`unrestricted` mode)

```
run_write(sql)                                       # one INSERT/UPDATE/DELETE; add RETURNING
run_maintenance(operation, schema, table)            # VACUUM / ANALYZE / REINDEX
prune_audit_events(older_than_days)                  # retention: delete old mcpg_audit.events rows (refused if integrity on)
cancel_query(pid)
terminate_backend(pid)
```

Plus `MCPG_ALLOW_DDL=true`:
```
run_ddl(sql, schema=null, table=null)                # one DDL statement; optional schema-diff snapshot
enable_extension(name)                               # allowlisted extensions only
```

## "Schedule a job" (`unrestricted` + pg_cron installed)

```
pg_cron.schedule(name, schedule, command)
pg_cron.unschedule(name_or_id)
pg_cron.update(name_or_id, …)

schedule_logical_backup(name, schedule, destination, …)  # composes pg_cron + pg_dump via COPY TO PROGRAM
```

## "Manage partitions" (`unrestricted` + `MCPG_ALLOW_DDL=true` + pg_partman installed)

```
partman.create_parent(parent_table, control, partition_type, partition_interval)
partman.run_maintenance()
partman.drop_partition_time(parent_table, retention)
```

## "Manage TimescaleDB hypertables" (`unrestricted` + `MCPG_ALLOW_DDL=true` + timescaledb installed)

```
list_hypertables()                                          # read-only — every mode
list_chunks(schema, table)                                  # read-only — every mode
create_hypertable(schema, table, time_column, chunk_time_interval='7 days', if_not_exists=true)
add_compression_policy(schema, table, compress_after='7 days')
add_retention_policy(schema, table, drop_after='30 days')
```

## "Work with property graphs" (Apache AGE)

When the `age` extension is installed and loaded:

```
list_graphs()                                               # graphs in ag_catalog
describe_graph(graph_name)                                  # labels + properties + edges
run_cypher(graph_name, cypher_query)                        # arbitrary Cypher; writes (CREATE/SET/DELETE/REMOVE/MERGE) need unrestricted
generate_graph_diagram(graph_name, max_labels=50)           # Mermaid graph of label relationships
create_graph(graph_name)                                    # DDL — unrestricted + MCPG_ALLOW_DDL
drop_graph(graph_name, cascade=true)                        # DDL — unrestricted + MCPG_ALLOW_DDL
```

## "Hook up Prometheus / health probes"

```
get_metrics_exposition()                                    # Prometheus text-format snapshot
                                                            # HTTP transport also serves /metrics + /healthz + /readyz
list_replicas()                                             # per-replica health when MCPG_REPLICA_URLS set
```

## "Who did what?"

```
list_audit_events(limit=100, tool=null)              # MCPG_AUDIT_PERSIST=true required
verify_audit_chain()                                 # validate the HMAC integrity chain
get_server_info()                                    # version, mode, transport, DB state
```

The `mcpg --version` shell flag (also `-V`) prints the version when
you're filing a bug report.

---

## Reading more

- [`cookbook.md`](cookbook.md) — task-oriented recipes (start here for common workflows).
- [`tools.md`](tools.md) — full parameter / return shape per tool.
- [`user-guide.md`](user-guide.md) — narrative walkthrough.
- [`security.md`](security.md) — threat model + access-mode boundaries.
- [`security-hardening.md`](security-hardening.md) — shipped vs queued hardening roadmap.
- [`architecture.md`](architecture.md) — design + module map.
- [`adr/`](adr/) — accepted architecture decision records.
