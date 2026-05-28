# MCPg

A production-grade [Model Context Protocol](https://modelcontextprotocol.io)
server for **PostgreSQL** — letting AI agents safely inspect, query, operate,
and tune a Postgres database.\*

> **Status:** v0.5.0 released; trunk at **114 MCP tools**. Beyond
> v0.5.0's surface (NL→SQL via Anthropic / OpenAI / Gemini,
> per-request `SET ROLE` multi-tenancy, server-side cursors, hybrid
> vector+FTS search, TimescaleDB wrappers, HTTP bearer-token auth,
> Prometheus `/metrics`, RLS testing, FK cascade graphs, and the
> rest of the Tier-A/B/C shortlist), trunk adds **Apache AGE graph
> + Cypher** (six new tools — `list_graphs`, `describe_graph`,
> `create_graph`, `drop_graph`, `run_cypher`,
> `generate_graph_diagram`), **read-replica routing**
> (`MCPG_REPLICA_URLS` round-robins read-only queries across
> replicas with degraded-replica detection and primary fallback),
> and **OIDC / JWT bearer-token validation**
> (`MCPG_AUTH_MODE=oidc` swaps the static token for full JWT
> validation against an OIDC issuer's JWKS, with optional role-claim
> mapping that composes with the tenancy driver). CI matrix runs the
> integration suite against **PostgreSQL 14, 15, 16, 17, and 18**.
> See [`docs/cookbook.md`](docs/cookbook.md) for common agent
> recipes, [`docs/tour.md`](docs/tour.md) for the tool tour,
> [`CHANGELOG.md`](CHANGELOG.md) and
> [`docs/PROGRESS.md`](docs/PROGRESS.md) for detail.

## Quick start

```bash
git clone https://github.com/devopam/MCPg && cd MCPg
uv sync
MCPG_DATABASE_URL=postgresql://localhost/mydb uv run mcpg
```

See the [Installation Guide](docs/installation.md) and
[User Guide](docs/user-guide.md) to get started.

## Goals

- **Safe by default** — read-only access mode, every SQL statement parsed and
  validated; no string-interpolated queries.
- **Broad scope** — both an application data access layer and a database
  operations toolkit (health checks, index tuning, EXPLAIN analysis), gated by
  an access mode.
- **Test-driven** — every feature backed by tests against a real Postgres
  (PG 14–17 in CI).
- **Production-ready** — connection pooling, scalability, multi-tenancy,
  thorough documentation.

## Capability surface

- **Catalog introspection** — schemas, tables, columns, indexes,
  constraints, views, functions, triggers, sequences, partitions,
  policies, roles, grants, enums, domains, composite types, foreign-data
  wrappers, foreign servers, foreign tables, user mappings,
  publications, subscriptions, foreign keys, extensions, generated
  columns.
- **Visualisation** — `generate_schema_diagram` (Mermaid ER) +
  `generate_fk_cascade_graph` (Mermaid blast-radius graph of
  ON DELETE / ON UPDATE CASCADE FKs) + `generate_graph_diagram`
  (Mermaid view of an Apache AGE property graph).
- **Structural diff** — `compare_schemas` returns a typed diff
  between two schemas; `validate_migration` re-runs a candidate
  against a transient sample of real rows so failures the diff
  misses (NOT NULL on existing NULLs, CHECK violations, type
  narrowings) surface before apply.
- **Query intelligence** — `run_select`, `run_select_parallel`,
  `explain_query`, `analyze_query_plan`, `why_is_this_slow`,
  `recommend_indexes`, `analyze_workload`, `check_database_health`,
  `detect_n_plus_one`.
- **NL → SQL** — `translate_nl_to_sql` (Anthropic / OpenAI /
  Gemini via `MCPG_NL2SQL_PROVIDER`). Generated SQL passes through
  the same `SafeSqlDriver` allowlist as `run_select` before execution.
- **Server-side cursors** — `open_cursor` / `fetch_cursor` /
  `close_cursor` / `list_cursors` for pageable reads over millions
  of rows; each cursor holds a dedicated connection so long-lived
  cursors can't starve the main pool.
- **Search** — `fuzzy_search` (trigram), `full_text_search`,
  `vector_search` + `vector_range_search` + `hybrid_search`
  (pgvector + FTS via reciprocal-rank fusion), `geo_search`
  (PostGIS k-NN).
- **Apache AGE graph + Cypher** — `list_graphs`, `describe_graph`,
  `run_cypher`, `create_graph`, `drop_graph`, `generate_graph_diagram`.
  Write tools gated under `MCPG_ALLOW_DDL`.
