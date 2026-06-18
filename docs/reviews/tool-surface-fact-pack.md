# MCPg tool-surface fact pack (Phase A — static observation)

_Generated from `tests/contract/tool_surface.snapshot.json` (173 tools) and the v0.6.2 source tree. Read-only; no MCPg code changes. Token counts are char/4 estimates (±~15% vs tiktoken)._

---

## 1. Catalog shape

- Total tools registered: **173**
- Distinct verb prefixes: **65**
- Description length — median **288 chars**, p90 **669**, max **1156**, min **42**
- Parameter count — median **2**, p90 **7**, max **18**
- Required parameter count — median **1**, p90 **4**, max **14**
- Schema depth (0 = no params; 1 = flat) — median **1**, max **1**

### 1a. Verb-prefix breakdown

| Verb prefix | Tool count | % of catalog |
|---|---:|---:|
| `list_*` | 39 | 22.5% |
| `generate_*` | 13 | 7.5% |
| `analyze_*` | 9 | 5.2% |
| `recommend_*` | 8 | 4.6% |
| `get_*` | 7 | 4.0% |
| `run_*` | 7 | 4.0% |
| `read_*` | 6 | 3.5% |
| `create_*` | 4 | 2.3% |
| `find_*` | 3 | 1.7% |
| `import_*` | 3 | 1.7% |
| `partman_*` | 3 | 1.7% |
| `pg_*` | 3 | 1.7% |
| `vector_*` | 3 | 1.7% |
| `add_*` | 2 | 1.2% |
| `cancel_*` | 2 | 1.2% |
| `describe_*` | 2 | 1.2% |
| `detect_*` | 2 | 1.2% |
| `export_*` | 2 | 1.2% |
| `hybrid_*` | 2 | 1.2% |
| `monitor_*` | 2 | 1.2% |
| `reindex_*` | 2 | 1.2% |
| `schedule_*` | 2 | 1.2% |
| `setup_*` | 2 | 1.2% |
| `turboquant_*` | 2 | 1.2% |
| `validate_*` | 2 | 1.2% |
| `verify_*` | 2 | 1.2% |
| `audit_*` | 1 | 0.6% |
| `check_*` | 1 | 0.6% |
| `close_*` | 1 | 0.6% |
| `cluster_*` | 1 | 0.6% |
| `compare_*` | 1 | 0.6% |
| `complete_*` | 1 | 0.6% |
| `copy_*` | 1 | 0.6% |
| `cross_*` | 1 | 0.6% |
| `drop_*` | 1 | 0.6% |
| `dump_*` | 1 | 0.6% |
| `enable_*` | 1 | 0.6% |
| `explain_*` | 1 | 0.6% |
| `fetch_*` | 1 | 0.6% |
| `full_*` | 1 | 0.6% |
| `fuzzy_*` | 1 | 0.6% |
| `geo_*` | 1 | 0.6% |
| `lint_*` | 1 | 0.6% |
| `log_*` | 1 | 0.6% |
| `maintain_*` | 1 | 0.6% |
| `migrate_*` | 1 | 0.6% |
| `mmr_*` | 1 | 0.6% |
| `open_*` | 1 | 0.6% |
| `optimize_*` | 1 | 0.6% |
| `poll_*` | 1 | 0.6% |
| `prepare_*` | 1 | 0.6% |
| `prune_*` | 1 | 0.6% |
| `record_*` | 1 | 0.6% |
| `restore_*` | 1 | 0.6% |
| `seed_*` | 1 | 0.6% |
| `subscribe_*` | 1 | 0.6% |
| `summarize_*` | 1 | 0.6% |
| `terminate_*` | 1 | 0.6% |
| `test_*` | 1 | 0.6% |
| `translate_*` | 1 | 0.6% |
| `tune_*` | 1 | 0.6% |
| `unschedule_*` | 1 | 0.6% |
| `unsubscribe_*` | 1 | 0.6% |
| `walk_*` | 1 | 0.6% |
| `why_*` | 1 | 0.6% |

