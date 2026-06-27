"""Tool outputSchema contract test — guards the structured-output surface.

Closes a gap that ``test_tool_return_shapes.py`` doesn't cover: the
return-shape snapshot pins the dataclass field names of every tool's
underlying helper, but it doesn't assert that the tool's MCP
``outputSchema`` is actually populated on the wire. FastMCP auto-derives
``outputSchema`` from the function's return type annotation only — a
handler annotated ``-> dict[str, Any]`` produces ``outputSchema = None``
and the client (LangChain, LangGraph, etc.) can't validate the response.

This test:

1. Boots a maximal-flag FastMCP server (same fixture as the surface
   snapshot test).
2. Walks every registered tool.
3. For tools listed in ``_TOOLS_WITH_STRUCTURED_OUTPUT``, asserts the
   tool's ``output_schema`` is a non-empty JSON Schema and its
   declared properties include every expected field.

As we sweep more tools from ``dict[str, Any]`` to typed dataclass
returns, add their names + expected fields to the manifest below. The
manifest is the explicit "what's structured-output today" list — a PR
that touches a converted tool's return shape will trip this test
before it merges.

The companion contract test ``test_tool_return_shapes.py`` still
pins the dataclass field set itself, so a rename / removal of a
field on the helper is caught by *both* tests in concert.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcpg.config import load_settings
from mcpg.tools import register_tools

# Mirrors the loopback fixture URL the sibling surface-snapshot test
# uses — never actually connected; ``register_tools`` only reads
# ``Settings`` fields.
_FIXTURE_DB_URL = "postgresql://snapshot:snapshot@127.0.0.1:5432/snapshot"

# Map tool name → expected property keys in the auto-derived JSON Schema.
# Field sets here mirror the dataclass field set in the helper module —
# kept in sync explicitly (rather than re-derived) so the test fails loud
# on intentional shape changes, prompting the PR author to update both
# this manifest and the snapshot.
_TOOLS_WITH_STRUCTURED_OUTPUT: dict[str, frozenset[str]] = {
    # PG 19 DDL helpers (Phase 3 PR-9 — the first sweep).
    "get_pg19_ddl_status": frozenset(
        {
            "available",
            "server_version_num",
            "server_version",
            "has_pg_get_roledef",
            "has_pg_get_databasedef",
            "has_pg_get_tablespacedef",
            "detail",
        }
    ),
    "get_role_ddl": frozenset({"object_type", "object_name", "found", "ddl"}),
    "get_database_ddl": frozenset({"object_type", "object_name", "found", "ddl"}),
    "get_tablespace_ddl": frozenset({"object_type", "object_name", "found", "ddl"}),
    "validate_check_constraint": frozenset(
        {
            "table_schema",
            "table",
            "constraint_name",
            "was_valid",
            "now_valid",
            "changed",
            "validate_sql",
        }
    ),
    # PG 19 SQL/PGQ helpers (Phase 3 PR-13 sweep).
    "get_pgq_status": frozenset({"available", "server_version_num", "server_version", "detail"}),
    # List-returning handlers: FastMCP wraps `list[Dataclass]` returns into a
    # `{"result": [...]}` envelope at the top level. The per-item dataclass
    # fields live under `$defs` — the field-level snapshot test in
    # `test_tool_return_shapes.py` pins those. We only assert the envelope key
    # here so the schema-population gate still trips on a dict-typed regression.
    "list_property_graphs": frozenset({"result"}),
    "describe_property_graph": frozenset({"schema", "name", "vertex_tables", "edge_tables"}),
    "run_pgq": frozenset({"columns", "rows", "row_count", "truncated"}),
    "create_property_graph": frozenset({"schema", "name", "created"}),
    "drop_property_graph": frozenset({"schema", "name", "dropped"}),
    # PG 19 runtime toggles.
    "get_data_checksums_status": frozenset({"available", "server_version_num", "server_version", "enabled", "detail"}),
    "get_logical_replication_status": frozenset(
        {
            "available",
            "server_version_num",
            "server_version",
            "wal_level",
            "effective_wal_level",
            "max_replication_slots",
            "detail",
        }
    ),
    "enable_data_checksums": frozenset({"was_enabled", "now_enabled", "changed", "toggle_sql"}),
    "disable_data_checksums": frozenset({"was_enabled", "now_enabled", "changed", "toggle_sql"}),
    "enable_logical_replication_on_demand": frozenset(
        {"previous_wal_level", "new_wal_level", "requires_restart", "detail"}
    ),
    # PG 19 skip-scan.
    "get_skip_scan_status": frozenset({"available", "server_version_num", "server_version", "detail"}),
    "recommend_skip_scan_indexes": frozenset({"result"}),
    # PG 19 partitions.
    "get_pg19_partitions_status": frozenset({"available", "server_version_num", "server_version", "detail"}),
    "merge_partitions": frozenset(
        {
            "parent_schema",
            "parent_table",
            "source_partitions",
            "target_partition",
            "merge_sql",
        }
    ),
    "split_partition": frozenset(
        {
            "parent_schema",
            "parent_table",
            "source_partition",
            "new_partitions",
            "split_sql",
        }
    ),
    # WAIT FOR LSN.
    "get_wait_for_lsn_status": frozenset(
        {"available", "server_version_num", "server_version", "is_in_recovery", "detail"}
    ),
    "get_current_wal_lsn": frozenset({"role", "lsn"}),
    "recommend_read_your_writes": frozenset(
        {
            "recommend_use",
            "reason",
            "is_in_recovery",
            "server_version_num",
            "current_lag_bytes",
            "detail",
        }
    ),
    "wait_for_lsn": frozenset({"lsn", "timeout_ms", "timed_out", "wait_sql"}),
    # PG 19 stats.
    "get_pg19_stats_status": frozenset(
        {
            "available",
            "server_version_num",
            "server_version",
            "has_pg_stat_lock",
            "has_pg_stat_recovery",
            "detail",
        }
    ),
    "read_pg_stat_lock": frozenset({"result"}),
    "read_pg_stat_recovery": frozenset({"result"}),
    "analyze_lock_hotspots": frozenset({"available", "server_version_num", "detail", "hotspots"}),
    # PG 19 async I/O.
    "get_aio_status": frozenset(
        {
            "available",
            "server_version_num",
            "server_version",
            "io_method",
            "io_min_workers",
            "io_max_workers",
            "detail",
        }
    ),
    "recommend_io_method": frozenset({"available", "server_version_num", "detail", "recommendations"}),
    # PG 19 in-server REPACK.
    "get_repack_status": frozenset({"available", "server_version_num", "server_version", "detail"}),
    "repack_table": frozenset({"schema", "table", "concurrently", "repack_sql"}),
    # --- Long-tail sweep (batch 3): live-ops / health / catalogue reads ---
    # List-returning tools auto-wrap into a {"result": [...]} envelope.
    "list_locks": frozenset({"result"}),
    "find_blocking_chains": frozenset({"result"}),
    "walk_blocking_chains": frozenset({"cycles", "paths", "roots", "nodes", "mermaid"}),
    "read_pg_stat_io": frozenset({"available", "server_version", "rows"}),
    "read_pg_buffercache_summary": frozenset(
        {"available", "total_buffers", "free_buffers", "used_buffers", "dirty_buffers", "average_usage_count"}
    ),
    "read_pg_buffercache_relations": frozenset({"available", "relations"}),
    "read_pg_wal_records": frozenset({"available", "records"}),
    "read_pg_wal_stats": frozenset({"available", "stats"}),
    "check_database_health": frozenset({"status", "checks"}),
    "read_migration_history": frozenset(
        {"alembic", "flyway", "diesel", "django", "prisma", "golang_migrate", "goose", "sequelize"}
    ),
    "list_active_queries": frozenset({"result"}),
    "monitor_index_build": frozenset({"result"}),
    "verify_connection_encryption": frozenset(
        {"ssl", "version", "cipher", "bits", "total_connections", "encrypted_connections", "unencrypted_connections"}
    ),
    "cancel_query": frozenset({"pid", "action", "succeeded"}),
    "terminate_backend": frozenset({"pid", "action", "succeeded"}),
    "list_cron_jobs": frozenset({"result"}),
    "partman_run_maintenance": frozenset({"parent_table", "detail"}),
    "enable_extension": frozenset({"name", "enabled"}),
    # --- Batch 4: schema-introspection catalogue reads ---
    "list_schemas": frozenset({"result"}),
    "list_tables": frozenset({"result"}),
    "describe_table": frozenset({"result"}),
    "list_indexes": frozenset({"result"}),
    "list_constraints": frozenset({"result"}),
    "list_foreign_keys": frozenset({"result"}),
    "list_views": frozenset({"result"}),
    "list_functions": frozenset({"result"}),
    "list_triggers": frozenset({"result"}),
    "list_partitions": frozenset({"partitioned", "strategy", "partitions"}),
    "list_roles": frozenset({"result"}),
    "list_grants": frozenset({"result"}),
    "list_policies": frozenset({"rls_enabled", "policies"}),
    "list_sequences": frozenset({"result"}),
    "list_enums": frozenset({"result"}),
    "list_domains": frozenset({"result"}),
    "list_composite_types": frozenset({"result"}),
    "list_foreign_data_wrappers": frozenset({"result"}),
    "list_foreign_servers": frozenset({"result"}),
    "list_foreign_tables": frozenset({"result"}),
    "list_user_mappings": frozenset({"result"}),
    "list_publications": frozenset({"result"}),
    "list_subscriptions": frozenset({"result"}),
    "list_extensions": frozenset({"result"}),
    "list_available_extensions": frozenset({"result"}),
    "list_generated_columns": frozenset({"result"}),
    # --- Batch 5: vector / RAG / search family ---
    "reindex_pg_search_index": frozenset(
        {"schema", "index", "concurrently", "reindex_sql", "started_at", "completed_at", "duration_seconds"}
    ),
    "create_pg_search_index": frozenset(
        {
            "schema",
            "table",
            "columns",
            "index_name",
            "key_field",
            "options",
            "concurrently",
            "create_sql",
            "started_at",
            "completed_at",
            "duration_seconds",
        }
    ),
    "recommend_efficiency_thresholds": frozenset(
        {
            "baseline_recall_low",
            "baseline_recall_low_adapted",
            "ranking_degraded_spearman",
            "ranking_degraded_spearman_adapted",
            "pruning_ineffective",
            "pruning_ineffective_adapted",
            "rerank_lift_flat_delta",
            "rerank_lift_steep_low",
            "rerank_lift_steep_high",
            "ranking_degraded_recall",
            "corpus_size",
            "derived_from_corpus",
        }
    ),
    "setup_efficiency_observations": frozenset({"schema_created", "table_created", "indexes_created", "setup_sql"}),
    "setup_rag_telemetry": frozenset({"schema_created", "table_created", "indexes_created", "setup_sql"}),
    "record_efficiency_observation": frozenset({"observation_id"}),
    "log_rerank_event": frozenset({"event_id"}),
    "hybrid_bm25_vector_search": frozenset({"result"}),
    "pg_search_parse_query": frozenset({"parsed"}),
    "pg_search_more_like_this": frozenset({"result"}),
    "pg_search_run": frozenset({"result"}),
    "recommend_pg_search_maintenance": frozenset({"result"}),
    "get_pg_search_index_metadata": frozenset(
        {
            "schema",
            "index",
            "table",
            "columns",
            "key_field",
            "text_fields",
            "numeric_fields",
            "boolean_fields",
            "json_fields",
            "range_fields",
            "datetime_fields",
            "layer_sizes",
            "background_layer_sizes",
            "target_segment_count",
            "mutable_segment_rows",
            "sort_by",
            "search_tokenizer",
            "index_options",
        }
    ),
    "list_pg_search_indexes": frozenset({"result"}),
    "geo_search": frozenset({"available", "matches"}),
    "recommend_vector_quantization": frozenset({"result"}),
    "hybrid_search": frozenset({"available", "matches"}),
    "mmr_search": frozenset({"available", "matches"}),
    "vector_range_search": frozenset({"available", "matches"}),
    "vector_search": frozenset({"available", "matches"}),
    "full_text_search": frozenset({"result"}),
    "fuzzy_search": frozenset({"available", "matches"}),
    "add_retention_policy": frozenset({"available", "function", "details"}),
    "add_compression_policy": frozenset({"available", "function", "details"}),
    "create_hypertable": frozenset({"available", "function", "details"}),
    "list_chunks": frozenset({"available", "chunks"}),
    "list_hypertables": frozenset({"available", "hypertables"}),
    "monitor_embedding_drift": frozenset(
        {
            "available",
            "dimension",
            "drift_threshold",
            "insufficient_data",
            "drift_detected",
            "centroid_cosine_distance",
            "norm_mean_relative_change",
            "norm_std_relative_change",
            "baseline",
            "current",
            "notes",
        }
    ),
    "detect_vector_outliers": frozenset(
        {
            "available",
            "sampled_rows",
            "dimension",
            "metric",
            "k",
            "zscore_threshold",
            "total_outliers",
            "outliers",
            "cluster_stats",
        }
    ),
    "cluster_vectors": frozenset(
        {
            "available",
            "sampled_rows",
            "dimension",
            "metric",
            "iterations",
            "converged",
            "inertia",
            "centroids",
            "assignments",
        }
    ),
    "cross_table_similarity": frozenset({"available", "source_embedding_found", "source_dimension", "matches"}),
    "analyze_distance_metric": frozenset(
        {
            "available",
            "sampled_rows",
            "mean_magnitude",
            "magnitude_std",
            "magnitude_cv",
            "pre_normalised",
            "recommended_metric",
            "rationale",
        }
    ),
    "recommend_hnsw_ef_search": frozenset(
        {
            "available",
            "has_hnsw_index",
            "index_name",
            "metric",
            "k",
            "target_recall",
            "sample_queries",
            "recommended_ef_search",
            "sweep",
            "detail",
        }
    ),
    "migrate_vector_to_halfvec": frozenset(
        {
            "available",
            "already_halfvec",
            "column_type",
            "dimension",
            "row_count",
            "estimated_bytes_per_row_before",
            "estimated_bytes_per_row_after",
            "estimated_total_bytes_saved",
            "indexes",
            "migration_sql",
            "rollback_sql",
            "notes",
        }
    ),
    "vector_recall_at_k": frozenset({"metric", "k", "sample_size", "mean_recall"}),
    "tune_vector_index": frozenset(
        {"index_type", "parameters", "rationale", "create_index_sql", "row_count", "dimension"}
    ),
    "recommend_rerank_strategy": frozenset({"window_days", "retrieval_index", "summary", "findings"}),
    "analyze_rerank_ndcg": frozenset(
        {
            "window_days",
            "k",
            "model",
            "retrieval_index",
            "labeled_query_count",
            "ndcg_at_k_under_bi_order",
            "ndcg_at_k_under_cross_order",
            "delta",
            "findings",
        }
    ),
    "analyze_rerank_score_distribution": frozenset(
        {
            "window_days",
            "model",
            "retrieval_index",
            "event_count",
            "histogram",
            "bucket_edges",
            "top_decile_share",
            "findings",
        }
    ),
    "analyze_topk_stability": frozenset(
        {
            "window_days",
            "k",
            "model",
            "retrieval_index",
            "query_count",
            "mean_jaccard",
            "p25_jaccard",
            "p75_jaccard",
            "findings",
        }
    ),
    "analyze_reranker_lift": frozenset(
        {
            "window_days",
            "model",
            "retrieval_index",
            "query_count",
            "mean_spearman",
            "mean_kendall",
            "p25_spearman",
            "p75_spearman",
            "interpretation",
            "findings",
        }
    ),
    "analyze_vector_search_efficiency": frozenset(
        {
            "schema",
            "table",
            "column",
            "index_name",
            "backend",
            "metric",
            "sample_size",
            "k",
            "recall_at_k_baseline",
            "rerank_lift_curve",
            "score_rank_correlation_spearman",
            "score_rank_correlation_kendall",
            "pages_pruned_ratio_p50",
            "findings",
        }
    ),
    # --- Batch 6: FDW / extensions family (pg_prewarm, turboquant, redis_fdw) ---
    "unschedule_autowarm": frozenset({"name", "removed"}),
    "schedule_autowarm": frozenset({"jobid", "name", "schedule"}),
    "prewarm_recommended": frozenset({"dry_run", "total_blocks", "outcomes"}),
    "prewarm_relation": frozenset({"schema", "relation", "mode", "blocks_prewarmed"}),
    "list_autowarm_jobs": frozenset({"result"}),
    "recommend_prewarm_targets": frozenset(
        {"shared_buffers_blocks", "budget_blocks", "total_cost_blocks", "candidates"}
    ),
    "list_prewarmed_relations": frozenset({"result"}),
    "get_prewarm_extension_status": frozenset(
        {
            "pg_prewarm_installed",
            "pg_buffercache_installed",
            "autoprewarm_libraries_present",
            "shared_preload_libraries",
        }
    ),
    "create_redis_cache_table": frozenset({"schema", "name", "server", "key_type", "columns", "created"}),
    "create_redis_user_mapping": frozenset({"server", "user", "secret_ref", "created"}),
    "create_redis_cache_server": frozenset({"name", "address", "port", "database", "tls", "created"}),
    "recommend_redis_cache_targets": frozenset({"server", "candidates"}),
    "get_redis_cache_stats": frozenset({"server", "available", "key_count", "used_memory_bytes", "detail"}),
    "describe_redis_cache_table": frozenset(
        {"schema", "name", "server", "key_type", "key_prefix", "ttl_seconds", "columns", "options"}
    ),
    "list_redis_foreign_servers": frozenset({"result"}),
    "reindex_turboquant_index": frozenset(
        {"schema", "index", "concurrently", "reindex_sql", "started_at", "completed_at", "duration_seconds"}
    ),
    "create_turboquant_index": frozenset(
        {
            "schema",
            "table",
            "column",
            "index_name",
            "metric",
            "options",
            "concurrently",
            "create_sql",
            "started_at",
            "completed_at",
            "duration_seconds",
        }
    ),
    "maintain_turboquant_index": frozenset(
        {
            "schema",
            "index",
            "started_at",
            "completed_at",
            "duration_seconds",
            "delta_merge_performed",
            "merged_delta_count",
            "recycled_delta_page_count",
            "raw",
        }
    ),
    "recommend_turboquant_query_knobs": frozenset(
        {"probes", "oversample_factor", "max_visited_codes", "max_visited_pages"}
    ),
    "turboquant_rerank_candidates": frozenset({"result"}),
    "turboquant_approx_candidates": frozenset({"result"}),
    "recommend_turboquant_maintenance": frozenset({"result"}),
    # Optional (DataClass | None) returns are enveloped by FastMCP into {"result": ...} too.
    "get_turboquant_last_scan_stats": frozenset({"result"}),
    "get_turboquant_heap_stats": frozenset({"schema", "index", "row_count", "raw"}),
    "get_turboquant_index_metadata": frozenset(
        {
            "schema",
            "index",
            "table",
            "column",
            "access_method",
            "opclass",
            "input_type",
            "heap_relation",
            "heap_live_rows_estimate",
            "capabilities",
            "operability",
            "delta_enabled",
            "delta_live_count",
            "delta_batch_page_count",
            "delta_head_block",
            "delta_tail_block",
            "delta_page_depth",
            "delta_live_fraction",
            "delta_merge_recommended",
            "delta_merge_thresholds",
            "raw_metadata",
            "index_options",
        }
    ),
    "list_turboquant_indexes": frozenset({"result"}),
    # --- Batch 7: advisors / ops / data / write remainder ---
    "drop_subscription": frozenset({"name", "if_exists", "executed_sql", "detail"}),
    "create_subscription": frozenset(
        {"name", "publications", "enabled", "copy_data", "create_slot", "slot_name", "executed_sql", "detail"}
    ),
    "drop_publication": frozenset({"name", "if_exists", "cascade", "executed_sql", "detail"}),
    "create_publication": frozenset({"name", "all_tables", "tables", "executed_sql", "detail"}),
    "run_ddl": frozenset({"rows", "row_count", "schema_diff"}),
    "prune_audit_events": frozenset({"deleted", "cutoff", "remaining"}),
    # NB: two MaintenanceResult dataclasses exist (maintenance + turboquant);
    # run_maintenance returns mcpg.maintenance.MaintenanceResult.
    "run_maintenance": frozenset({"operation", "target", "maintenance_sql"}),
    "seed_table_with_sample_data": frozenset(
        {"schema", "table", "rows_seeded", "statements_executed", "skipped_columns"}
    ),
    "run_write": frozenset({"rows", "row_count", "schema_diff"}),
    "schedule_logical_backup": frozenset({"jobid", "name"}),
    "schedule_cron_job": frozenset({"jobid", "name"}),
    "recommend_index_drops": frozenset({"result"}),
    "recommend_indexes": frozenset({"result"}),
    "read_autovacuum_priority": frozenset({"available", "overdue_count", "watchlist_count", "rows", "detail"}),
    "detect_n_plus_one": frozenset({"available", "thresholds", "candidates"}),
    "analyze_workload": frozenset({"available", "slow_queries"}),
    "audit_database": frozenset(
        {
            "timestamp",
            "database",
            "version",
            "overall_health",
            "health_score",
            "categories",
            "top_issues",
            "recommendations",
            "raw_stats_snapshot",
        }
    ),
    "translate_nl_to_sql": frozenset(
        {"sql", "explanation", "model", "provider", "executed", "rows", "columns", "row_count", "error"}
    ),
    "analyze_query_plan": frozenset(
        {
            "total_cost",
            "estimated_rows",
            "node_types",
            "sequential_scans",
            "actual_total_time_ms",
            "actual_rows",
            "shared_blocks_read",
            "shared_blocks_hit",
            "io_read_time_ms",
            "io_write_time_ms",
            "aio_read_blocks",
            "aio_write_blocks",
        }
    ),
    "explain_query": frozenset({"plan"}),
    "run_select_parallel": frozenset({"outcomes"}),
    "run_select": frozenset({"columns", "rows", "row_count", "truncated"}),
    "list_audit_events": frozenset({"result"}),
    "copy_table_between_databases": frozenset(
        {
            "schema",
            "table",
            "schema_copied",
            "data_copied",
            "dump_exit_code",
            "restore_exit_code",
            "dump_output_bytes",
            "restore_output_bytes",
            "dump_stderr_tail",
            "restore_stderr_tail",
            "dump_argv",
            "restore_argv",
            "timed_out",
        }
    ),
    "restore_database": frozenset(
        {"exit_code", "output_bytes", "output_truncated", "timed_out", "stderr_tail", "binary", "argv"}
    ),
    "dump_database": frozenset(
        {"exit_code", "content", "output_bytes", "output_truncated", "timed_out", "stderr_tail", "binary", "argv"}
    ),
    "list_unapplied_migration_scripts": frozenset(
        {
            "available",
            "framework",
            "scripts_dir",
            "history_table",
            "applied_count",
            "pending_count",
            "pending",
            "applied",
            "notes",
        }
    ),
    "validate_migration": frozenset(
        {"target_schema", "sample_rows_per_table", "tables_sampled", "table_stats", "candidate_applied", "error"}
    ),
    "import_vectors": frozenset({"schema", "table", "format", "rows_imported"}),
    "import_json": frozenset({"schema", "table", "format", "rows_imported"}),
    "import_csv": frozenset({"schema", "table", "format", "rows_imported"}),
    "export_table": frozenset({"format", "content", "row_count", "truncated"}),
    "export_query": frozenset({"format", "content", "row_count", "truncated"}),
    "why_is_this_slow": frozenset(
        {"sql", "plan_summary", "explain_plan", "active_queries", "blocking_locks", "cache_hit_ratio", "suggestions"}
    ),
    "summarize_table": frozenset(
        {"schema", "table", "columns", "primary_key", "foreign_keys", "constraints", "indexes", "stats", "sample_rows"}
    ),
    "optimize_query": frozenset({"original_sql", "optimized_sql", "findings", "explain_summary", "rationale"}),
    "audit_settings": frozenset({"ram_aware", "findings", "examined_settings", "detail"}),
    "audit_sequences": frozenset({"available", "total_examined", "warning_pct", "critical_pct", "sequences", "detail"}),
    "analyze_session_cost": frozenset(
        {"audit_table_present", "events_examined", "lookback_minutes", "findings", "detail"}
    ),
    "generate_test_row_for": frozenset({"schema", "table", "columns", "insert_sql"}),
    "generate_test_data": frozenset({"schema", "table", "rows_generated", "statements", "skipped_columns"}),
    "test_rls_for_role": frozenset(
        {"schema", "table", "role", "rls_enabled", "active_policies", "rows_visible", "columns", "sample"}
    ),
    "lint_naming_conventions": frozenset({"schema", "schema_majority_style", "findings"}),
    "find_sensitive_columns": frozenset({"schema", "columns"}),
    "find_unused_objects": frozenset({"schema", "tables", "indexes"}),
    "run_advisors": frozenset({"schema", "rules_run", "findings"}),
    "compare_schemas": frozenset({"left_schema", "right_schema", "tables_added", "tables_removed", "tables_changed"}),
}


def _build_maximal_server() -> FastMCP:
    """Build a FastMCP server with every capability gate flipped on.

    Same shape as the fixture used by ``test_tool_surface_snapshot``;
    duplicated here to keep this test self-contained.
    """
    settings = load_settings(
        {
            "MCPG_DATABASE_URL": _FIXTURE_DB_URL,
            "MCPG_ACCESS_MODE": "unrestricted",
            "MCPG_ALLOW_DDL": "true",
            "MCPG_ALLOW_SHELL": "true",
            "MCPG_ALLOW_LISTEN": "true",
        }
    )
    server: FastMCP = FastMCP("mcpg-output-schemas-fixture")
    register_tools(server, settings)
    return server


def test_converted_tools_emit_non_empty_output_schemas() -> None:
    """Every tool in the manifest must expose a populated JSON Schema."""
    server = _build_maximal_server()
    registered = {t.name: t for t in server._tool_manager.list_tools()}

    missing_from_server: list[str] = []
    schema_missing: list[str] = []
    for name in _TOOLS_WITH_STRUCTURED_OUTPUT:
        if name not in registered:
            missing_from_server.append(name)
            continue
        schema = registered[name].output_schema
        if not schema or not schema.get("properties"):
            schema_missing.append(name)
    assert not missing_from_server, (
        f"manifest references tools not registered on the maximal server: "
        f"{', '.join(missing_from_server)}. Either the tool was removed (update the "
        f"manifest) or a capability gate is now blocking it from registering."
    )
    assert not schema_missing, (
        f"the following tools are in the structured-output manifest but their "
        f"output_schema is None / empty: {', '.join(schema_missing)}. The most "
        f"common cause is a handler whose return annotation is still "
        f"`dict[str, Any]` — change it to the helper's dataclass return type."
    )


def test_converted_tools_output_schemas_carry_expected_fields() -> None:
    """The auto-derived schema must declare every expected dataclass field.

    Asserts on the JSON Schema's ``properties`` keys. Extra fields aren't
    flagged (an additive shape change is fine); missing fields trip the
    test so a field rename / removal can't slip past.
    """
    server = _build_maximal_server()
    registered = {t.name: t for t in server._tool_manager.list_tools()}

    drift: list[str] = []
    for name, expected_fields in _TOOLS_WITH_STRUCTURED_OUTPUT.items():
        if name not in registered:
            continue  # caught by the sibling test
        schema = registered[name].output_schema or {}
        properties = set((schema.get("properties") or {}).keys())
        missing = expected_fields - properties
        if missing:
            drift.append(f"{name}: missing fields {sorted(missing)}")
    assert not drift, (
        "the following tools' output_schema fields drifted from the manifest:\n  "
        + "\n  ".join(drift)
        + "\nUpdate the manifest if the rename is intentional; otherwise revert the helper change."
    )


def test_converted_tool_count_grows_monotonically() -> None:
    """Sanity gate — the structured-output manifest should never shrink.

    FastMCP auto-wraps even ``list[dict[str, Any]]`` returns into a
    ``{"result": [...]}`` envelope, so we can't usefully canary on
    "tools still annotated dict[str, Any] should have no schema" (they
    do, just a permissive one). Instead, we lock in a floor: as we
    sweep more tools onto typed returns the manifest grows; this test
    fails when the count drops below the recorded floor.

    Bump the ``floor`` literal below when adding to the manifest;
    never decrement it without a deliberate "we're rolling back
    structured output for tool X" conversation in the PR.
    """
    floor = 192
    actual = len(_TOOLS_WITH_STRUCTURED_OUTPUT)
    assert actual >= floor, (
        f"structured-output manifest dropped from at-least-{floor} tools "
        f"to {actual}. Either bump the floor down deliberately (and document "
        f"why in the PR), or restore the manifest entries that were removed."
    )
