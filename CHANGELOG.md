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