### 1b. Description-length histogram

| Length (chars) | Tools |
|---|---:|
| 0..0 | 0 |
| 1..50 | 2 |
| 51..100 | 21 |
| 101..200 | 42 |
| 201..400 | 56 |
| 401..800 | 44 |
| 801+ | 8 |

### 1c. Parameter-count histogram

| Parameters | Tools |
|---|---:|
| 0..0 | 30 |
| 1..1 | 49 |
| 2..2 | 28 |
| 3..3 | 22 |
| 4..5 | 17 |
| 6..8 | 11 |
| 9+ | 16 |

## 2. Description quality heuristics

- **Empty descriptions:** 0 / 173 (0.0%)
- **Very short (<50 chars, non-empty):** 2 / 173 (1.2%)
- **Name-restating (description adds <3 novel content words):** 2 / 173 (1.2%)
- **Gated tools missing security caveat in description:** 0 / 173 (0.0%)
- **No return-shape hint in description:** 75 / 173 (43.4%)

### 2b. Tools with very short descriptions (<50 chars)

- `list_extensions` — _List the extensions installed in the database._
- `list_triggers` — _List the user-defined triggers on a table._

### 2c. Tools whose description appears to just restate the name

_Flagged when the description contains <3 distinct content words beyond what's already in the tool name. Low-signal descriptions cost the LLM tokens without adding picking signal._

- `list_extensions` — _List the extensions installed in the database._
- `list_foreign_data_wrappers` — _List the foreign-data wrappers installed in the database._

### 2e. Tools with no return-shape hint

_75 tools whose description never says what they return (no 'returns', 'yields', 'list of', 'object with', 'rows', etc.). An LLM picker often needs to know the shape to decide whether to call this tool or another that returns a more directly-usable shape._

Top 20 by alphabetical order (full list elided to keep the report readable):

- `add_compression_policy`
- `analyze_hnsw_recall`
- `analyze_rerank_score_distribution`
- `audit_database`
- `cancel_query`
- `check_database_health`
- `create_graph`
- `create_hypertable`
- `describe_graph`
- `describe_table`
- `drop_graph`
- `enable_extension`
- `export_table`
- `find_sensitive_columns`
- `find_unused_objects`
- `full_text_search`
- `fuzzy_search`
- `generate_drizzle_schema`
- `generate_fk_cascade_graph`
- `generate_graph_diagram`
- _…and 55 more._

## 3. Token-budget cost

Total catalogue cost when surfaced to an LLM (single `tools/list` response, approximate): **~28,071 tokens**.

Per-component breakdown:

| Component | Tokens | % of total |
|---|---:|---:|
| Names | 816 | 2.9% |
| Descriptions | 14,166 | 50.5% |
| Input schemas | 13,089 | 46.6% |
| **Total** | **28,071** | 100% |

### 3a. Top 15 most-expensive tools (token budget)

| Tool | Total | Name | Desc | Schema |
|---|---:|---:|---:|---:|
| `create_pg_search_index` | 749 | 6 | 220 | 523 |
| `record_efficiency_observation` | 558 | 7 | 176 | 375 |
| `pg_search_more_like_this` | 518 | 6 | 145 | 367 |
| `monitor_embedding_drift` | 510 | 6 | 289 | 215 |
| `hybrid_bm25_vector_search` | 483 | 6 | 191 | 286 |
| `detect_vector_outliers` | 463 | 6 | 232 | 225 |
| `pg_search_run` | 458 | 3 | 211 | 244 |
| `log_rerank_event` | 450 | 4 | 132 | 314 |
| `translate_nl_to_sql` | 429 | 5 | 276 | 148 |
| `cross_table_similarity` | 402 | 6 | 170 | 226 |
| `analyze_vector_search_efficiency` | 399 | 8 | 190 | 201 |
| `schedule_logical_backup` | 399 | 6 | 214 | 179 |
| `hybrid_search` | 384 | 3 | 176 | 205 |
| `create_turboquant_index` | 379 | 6 | 154 | 219 |
| `cluster_vectors` | 372 | 4 | 188 | 180 |

