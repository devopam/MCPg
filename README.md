# MCPg

A production-grade [Model Context Protocol](https://modelcontextprotocol.io)
server for **PostgreSQL** — letting AI agents safely inspect, query, operate,
and tune a Postgres database.

> **Status:** preparing the v0.1.0 release. The server is feature-complete
> for its core scope (14 tools across introspection, querying, writes, and
> tuning). See [`docs/PROGRESS.md`](docs/PROGRESS.md) for detail.

## Quick start

```bash
git clone https://github.com/devopam/MCPg && cd MCPg
uv sync
MCPG_DATABASE_URL=postgresql://localhost/mydb uv run mcpg
```

See [`docs/usage.md`](docs/usage.md) for configuration, Docker, and MCP
client setup, and [`docs/tools.md`](docs/tools.md) for the tool reference.

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

- [`docs/usage.md`](docs/usage.md) — install, configure, run, connect a client
- [`docs/tools.md`](docs/tools.md) — reference for every MCP tool
- [`PLAN.md`](PLAN.md) — master plan, architecture, technology choices, roadmap
- [`docs/PROGRESS.md`](docs/PROGRESS.md) — live progress tracker (resume point)
- [`docs/security.md`](docs/security.md) — threat model and security controls
- [`docs/scaling.md`](docs/scaling.md) — scaling characteristics and tuning
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup and workflow
- [`CHANGELOG.md`](CHANGELOG.md) — release notes

## License

See [`LICENSE`](LICENSE).
