# MCPg

A production-grade [Model Context Protocol](https://modelcontextprotocol.io)
server for **PostgreSQL** — letting AI agents safely inspect, query, operate,
and tune a Postgres database.

> **Status:** v0.1.0 released; extension-support phases in progress. See
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
- **Test-driven** — every feature backed by tests against a real Postgres.
- **Production-ready** — connection pooling, scalability, multi-tenancy,
  thorough documentation.

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

## License

See [`LICENSE`](LICENSE).