### 3b. Bottom 15 cheapest tools

| Tool | Total |
|---|---:|
| `list_views` | 55 |
| `list_functions` | 54 |
| `list_active_queries` | 53 |
| `list_enums` | 53 |
| `list_cron_jobs` | 48 |
| `list_available_extensions` | 46 |
| `verify_audit_chain` | 45 |
| `get_server_info` | 44 |
| `list_foreign_servers` | 44 |
| `list_publications` | 43 |
| `list_subscriptions` | 42 |
| `list_user_mappings` | 42 |
| `list_foreign_data_wrappers` | 41 |
| `list_graphs` | 35 |
| `list_extensions` | 34 |

### 3c. Context-window context

At ~28,071 tokens, the full mcpg catalogue is 14.0% of a Claude 200k context window, 21.9% of a 128k window, and 87.7% of a 32k window. The cost is fixed per turn — every conversation pays it for every request as long as the LLM holds the catalogue in context.

## 4. Self-introspection inventory

What an MCP client (Claude Desktop, Cursor, an automation agent) sees when it first connects to mcpg, *beyond* the `tools/list` response covered by sections 1–3.

- **MCP resources exposed:** 0

- **MCP prompts exposed:** 0

- **Dedicated self-description tool present** (`about` / `capabilities` / `describe_self`): no

- **`get_server_info` tool body (current implementation):**

```python
async def get_server_info(ctx: _Ctx) -> dict[str, Any]:
        return asdict(build_server_info(ctx.request_context.lifespan_context))

    @server.tool(
        name="get_metrics_exposition",
        description=(
            "Return the in-process Prometheus-format metrics for this MCPg "
            "server. Three series: mcpg_tool_calls_total (counter by tool / "
            "status), mcpg_tool_duration_seconds (histogram by tool with "
            "sum and count). Useful when the HTTP transport's /metrics "
            "endpoint is unreachable (e.g. running over stdio) or to fetch "
            "via the MCP protocol itself."
        ),
    )
    async def get_metrics_exposition(ctx: _Ctx) -> str:
        del ctx  # context unused; tool reads from process-wide singleton
        from mcpg.observability import render_prometheus

        return render_prometheus()
```

---

## 5. Cross-cutting observations

Interpretations the raw stats above point at — not verdicts, just leads worth Phase-B verification.

- 43% of tools never describe what they return. The LLM has to call them speculatively to find out, which wastes turns when the return shape is wrong for the task.
- mcpg exposes **zero MCP resources and zero MCP prompts**. When Claude Chat or Cursor asks 'what is this server / what can it do' beyond just listing tools, there is no structured answer to surface. Self-introspection currently relies on the client reading every tool description and synthesising.
- No dedicated `about` / `capabilities` / `describe_self` tool. The closest is `get_server_info`, which mostly returns server config / version. There is no tool an LLM can call to get a human-readable answer to 'what does mcpg do?' without ingesting the full 173-tool catalogue.

---

## 6. Hand-off to Phase B

Phase A surfaces leads; Phase B (LLM behaviour observation) confirms or refutes them. The Phase-B prompt corpus should include at least:

1. **Picker-confusion probes** for the top-scoring overlap pairs from `tool-overlap-report.md` (Phase A.0). Ask the LLM to disambiguate them; if it can't, the static flag is real.
2. **Permission-denial probes** for the `missing security caveat` list. Simulate a denial; see whether the LLM can suggest the right `MCPG_*` flag.
3. **Return-shape probes** for the `no return hint` list. Ask the LLM what it expects the tool to return; compare to reality.
4. **Self-introspection probes** — 'What can this MCP server do?' / 'List mcpg's capabilities' / 'Can mcpg do X?' Measure whether the response is accurate, complete, and useful.

