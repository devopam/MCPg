# Changelog

All notable changes to MCPg are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Project plan, phased roadmap, and session-resume protocol (`PLAN.md`,
  `docs/PROGRESS.md`).
- ADR-0001 (build approach: hard-fork) and ADR-0002 (technology stack).
- Vendored the self-contained `sql/` SQL-safety kernel from
  `crystaldba/postgres-mcp` @ `07eb329` (MIT) into `src/mcpg/_vendor/sql/`,
  with the upstream unit tests that port cleanly.
- Project scaffold: `pyproject.toml`, packaging, `ruff`/`mypy`/`pytest`/
  coverage configuration, `NOTICE`.
- GitHub Actions CI (`.github/workflows/ci.yml`): lint, format, type-check,
  and test jobs.
- `CONTRIBUTING.md`, local `pre-commit` hooks, and GitHub issue/PR templates.
- Env-driven configuration (`mcpg.config`): `Settings`, `AccessMode`,
  `Transport`, and `load_settings`. Read-only is the default access mode and
  the settings repr redacts database credentials.
- Database connection lifecycle (`mcpg.database`): `Database` wraps the pool
  with connect/close, async-context-manager support, and a typed
  `DatabaseError`.
- MCP server bootstrap (`mcpg.server`): `create_server` builds a configured
  `FastMCP` whose lifespan owns the settings and database (no global state);
  `run` serves over the stdio, streamable-HTTP, or SSE transport.
- First MCP tool, `get_server_info` (`mcpg.tools`): reports the server
  version, access mode, transport, and database connection status.
- Console entry point: `mcpg` (and `python -m mcpg`) loads configuration
  and runs the server.
- CI now enforces the test-coverage gate (90% of authored code).
