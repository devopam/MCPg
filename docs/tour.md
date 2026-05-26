# MCPg tool tour

A compact one-page tour of every tool MCPg exposes, organised by
**what an agent typically wants to do**. Use this as a discovery surface;
the full reference is in [`tools.md`](tools.md).

**78 tools.** Numbers in `()` after each tool show roughly how parameters
land — required ones first, common defaults afterwards.

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
```

## "Show me the shape"

Visualisation + structural diff.

```
generate_schema_diagram(schema)                      # Mermaid ER text
compare_schemas(left_schema, right_schema)           # added/removed/changed
```

## "Run a query / explain a plan"

```
run_select(sql, max_rows=1000)
explain_query(sql, format="json")
analyze_query_plan(sql)                              # walks the EXPLAIN tree
```

## "Is this database healthy?"

```
check_database_health()                              # connections + cache + dead tuples + invalid indexes + replication lag + bloat
analyze_workload(top_n=10)                           # slow queries from pg_stat_statements
recommend_indexes()                                  # missing-index heuristics
run_advisors()                                       # aggregate of the above
list_active_queries()                                # who's running what right now
```

## "Search for something"

```
fuzzy_search(schema, table, column, query)           # pg_trgm trigram
full_text_search(schema, table, column, query)       # tsvector / tsquery
vector_search(schema, table, column, query_vector, k=10, operator="<->")  # pgvector k-NN
vector_range_search(schema, table, column, query_vector, max_distance)    # pgvector threshold
hybrid_search(schema, table, vector_col, text_col, query_vector, text_query)
                                                     # vector + FTS fused via RRF
geo_search(schema, table, column, lon, lat, k=10)    # PostGIS k-NN
```

## "Tune pgvector"

```
recommend_vector_index(schema, table, column)        # HNSW vs IVFFlat heuristics
recommend_vector_quantization(schema)                # vector -> halfvec storage advisor
analyze_vector_search(schema, table, column, query_vector)
analyze_vector_table(schema, table)
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
complete_migration(migration_id)                     # applies to target
cancel_migration(migration_id)                       # drops shadow
list_pending_migrations()
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

All eight share v1 coverage: base tables, columns, primary keys,
single-column intra-schema FKs, enums. Cross-schema and composite FKs
are documented gaps.

## "Write to the database" (`unrestricted` mode)

```
run_write(sql)                                       # one INSERT/UPDATE/DELETE; add RETURNING
run_maintenance(operation, schema, table)            # VACUUM / ANALYZE / REINDEX
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
pg_cron.update(name_or_id, ...)
```

## "Manage partitions" (`unrestricted` + `MCPG_ALLOW_DDL=true` + pg_partman installed)

```
partman.create_parent(parent_table, control, partition_type, partition_interval)
partman.run_maintenance()
partman.drop_partition_time(parent_table, retention)
```

## "Who did what?"

```
list_audit_events(limit=100, tool=null)              # MCPG_AUDIT_PERSIST=true required
get_server_info()                                    # version, mode, transport, DB state
```

## Reading more

- [`tools.md`](tools.md) — full parameter / return shape per tool.
- [`user-guide.md`](user-guide.md) — narrative walkthrough.
- [`security.md`](security.md) — threat model + access-mode boundaries.
- [`architecture.md`](architecture.md) — design + module map.
- [`adr/`](adr/) — accepted architecture decision records.
