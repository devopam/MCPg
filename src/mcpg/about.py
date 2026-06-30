"""MCPg self-description.

Closes the "what does mcpg do?" introspection gap surfaced by
Phase A of the tool-surface review (no MCP resources, no MCP
prompts, no ``about`` / ``capabilities`` tool — the only answer
surface was the 173-tool catalogue).

This module owns the hand-curated capability buckets used by the
``describe_self`` MCP tool. The buckets are intentionally coarse
(16 groups, each one a coherent operational use case) so an LLM
agent can hold the whole list in working memory and decide which
bucket to expand on with a follow-up call.

The mapping ``tool name -> bucket id`` is part-pattern, part-override
(see :data:`_TOOL_TO_BUCKET_PATTERNS` and :data:`_TOOL_TO_BUCKET_OVERRIDES`).
The contract test in ``tests/unit/test_about.py`` asserts every tool
registered on the maximal-flag server falls into exactly one bucket,
so adding a new MCPg tool always forces a deliberate decision about
which capability it belongs to.

Public surface:

* :func:`build_capability_summary` — assembles the JSON payload the
  ``describe_self`` tool returns. Takes the live tool list (so the
  counts always match reality) and returns the structured response.
* :data:`CAPABILITIES` — the 16 capability buckets, in display order.
* :data:`BUCKET_IDS` — the canonical bucket-id set (use this in tests
  / wherever you need an exhaustive enum).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mcpg import __version__


@dataclass(frozen=True, slots=True)
class Capability:
    """One capability bucket.

    Each capability has a short, agent-friendly ``summary`` (≤140 chars
    — fits comfortably in a single LLM token-budget line) and a
    longer ``detail`` for when the agent needs more depth. The
    ``headline_tools`` are the 3-6 tools an agent should reach for
    first within this bucket; the full list comes from
    :func:`build_capability_summary`.
    """

    id: str
    name: str
    summary: str
    detail: str
    headline_tools: tuple[str, ...]


# ---------------------------------------------------------------------------
# Capability buckets — hand-curated.
#
# Display order matters: it's the order an LLM will scan when answering
# "what can mcpg do?" — schema_introspection first (most common entry
# point), query_execution second, then domain-specific buckets, then
# operations/diagnostics last.
# ---------------------------------------------------------------------------

CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="schema_introspection",
        name="Schema introspection",
        summary=(
            "Inspect tables, columns, indexes, FKs, views, functions, triggers, "
            "sequences, enums, roles, and every other catalogue object."
        ),
        detail=(
            "MCPg exposes every aspect of the PostgreSQL information schema as "
            "list / describe tools. Start with `list_tables` and `describe_table` "
            "for the common path; reach for `compare_schemas` or "
            "`get_compact_schema` when you need cross-schema diffs or compact "
            "summaries for prompt context."
        ),
        headline_tools=(
            "list_tables",
            "describe_table",
            "list_foreign_keys",
            "list_indexes",
            "get_compact_schema",
            "compare_schemas",
        ),
    ),
    Capability(
        id="query_execution",
        name="Query execution",
        summary=(
            "Run SELECT / write / DDL against the database, with safety gates, "
            "EXPLAIN plans, parallel reads, and natural-language SQL translation."
        ),
        detail=(
            "Read-only by default via `run_select`; `run_write` and `run_ddl` "
            "are gated behind `MCPG_ACCESS_MODE` and `MCPG_ALLOW_DDL`. "
            "`translate_nl_to_sql` lets an agent describe what it wants in plain "
            "language and get back vetted SQL. `explain_query` and "
            "`analyze_query_plan` surface the planner's view."
        ),
        headline_tools=(
            "run_select",
            "run_write",
            "run_ddl",
            "translate_nl_to_sql",
            "explain_query",
            "analyze_query_plan",
        ),
    ),
    Capability(
        id="vector_search",
        name="Vector and ANN search",
        summary=(
            "Similarity search over pgvector and pg_turboquant — kNN, range, "
            "MMR diversity, clustering, outlier detection, recall analytics."
        ),
        detail=(
            "Both pgvector (HNSW / IVFFlat) and pg_turboquant (compressed ANN) "
            "are first-class. Use `vector_search` for ranked kNN, `mmr_search` "
            "for diversity-aware retrieval, `cluster_vectors` and "
            "`detect_vector_outliers` for offline analytics, and the "
            "`analyze_*_recall` / `recommend_vector_quantization` advisors when "
            "you're tuning. `hybrid_search` / `hybrid_bm25_vector_search` combine "
            "vector and BM25 via reciprocal-rank fusion."
        ),
        headline_tools=(
            "vector_search",
            "mmr_search",
            "hybrid_search",
            "cluster_vectors",
            "analyze_hnsw_recall",
            "tune_vector_index",
        ),
    ),
    Capability(
        id="text_search",
        name="Text and geospatial search",
        summary=("BM25 (pg_search), full-text tsvector, fuzzy / trigram, and PostGIS-style geo search."),
        detail=(
            "`pg_search_*` exposes the BM25 surface (create indexes, run queries, "
            "do more-like-this). `full_text_search` is the classic tsvector path; "
            "`fuzzy_search` covers trigram / Levenshtein; `geo_search` covers "
            "spatial proximity. For combined BM25 + vector retrieval see "
            "`hybrid_bm25_vector_search` in the vector_search bucket."
        ),
        headline_tools=(
            "full_text_search",
            "fuzzy_search",
            "geo_search",
            "pg_search_run",
            "pg_search_more_like_this",
        ),
    ),
    Capability(
        id="rag_telemetry",
        name="RAG telemetry and analytics",
        summary=(
            "Log rerank events, observe pipeline efficiency, analyse lift / "
            "stability / NDCG, get strategy recommendations."
        ),
        detail=(
            "If you run a RAG pipeline against MCPg-managed data, the "
            "`mcpg_rag.*` schema captures rerank events (`log_rerank_event`) "
            "and efficiency observations (`record_efficiency_observation`). "
            "The `analyze_reranker_*` and `analyze_topk_stability` advisors "
            "turn that data into ranking-lift / stability / NDCG insights, and "
            "`recommend_rerank_strategy` is the roll-up."
        ),
        headline_tools=(
            "log_rerank_event",
            "analyze_reranker_lift",
            "analyze_topk_stability",
            "analyze_rerank_ndcg",
            "recommend_rerank_strategy",
            "monitor_embedding_drift",
        ),
    ),
    Capability(
        id="audit_trail",
        name="Audit trail and integrity",
        summary=("Persistent, HMAC-chained audit log for every write/DDL — list, prune, and verify chain integrity."),
        detail=(
            "When `MCPG_AUDIT_PERSIST=true`, every write/DDL is captured into "
            "`mcpg_audit.events` with credential redaction. The optional "
            "`MCPG_AUDIT_INTEGRITY` HMAC chain detects tampering — "
            "`verify_audit_chain` walks the chain end-to-end."
        ),
        headline_tools=(
            "list_audit_events",
            "prune_audit_events",
            "verify_audit_chain",
        ),
    ),
    Capability(
        id="data_movement",
        name="Data movement",
        summary=(
            "Dump / restore / import / export between databases, files, and "
            "tables. CSV / JSON / vector formats supported."
        ),
        detail=(
            "`dump_database` / `restore_database` wrap pg_dump / pg_restore "
            "(shell mode required). `copy_table_between_databases` runs "
            "psycopg's binary COPY for fast in-cluster moves. `import_csv` "
            "and `import_json` load files; `export_*` writes them. "
            "`seed_table_with_sample_data` and `generate_test_data` are for "
            "smoke-testing fresh deployments."
        ),
        headline_tools=(
            "dump_database",
            "restore_database",
            "copy_table_between_databases",
            "import_csv",
            "export_query",
        ),
    ),
    Capability(
        id="migrations",
        name="Schema migrations",
        summary=(
            "List pending migrations, validate them safely, prepare / complete / cancel migrations, read history."
        ),
        detail=(
            "`list_pending_migrations` and `list_unapplied_migration_scripts` "
            "show what's queued. `validate_migration` and "
            "`validate_migration_schema` simulate without running. "
            "`prepare_migration` / `complete_migration` / `cancel_migration` "
            "drive a two-phase apply; `read_migration_history` shows what "
            "ran when."
        ),
        headline_tools=(
            "list_pending_migrations",
            "validate_migration",
            "prepare_migration",
            "complete_migration",
            "read_migration_history",
        ),
    ),
    Capability(
        id="timeseries_partitioning",
        name="Time-series and partitioning",
        summary=(
            "TimescaleDB hypertables, compression / retention policies, chunks, "
            "and pg_partman declarative partitioning."
        ),
        detail=(
            "If TimescaleDB is installed: `create_hypertable`, "
            "`add_compression_policy`, `add_retention_policy`, `list_hypertables`, "
            "`list_chunks`. For PostgreSQL native partitioning managed by "
            "pg_partman: `partman_create_parent`, `partman_run_maintenance`, "
            "`partman_drop_partition`, `list_partitions`. Both backends are "
            "first-class for the audit / RAG telemetry tables (see PR-4 / PR-5 "
            "in v0.6.2)."
        ),
        headline_tools=(
            "create_hypertable",
            "add_compression_policy",
            "add_retention_policy",
            "partman_create_parent",
            "partman_run_maintenance",
            "list_partitions",
        ),
    ),
    Capability(
        id="graph_operations",
        name="Graph operations",
        summary=("Create / drop / describe graphs, run Cypher queries, generate FK-cascade graphs."),
        detail=(
            "MCPg surfaces an OpenCypher-compatible graph layer over PostgreSQL "
            "(via Apache AGE-style schema). `create_graph` / `drop_graph` manage "
            "graphs; `run_cypher` executes queries. `generate_fk_cascade_graph` "
            "derives a graph view from existing FK relationships."
        ),
        headline_tools=(
            "create_graph",
            "run_cypher",
            "describe_graph",
            "list_graphs",
            "generate_fk_cascade_graph",
        ),
    ),
    Capability(
        id="cache_and_foreign_data",
        name="Cache and foreign data",
        summary=(
            "Foreign-data-wrapper coverage for cache-style data sources — "
            "currently Redis via redis_fdw, with FDW catalog introspection "
            "shared with the schema-introspection bucket."
        ),
        detail=(
            "`list_redis_foreign_servers`, `describe_redis_cache_table`, and "
            "`get_redis_cache_stats` filter the foreign-data catalog to the "
            "redis_fdw subset. `create_redis_cache_server`, "
            "`create_redis_user_mapping`, and `create_redis_cache_table` wrap "
            "the DDL with identifier validation, secrets-backend credential "
            "plumbing, and TLS-by-default. `recommend_redis_cache_targets` "
            "analyses pg_stat_user_tables for read-heavy tables that would "
            "benefit from a Redis cache layer. The generic FDW catalog tools "
            "(`list_foreign_data_wrappers`, `list_foreign_servers`, "
            "`list_foreign_tables`, `list_user_mappings`) live in the "
            "schema-introspection bucket."
        ),
        headline_tools=(
            "list_redis_foreign_servers",
            "describe_redis_cache_table",
            "recommend_redis_cache_targets",
            "create_redis_cache_server",
            "create_redis_cache_table",
        ),
    ),
    Capability(
        id="cache_warming",
        name="Cache warming and pg_prewarm",
        summary=(
            "Inspect shared-buffer residency, recommend prewarm targets "
            "against a shared_buffers budget, and schedule pg_cron-backed "
            "autowarm jobs."
        ),
        detail=(
            "`get_prewarm_extension_status` reports whether ``pg_prewarm`` "
            "and ``pg_buffercache`` are installed and whether pg_prewarm is "
            "registered in shared_preload_libraries. `list_prewarmed_relations` "
            "surfaces current shared-buffer residency. The headline advisor "
            "`recommend_prewarm_targets` ranks relations by miss-volume and "
            "caps the cumulative cost at a shared_buffers budget. Pair with "
            "`prewarm_recommended` to act on its output. "
            "`schedule_autowarm` / `unschedule_autowarm` / `list_autowarm_jobs` "
            "drive a pg_cron-backed loop for the 'warm after restart' pattern."
        ),
        headline_tools=(
            "recommend_prewarm_targets",
            "prewarm_recommended",
            "list_prewarmed_relations",
            "schedule_autowarm",
            "get_prewarm_extension_status",
        ),
    ),
    Capability(
        id="property_graph_queries",
        name="SQL/PGQ property graphs",
        summary=(
            "PG 19 SQL/PGQ — define property graphs over existing tables "
            "and query them with `GRAPH_TABLE(...)` / `MATCH` patterns."
        ),
        detail=(
            "`get_pgq_status` reports availability (requires PG 19+); on "
            "older servers it points the agent at the AGE-style "
            "`graph_operations` bucket as a fallback. "
            "`list_property_graphs` / `describe_property_graph` enumerate "
            "what's defined. `run_pgq` executes "
            "`SELECT ... FROM GRAPH_TABLE(...)` queries with a "
            "single-statement / GRAPH_TABLE-required guard. "
            "`create_property_graph` / `drop_property_graph` handle the "
            "DDL with identifier validation. SQL/PGQ coexists with the "
            "AGE-style `graph_operations` bucket — pick by `get_pgq_status` "
            "(SQL/PGQ on PG 19+) or by personal preference."
        ),
        headline_tools=(
            "get_pgq_status",
            "list_property_graphs",
            "run_pgq",
            "create_property_graph",
            "describe_property_graph",
        ),
    ),
    Capability(
        id="diagrams_and_codegen",
        name="Diagrams and ORM codegen",
        summary=(
            "Generate Mermaid / Graphviz diagrams, schema docs, and ORM stubs "
            "for 8 ecosystems (Diesel, Drizzle, Ecto, Ent, jOOQ, Prisma, "
            "SQLAlchemy, sqlc)."
        ),
        detail=(
            "`generate_schema_diagram` / `generate_graph_diagram` produce ERD-"
            "style visualisations. `generate_schema_docs` produces a Markdown "
            "reference. The `generate_*_schema` family emits ORM code for "
            "downstream ecosystems — pick the one matching your stack."
        ),
        headline_tools=(
            "generate_schema_diagram",
            "generate_schema_docs",
            "generate_prisma_schema",
            "generate_sqlalchemy_models",
            "generate_drizzle_schema",
        ),
    ),
    Capability(
        id="advisors",
        name="Advisors and recommendations",
        summary=("Indexing / vector / RAG / workload advisors — find what to add, drop, tune, or fix."),
        detail=(
            "`recommend_indexes` / `recommend_index_drops` for index hygiene. "
            "`recommend_vector_quantization`, "
            "`recommend_turboquant_query_knobs`, `recommend_rerank_strategy`, "
            "`recommend_efficiency_thresholds` for vector / RAG tuning. "
            "`find_unused_objects` / `find_sensitive_columns` / "
            "`detect_n_plus_one` / `analyze_workload` are diagnostic. "
            "`run_advisors` runs them all; `audit_database` is the deep DBA "
            "scan."
        ),
        headline_tools=(
            "run_advisors",
            "recommend_indexes",
            "recommend_index_drops",
            "find_unused_objects",
            "detect_n_plus_one",
            "audit_database",
        ),
    ),
    Capability(
        id="operations_and_health",
        name="Operations and health",
        summary=(
            "Connection / lock / blocking-chain visibility, query cancellation, "
            "maintenance, extension enablement, TLS verification."
        ),
        detail=(
            "`check_database_health` is the one-call summary. "
            "`list_active_queries` / `list_locks` / `walk_blocking_chains` / "
            "`find_blocking_chains` for the live picture; "
            "`cancel_query` / `terminate_backend` for intervention. "
            "`enable_extension` / `verify_connection_encryption` for setup. "
            "`run_maintenance` for VACUUM / ANALYZE / REINDEX orchestration."
        ),
        headline_tools=(
            "check_database_health",
            "list_active_queries",
            "list_locks",
            "walk_blocking_chains",
            "cancel_query",
            "run_maintenance",
        ),
    ),
    Capability(
        id="observability",
        name="Server introspection and observability",
        summary=(
            "Server config, Prometheus metrics, WAL / I/O / buffer-cache stats, self-describing capability summary."
        ),
        detail=(
            "`get_server_info` returns runtime config (version, access mode, "
            "transport). `get_metrics_exposition` returns Prometheus-format "
            "metrics over MCP. `read_pg_stat_io`, `read_pg_wal_records`, "
            "`read_pg_wal_stats`, `read_pg_buffercache_*` expose per-relation "
            "/ system-wide internals. `describe_self` — this tool — returns "
            "the capability summary you're reading."
        ),
        headline_tools=(
            "describe_self",
            "get_server_info",
            "get_metrics_exposition",
            "read_pg_stat_io",
            "read_pg_wal_records",
        ),
    ),
    Capability(
        id="scheduled_jobs",
        name="Scheduled jobs",
        summary=("pg_cron job management and scheduled logical backups."),
        detail=(
            "`list_cron_jobs` / `schedule_cron_job` / `unschedule_cron_job` "
            "wrap pg_cron. `schedule_logical_backup` chains a pg_dump call "
            "into a recurring pg_cron job for hands-off backup."
        ),
        headline_tools=(
            "list_cron_jobs",
            "schedule_cron_job",
            "unschedule_cron_job",
            "schedule_logical_backup",
        ),
    ),
    Capability(
        id="realtime_and_cursors",
        name="Real-time notifications and cursors",
        summary=(
            "LISTEN / NOTIFY subscriptions, polled notification streams, and "
            "explicit cursor lifecycles for chunked reads."
        ),
        detail=(
            "For event-driven flows: `subscribe_channel` / `unsubscribe_channel` "
            "/ `list_notification_subscriptions` / `poll_notifications` "
            "(requires `MCPG_ALLOW_LISTEN`). For chunked reads of large "
            "result sets: `open_cursor` / `fetch_cursor` / `close_cursor` / "
            "`list_cursors`."
        ),
        headline_tools=(
            "subscribe_channel",
            "poll_notifications",
            "open_cursor",
            "fetch_cursor",
            "close_cursor",
        ),
    ),
)

BUCKET_IDS: frozenset[str] = frozenset(c.id for c in CAPABILITIES)


# ---------------------------------------------------------------------------
# Tool-name → bucket mapping
#
# Order matters: overrides are checked first (verbatim name match), then
# pattern rules in declaration order. The first matching rule wins. A test
# asserts every registered tool lands in exactly one bucket.
# ---------------------------------------------------------------------------


# Specific tools whose name pattern would otherwise put them in the wrong
# bucket. Kept compact — only add an entry when the pattern can't classify.
_TOOL_TO_BUCKET_OVERRIDES: dict[str, str] = {
    # list_audit_events isn't catalogue introspection.
    "list_audit_events": "audit_trail",
    # list_active_queries / list_locks are operational, not catalogue.
    "list_active_queries": "operations_and_health",
    "list_locks": "operations_and_health",
    # list_cron_jobs is the scheduled-jobs surface.
    "list_cron_jobs": "scheduled_jobs",
    # list_notification_subscriptions / list_cursors are realtime/cursors.
    "list_notification_subscriptions": "realtime_and_cursors",
    "list_cursors": "realtime_and_cursors",
    # list_pending_migrations / list_unapplied_migration_scripts are migrations.
    "list_pending_migrations": "migrations",
    "list_unapplied_migration_scripts": "migrations",
    # list_hypertables / list_chunks / list_partitions are time-series.
    "list_hypertables": "timeseries_partitioning",
    "list_chunks": "timeseries_partitioning",
    "list_partitions": "timeseries_partitioning",
    # list_turboquant_indexes / list_pg_search_indexes are search-bucket.
    "list_turboquant_indexes": "vector_search",
    "list_pg_search_indexes": "text_search",
    # list_replicas is operational read-replica visibility.
    "list_replicas": "operations_and_health",
    # list_graphs is graph_operations.
    "list_graphs": "graph_operations",
    # find_unused_objects / find_sensitive_columns are advisor diagnostics.
    "find_unused_objects": "advisors",
    "find_sensitive_columns": "advisors",
    # find_blocking_chains is operational, not advisor.
    "find_blocking_chains": "operations_and_health",
    # walk_blocking_chains is operational.
    "walk_blocking_chains": "operations_and_health",
    # describe_graph is graph_operations.
    "describe_graph": "graph_operations",
    # generate_fk_cascade_graph is graph_operations (derives a graph).
    "generate_fk_cascade_graph": "graph_operations",
    # generate_test_data / seed_table_with_sample_data are data_movement.
    "generate_test_data": "data_movement",
    "generate_test_row_for": "data_movement",
    "seed_table_with_sample_data": "data_movement",
    # analyze_session_cost is observability — reads the audit log for
    # agent self-introspection.
    "analyze_session_cost": "observability",
    # recommend_headline_tools is observability — empirical curation
    # for describe_self's headline_tools tuples.
    "recommend_headline_tools": "observability",
    # config & sizing advisors (§16) — pghero / pgtune coverage.
    "audit_sequences": "advisors",
    "audit_settings": "advisors",
    "recommend_postgres_conf": "advisors",
    # analyze_query_plan / analyze_workload are query/advisors not vector.
    "analyze_query_plan": "query_execution",
    "analyze_workload": "advisors",
    # monitor_embedding_drift is rag_telemetry not generic monitoring.
    "monitor_embedding_drift": "rag_telemetry",
    # monitor_index_build is operations health.
    "monitor_index_build": "operations_and_health",
    # detect_n_plus_one is advisor diagnostic.
    "detect_n_plus_one": "advisors",
    # detect_vector_outliers is vector_search.
    "detect_vector_outliers": "vector_search",
    # cancel_query / terminate_backend are operations.
    "cancel_query": "operations_and_health",
    "terminate_backend": "operations_and_health",
    # cancel_migration / complete_migration / prepare_migration are migrations.
    "cancel_migration": "migrations",
    "complete_migration": "migrations",
    "prepare_migration": "migrations",
    # describe_self / describe_tool are observability (we own these).
    "describe_self": "observability",
    "describe_tool": "observability",
    # check_database_health is operations.
    "check_database_health": "operations_and_health",
    # Multi-database selector (roadmap 13.1) — discovery of configured
    # primary + read-only secondary databases.
    "list_databases": "operations_and_health",
    # read_autovacuum_priority is operations — shortlists tables for ops attention.
    "read_autovacuum_priority": "operations_and_health",
    # get_warehousepg_status is a status probe — same bucket as the other
    # `get_*_status` family entries (pgq, repack, aio, pg19_*).
    "get_warehousepg_status": "operations_and_health",
    # WarehousePG MPP read introspection (roadmap 15.2-15.5).
    "list_distribution_policies": "schema_introspection",
    "check_segment_health": "operations_and_health",
    "describe_ao_table": "schema_introspection",
    "list_resource_groups": "operations_and_health",
    "analyze_mpp_query_plan": "query_execution",
    "recommend_redistribute": "advisors",
    # Logical replication management writes (roadmap 2.1) — same bucket
    # as `get_logical_replication_status` / `enable_logical_replication_on_demand`
    # so an agent finds all the replication tooling in one place.
    "create_publication": "operations_and_health",
    "drop_publication": "operations_and_health",
    "create_subscription": "operations_and_health",
    "drop_subscription": "operations_and_health",
    # audit_database is the deep DBA advisor scan.
    "audit_database": "advisors",
    # turboquant tools whose name has `turboquant` in the middle (the
    # ^turboquant_ pattern only catches the prefix form).
    "create_turboquant_index": "vector_search",
    "get_turboquant_index_metadata": "vector_search",
    "get_turboquant_heap_stats": "vector_search",
    "get_turboquant_last_scan_stats": "vector_search",
    # lint_naming_conventions / test_rls_for_role are advisor-style.
    "lint_naming_conventions": "advisors",
    "test_rls_for_role": "advisors",
    # verify_connection_encryption is operations (TLS check).
    "verify_connection_encryption": "operations_and_health",
    # get_wal_archive_status — WAL-archiving health probe.
    "get_wal_archive_status": "operations_and_health",
    # check_pitr_readiness — point-in-time-recovery readiness advisor.
    "check_pitr_readiness": "operations_and_health",
    # verify_audit_chain stays in audit_trail (the name-pattern would route to ops).
    "verify_audit_chain": "audit_trail",
    # enable_extension is operations setup.
    "enable_extension": "operations_and_health",
    # close_cursor / open_cursor / fetch_cursor are cursors.
    "close_cursor": "realtime_and_cursors",
    "open_cursor": "realtime_and_cursors",
    "fetch_cursor": "realtime_and_cursors",
    # poll_notifications / subscribe_channel / unsubscribe_channel.
    "poll_notifications": "realtime_and_cursors",
    "subscribe_channel": "realtime_and_cursors",
    "unsubscribe_channel": "realtime_and_cursors",
    # run_cypher is graph_operations.
    "run_cypher": "graph_operations",
    # run_advisors / run_maintenance are ops not query.
    "run_advisors": "advisors",
    "run_maintenance": "operations_and_health",
    # tune_vector_index / migrate_vector_to_halfvec are vector_search.
    "tune_vector_index": "vector_search",
    # recommend_hnsw_ef_search — HNSW ef_search recall/speed advisor.
    "recommend_hnsw_ef_search": "vector_search",
    "migrate_vector_to_halfvec": "vector_search",
    # setup_rag_telemetry / setup_efficiency_observations / record_efficiency_observation.
    "setup_rag_telemetry": "rag_telemetry",
    "setup_efficiency_observations": "rag_telemetry",
    "record_efficiency_observation": "rag_telemetry",
    # log_rerank_event is rag_telemetry.
    "log_rerank_event": "rag_telemetry",
    # schedule_cron_job / unschedule_cron_job / schedule_logical_backup.
    "schedule_cron_job": "scheduled_jobs",
    "unschedule_cron_job": "scheduled_jobs",
    "schedule_logical_backup": "scheduled_jobs",
    # explain_query / why_is_this_slow / optimize_query are query/ops.
    "explain_query": "query_execution",
    "why_is_this_slow": "query_execution",
    "optimize_query": "operations_and_health",
    # compare_schemas / summarize_table / get_compact_schema are introspection.
    "compare_schemas": "schema_introspection",
    "summarize_table": "schema_introspection",
    "get_compact_schema": "schema_introspection",
    # redis_fdw — every redis-prefixed surface is the cache_and_foreign_data bucket.
    "list_redis_foreign_servers": "cache_and_foreign_data",
    "describe_redis_cache_table": "cache_and_foreign_data",
    "get_redis_cache_stats": "cache_and_foreign_data",
    "recommend_redis_cache_targets": "cache_and_foreign_data",
    "enable_redis_fdw": "cache_and_foreign_data",
    "create_redis_cache_server": "cache_and_foreign_data",
    "create_redis_user_mapping": "cache_and_foreign_data",
    "create_redis_cache_table": "cache_and_foreign_data",
    # pg_prewarm — every prewarm / autowarm surface is the cache_warming bucket.
    "get_prewarm_extension_status": "cache_warming",
    "list_prewarmed_relations": "cache_warming",
    "recommend_prewarm_targets": "cache_warming",
    "prewarm_relation": "cache_warming",
    "prewarm_recommended": "cache_warming",
    "schedule_autowarm": "cache_warming",
    "unschedule_autowarm": "cache_warming",
    "list_autowarm_jobs": "cache_warming",
    # SQL/PGQ — every pgq-related tool routes to the new bucket. Coexists
    # with the AGE-style graph_operations bucket (`run_cypher`, etc).
    "get_pgq_status": "property_graph_queries",
    "list_property_graphs": "property_graph_queries",
    "describe_property_graph": "property_graph_queries",
    "run_pgq": "property_graph_queries",
    "create_property_graph": "property_graph_queries",
    "drop_property_graph": "property_graph_queries",
    # PG 19 in-server REPACK — operational maintenance, sits next to
    # run_maintenance / check_database_health.
    "get_repack_status": "operations_and_health",
    "repack_table": "operations_and_health",
    # PG 19 async-I/O subsystem — observability + advisor; sits in the
    # operations_and_health bucket next to read_pg_stat_io.
    "get_aio_status": "operations_and_health",
    "recommend_io_method": "operations_and_health",
    # PG 19 lock + recovery analytics — sits next to find_blocking_chains
    # / walk_blocking_chains in the operations_and_health bucket.
    "get_pg19_stats_status": "operations_and_health",
    "read_pg_stat_lock": "operations_and_health",
    "read_pg_stat_recovery": "operations_and_health",
    "analyze_lock_hotspots": "operations_and_health",
    # PG 19 runtime toggles — online data_checksums + on-demand
    # logical replication. Operations + maintenance surface.
    "get_data_checksums_status": "operations_and_health",
    "enable_data_checksums": "operations_and_health",
    "disable_data_checksums": "operations_and_health",
    "get_logical_replication_status": "operations_and_health",
    "enable_logical_replication_on_demand": "operations_and_health",
    # PG 19 DDL helpers — pg_get_*def() reads route to schema_introspection
    # (cluster-level CREATE-statement dumps); validate_check_constraint is
    # a maintenance op that touches data validation, not schema shape.
    "get_pg19_ddl_status": "schema_introspection",
    "get_role_ddl": "schema_introspection",
    "get_database_ddl": "schema_introspection",
    "get_tablespace_ddl": "schema_introspection",
    "validate_check_constraint": "operations_and_health",
    # PG 19 partition reorganisation — MERGE / SPLIT PARTITION live in
    # the same bucket as the partman_* lifecycle tools.
    "get_pg19_partitions_status": "timeseries_partitioning",
    "merge_partitions": "timeseries_partitioning",
    "split_partition": "timeseries_partitioning",
    # PG 19 skip-scan advisor — sibling of recommend_indexes /
    # recommend_index_drops; routes to the advisors bucket.
    "get_skip_scan_status": "advisors",
    "recommend_skip_scan_indexes": "advisors",
    # PG 19 WAIT FOR LSN — read-your-writes consistency on standbys.
    # Operational concern (replica lag + RYW pattern) — sits next to
    # replication / replica advisors.
    "get_wait_for_lsn_status": "operations_and_health",
    "get_current_wal_lsn": "operations_and_health",
    "wait_for_lsn": "operations_and_health",
    "recommend_read_your_writes": "operations_and_health",
}


# Pattern → bucket. Tried in order; first match wins for tools not in
# the overrides dict above.
_TOOL_TO_BUCKET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), bucket)
    for pattern, bucket in (
        # Vector + ANN.
        (r"^(vector_|mmr_|hybrid_|cluster_vectors|cross_table_similarity)", "vector_search"),
        (r"^turboquant_|_turboquant", "vector_search"),
        (r"^analyze_(distance|hnsw|vector)", "vector_search"),
        (r"^recommend_(vector|turboquant)", "vector_search"),
        (r"^import_vectors", "vector_search"),
        # Text search.
        (r"^(full_text_|fuzzy_|geo_|pg_search_)", "text_search"),
        (r"^create_pg_search_index", "text_search"),
        (r"^reindex_pg_search_index", "text_search"),
        (r"^get_pg_search_index_metadata", "text_search"),
        (r"^recommend_pg_search", "text_search"),
        # RAG telemetry / reranker analytics.
        (r"^analyze_(rerank|reranker|topk)", "rag_telemetry"),
        (r"^recommend_(rerank|efficiency)", "rag_telemetry"),
        # Audit.
        (r"^(prune_|verify_)audit", "audit_trail"),
        # Data movement.
        (r"^(dump_|restore_|import_|export_|copy_)", "data_movement"),
        # Migrations.
        (r"^(validate_|read_)migration", "migrations"),
        # Time-series + partitioning.
        (r"^(add_compression_policy|add_retention_policy|create_hypertable)", "timeseries_partitioning"),
        (r"^partman_", "timeseries_partitioning"),
        # Graph.
        (r"^(create_graph|drop_graph)", "graph_operations"),
        # Diagrams + ORM codegen.
        (r"^generate_(graph_diagram|schema_diagram|schema_docs)", "diagrams_and_codegen"),
        (r"^generate_.*_(schema|schemas|models|config)$", "diagrams_and_codegen"),
        # Advisors / recommendations.
        (r"^recommend_(indexes|index_drops)", "advisors"),
        # Vector reindex falls into vector_search (must come before generic reindex).
        (r"^maintain_turboquant_index", "vector_search"),
        (r"^reindex_turboquant_index", "vector_search"),
        # Server info / observability.
        (r"^(get_server_info|get_metrics_exposition|read_pg_)", "observability"),
        # Query execution catch-all.
        (r"^(run_select|run_write|run_ddl|translate_nl_to_sql)", "query_execution"),
        # Schema introspection catch-all — last so the more specific
        # patterns above win first.
        (r"^(list_|describe_table)", "schema_introspection"),
    )
)


def classify_tool(tool_name: str) -> str | None:
    """Return the bucket id for ``tool_name``, or ``None`` if no rule matches.

    The contract test asserts this never returns ``None`` for any tool
    on the maximal-flag server — so a missing classification means the
    overrides dict or the pattern list needs an entry.
    """
    if tool_name in _TOOL_TO_BUCKET_OVERRIDES:
        return _TOOL_TO_BUCKET_OVERRIDES[tool_name]
    for pattern, bucket in _TOOL_TO_BUCKET_PATTERNS:
        if pattern.match(tool_name):
            return bucket
    return None


def build_capability_summary(tool_names: list[str]) -> dict[str, Any]:
    """Assemble the JSON payload the ``describe_self`` MCP tool returns.

    Pass the live tool list (e.g. from ``[t.name for t in server._tools]``)
    so per-bucket tool counts always match what an MCP client just listed.
    Tools without a bucket are reported under ``unclassified`` rather than
    silently dropped — they signal that this module is out of date.
    """
    by_bucket: dict[str, list[str]] = {bucket_id: [] for bucket_id in BUCKET_IDS}
    unclassified: list[str] = []
    for name in tool_names:
        bucket_id = classify_tool(name)
        if bucket_id is None:
            unclassified.append(name)
        else:
            by_bucket[bucket_id].append(name)

    capabilities_payload: list[dict[str, Any]] = []
    for cap in CAPABILITIES:
        # Filter headline_tools to those actually registered on this
        # server (a stricter flag profile may not expose all of them).
        registered = set(tool_names)
        headline = tuple(t for t in cap.headline_tools if t in registered)
        capabilities_payload.append(
            {
                "id": cap.id,
                "name": cap.name,
                "summary": cap.summary,
                "detail": cap.detail,
                "headline_tools": list(headline),
                "tool_count": len(by_bucket[cap.id]),
                "all_tools": sorted(by_bucket[cap.id]),
            }
        )

    return {
        "headline": (
            "MCPg is a production-grade PostgreSQL MCP server. It exposes "
            f"{len(tool_names)} tools across {len(CAPABILITIES)} capability "
            "buckets — see `capabilities` below. For the catalog-level view, "
            "call `list_tools` via the MCP protocol."
        ),
        "version": __version__,
        "tool_count": len(tool_names),
        "capability_count": len(CAPABILITIES),
        "capabilities": capabilities_payload,
        "unclassified_tools": sorted(unclassified),
        "next_step_hint": (
            "To learn more about a specific capability, look at its "
            "`headline_tools` and `all_tools` fields. To learn what a "
            "tool actually does, call it with no arguments to surface its "
            "validation errors, or read its description from `list_tools`."
        ),
    }
