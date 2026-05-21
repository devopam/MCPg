# ADR-0002: Technology stack

- **Status:** accepted
- **Date:** 2026-05-20

## Context

ADR-0001 commits to hard-forking `crystaldba/postgres-mcp`. The stack must
therefore be compatible with the inherited base while meeting our standards for
TDD, type safety, and reproducibility.

## Decision

Adopt the inherited runtime stack, with our own quality/tooling layer:

| Concern            | Choice                          | Notes |
|--------------------|---------------------------------|-------|
| Language           | Python **3.12+**                | Inherited; modern typing |
| MCP framework      | Official `mcp` SDK (`FastMCP`)  | Inherited; stdio + SSE + streamable HTTP |
| Postgres driver    | `psycopg` 3 + `psycopg_pool`    | Inherited; async |
| SQL parsing        | `pglast` 7.x (libpg_query)      | Inherited; powers `safe_sql.py` |
| Packaging / env    | `uv` + `hatchling`              | Inherited build backend |
| Test runner        | `pytest` + `pytest-asyncio`     | Inherited |
| Integration tests  | Real Postgres via Docker, PG matrix | Inherited harness; extend to PG 14–17 |
| Lint / format      | `ruff`                          | Inherited; bump target off `py39` (upstream #129) |
| Type checking      | **`mypy --strict`** for new code; keep `pyright` for inherited | Our standard for authored code |
| Coverage           | `pytest-cov` + CI gate on new/changed code | Added by us |
| CI                 | GitHub Actions, PG 14–17 matrix | Added by us |
| Distribution       | PyPI + Docker image             | Inherited Dockerfile; harden later |

### Deviations from the inherited base

- Add `mypy --strict` for newly authored code (inherited code stays on
  `pyright` until modernized).
- Add a `pytest-cov` coverage gate enforcing TDD on new/changed code.
- Bump the `ruff` target from `py39` to `py312` for new code; resolve the
  upstream modernization-debt ignores as touched.
- Evaluate whether the `instructor` LLM dependency belongs in core or behind an
  optional extra (decide in Phase 5).

## Consequences

- Minimal friction adopting the fork; no driver/SDK migration.
- Two type checkers temporarily coexist (`mypy` for new, `pyright` for
  inherited) — accepted as transitional; converge to one as modernization
  progresses.
- CI must provision Docker for the integration-test matrix.
