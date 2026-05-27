# MCPg

A production-grade [Model Context Protocol](https://modelcontextprotocol.io)
server for **PostgreSQL** — letting AI agents safely inspect, query, operate,
and tune a Postgres database.\*

> **Status:** v0.4.0 released; trunk at **107 MCP tools** with v0.5.0
> in prep. Beyond v0.4.0's catalog / search / live-ops surface, v0.5.0
> adds **HTTP transport bearer-token auth, Prometheus `/metrics`,
> TimescaleDB wrappers, hybrid (vector + FTS) search, sensitive-column
> heuristics, an N+1 detector, transient-shadow migration validation,
> per-request `SET ROLE` multi-tenancy, server-side cursors,
> RLS testing, synthetic test-data generation, FK cascade graphs, and
> a natural-language → SQL helper (Anthropic / OpenAI / Gemini)**. CI
> matrix runs the integration suite against **PostgreSQL 14, 15, 16,
> 17, and 18**. See [`docs/cookbook.md`](docs/cookbook.md) for common
> agent recipes, [`CHANGELOG.md`](CHANGELOG.md) and
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

## Capability surface (v0.4.0)

- **Catalog introspection** — schemas, tables, columns, indexes,
  constraints, views, functions, triggers, sequences, partitions,
  policies, roles, grants, enums, domains, composite types, foreign-data
  wrappers, foreign servers, foreign tables, user mappings,
  publications, subscriptions, foreign keys, extensions.
- **Visualisation** — `generate_schema_diagram` emits a Mermaid ER
  diagram an agent can paste into any Mermaid-aware renderer.
- **Structural diff** — `compare_schemas` returns a typed diff between
  two schemas (tables / columns / indexes / constraints / FKs added,
  removed, or changed).
- **Query intelligence** — `run_select`, `explain_query`,
  `analyze_query_plan`, `recommend_indexes`, `analyze_workload`,
  `check_database_health`.
- **Search** — `fuzzy_search` (trigram), `full_text_search`,
  `vector_search` (pgvector k-NN), `geo_search` (PostGIS k-NN).
- **Live ops & maintenance** (gated) — `list_active_queries`,
  `run_maintenance` (VACUUM/ANALYZE), `cancel_query`,
  `terminate_backend`, `run_write`, `run_ddl`, `enable_extension`.
- **Data movement** — `export_query` / `export_table` (in-process
  CSV/JSON), `dump_database` / `restore_database` (subprocess gate),
  `import_csv` / `import_json` (COPY FROM STDIN + parametrised
  executemany), `copy_table_between_databases` (cross-DB pipeline).
- **Event streams** (gated) — `subscribe_channel` /
  `poll_notifications` / `unsubscribe_channel` /
  `list_notification_subscriptions` bridge PostgreSQL `LISTEN`/`NOTIFY`
  into the MCP tool-poll model.
- **Staged migrations** (gated) — `prepare_migration` clones a target
  schema into a shadow, applies the candidate SQL there, and surfaces
  the structural diff for review; `complete_migration` /
  `cancel_migration` / `list_pending_migrations` round out the workflow.
- **ORM bridges** — eight read-only catalog → DSL exporters:
  `generate_prisma_schema`, `generate_drizzle_schema`,
  `generate_sqlalchemy_models`, `generate_sqlc_schema`,
  `generate_diesel_schema`, `generate_jooq_config`,
  `generate_ent_schemas`, `generate_ecto_schemas` cover the major
  Postgres-aware ORM ecosystems.

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

See [`LICENSE`](LICENSE).

\* : While best intent has been put to make it production grade, it is still a developmental project and is expected to have issues. Please refer to License Terms for details on indemnity. 