- **Composite + advisor tools** — `summarize_table` (one-call snapshot),
  `find_unused_objects`, `find_sensitive_columns` (PII heuristic),
  `lint_naming_conventions`, `test_rls_for_role` (debug RLS as a
  target role), `list_locks`, `find_blocking_chains`,
  `read_pg_stat_io` (PG16+), `generate_test_data` (synthetic INSERT
  generator).
- **Live ops & maintenance** (gated) — `list_active_queries`,
  `run_maintenance` (VACUUM/ANALYZE), `cancel_query`,
  `terminate_backend`, `run_write`, `run_ddl`, `enable_extension`.
- **Data movement** — `export_query` / `export_table` (in-process
  CSV/JSON), `dump_database` / `restore_database` (subprocess gate),
  `import_csv` / `import_json` (COPY FROM STDIN + parametrised
  executemany), `copy_table_between_databases` (cross-DB pipeline).
- **Event streams** (gated) — `subscribe_channel` /
  `poll_notifications` / `unsubscribe_channel` /
  `list_notification_subscriptions` bridge PostgreSQL `LISTEN` / `NOTIFY`
  into the MCP tool-poll model.
- **Staged migrations** (gated) — `prepare_migration` clones a target
  schema into a shadow, applies the candidate SQL there, and surfaces
  the structural diff for review; `validate_migration` applies the
  candidate to a transient shadow with sample data; `complete_migration` /
  `cancel_migration` / `list_pending_migrations` round out the workflow.
- **TimescaleDB hypertables** (gated) — `list_hypertables`,
  `list_chunks`, `create_hypertable`, `add_compression_policy`,
  `add_retention_policy`. Degrade to `available=false` when the
  extension isn't installed.
- **ORM bridges** — eight read-only catalog → DSL exporters:
  `generate_prisma_schema`, `generate_drizzle_schema`,
  `generate_sqlalchemy_models`, `generate_sqlc_schema`,
  `generate_diesel_schema`, `generate_jooq_config`,
  `generate_ent_schemas`, `generate_ecto_schemas`.
- **Observability** — Prometheus `/metrics` endpoint on the HTTP
  transport + `get_metrics_exposition` MCP tool for stdio. Three
  series: `mcpg_tool_calls_total{tool,status}` (counter),
  `mcpg_tool_duration_seconds_*` (histogram).
- **HTTP auth** — `MCPG_HTTP_AUTH_TOKEN` for static bearer (default
  `MCPG_AUTH_MODE=static`); `MCPG_AUTH_MODE=oidc` for full JWT
  validation against an OIDC issuer's JWKS, with optional
  `MCPG_OIDC_ROLE_CLAIM` → PG role mapping.
- **Multi-tenancy via `SET ROLE`** — `MCPG_DEFAULT_ROLE` (static)
  and the `X-MCPG-Role` HTTP header drive a tenant driver that
  wraps every query in `BEGIN ... SET LOCAL ROLE "<role>" ...` so
  one MCPg process can serve N tenants from a single pool.
- **Read-replica routing** — `MCPG_REPLICA_URLS` round-robins
  `force_readonly` queries across replicas with degraded-replica
  detection + primary fallback; `list_replicas` reports per-replica
  health.

## Documentation

- [`docs/installation.md`](docs/installation.md) — Installation Guide
- [`docs/user-guide.md`](docs/user-guide.md) — User Guide
- [`docs/tour.md`](docs/tour.md) — compact tool tour (start here for discovery)
- [`docs/cookbook.md`](docs/cookbook.md) — practical agent recipes (start here for common workflows)
- [`docs/tools.md`](docs/tools.md) — reference for every MCP tool
- [`docs/architecture.md`](docs/architecture.md) — Architecture Document
- [`docs/security.md`](docs/security.md) — threat model and security controls
- [`docs/scaling.md`](docs/scaling.md) — scaling characteristics and tuning
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`PLAN.md`](PLAN.md) — master plan and phased roadmap
- [`docs/PROGRESS.md`](docs/PROGRESS.md) — live progress tracker (resume point)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup and workflow
- [`CHANGELOG.md`](CHANGELOG.md) — release notes
- [`docs/release-notes-0.5.0.md`](docs/release-notes-0.5.0.md) — v0.5.0 release summary
- [`docs/release-notes-0.4.0.md`](docs/release-notes-0.4.0.md) — v0.4.0 release summary
- [`docs/release-notes-0.3.0.md`](docs/release-notes-0.3.0.md) — v0.3.0 release summary

## License

MIT — see [`LICENSE`](LICENSE). The vendored SQL-safety kernel at
`src/mcpg/_vendor/sql/` is also MIT-licensed; see
[`NOTICE`](NOTICE) for provenance.

\* : While best intent has been put to make it production grade, it is still a developmental project and is expected to have issues. Please refer to License Terms for details on indemnity. 
