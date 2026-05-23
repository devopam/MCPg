# MCPg

A production-grade [Model Context Protocol](https://modelcontextprotocol.io)
server for **PostgreSQL** — letting AI agents safely inspect, query, operate,
and tune a Postgres database.

> **Status:** v0.3.0 released. **45 MCP tools** covering deep catalog
> introspection (including custom types, foreign-data wrappers, and
> logical replication), index intelligence, full-text/trigram/vector/
> geospatial search, live ops, gated maintenance, Mermaid ER diagrams,
> and structural schema diff. CI matrix runs the integration suite
> against **PostgreSQL 14, 15, 16, and 17**. See
> [`CHANGELOG.md`](CHANGELOG.md) and [`docs/PROGRESS.md`](docs/PROGRESS.md)
> for detail; [`PLAN.md`](PLAN.md) §11 has the post-0.3.0 roadmap.

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

## Capability surface (v0.3.0)

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

## Documentation

- [`docs/installation.md`](docs/installation.md) — Installation Guide
- [`docs/user-guide.md`](docs/user-guide.md) — User Guide
- [`docs/tools.md`](docs/tools.md) — reference for every MCP tool
- [`docs/architecture.md`](docs/architecture.md) — Architecture Document
- [`docs/security.md`](docs/security.md) — threat model and security controls
- [`docs/scaling.md`](docs/scaling.md) — scaling characteristics and tuning
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`PLAN.md`](PLAN.md) — master plan and phased roadmap
- [`docs/PROGRESS.md`](docs/PROGRESS.md) — live progress tracker (resume point)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup and workflow
- [`CHANGELOG.md`](CHANGELOG.md) — release notes
- [`docs/release-notes-0.3.0.md`](docs/release-notes-0.3.0.md) — v0.3.0 release summary

## License

See [`LICENSE`](LICENSE).
