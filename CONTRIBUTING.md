# Contributing to MCPg

Thanks for your interest in MCPg. This document covers how we work.

## Project model

- The plan and roadmap live in [`PLAN.md`](PLAN.md); current progress and the
  resume point live in [`docs/PROGRESS.md`](docs/PROGRESS.md).
- Significant decisions are recorded as ADRs in [`docs/adr/`](docs/adr/).

## Development setup

MCPg uses [`uv`](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync                    # install dependencies into .venv
uv run pytest              # run the test suite
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy src/mcpg       # type-check
```

Install the git hooks once: `uv run pre-commit install`.

## Test-Driven Development

MCPg is a TDD project. For all **authored** code:

1. **Red** — write a failing test that specifies the desired behaviour.
2. **Green** — write the minimum code to make it pass.
3. **Refactor** — clean up with the test as a safety net.

Do not open a PR with production code that has no accompanying test. The CI
coverage gate applies to authored code (`src/mcpg`, excluding `_vendor`).

## Vendored code

`src/mcpg/_vendor/` contains pinned third-party code (see its `README.md`). Do
not hand-edit it. To update it, follow the documented re-sync procedure. Before
modifying code that *calls into* the vendored kernel in a way that depends on
its behaviour, add characterization tests first.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`, `ci:`.
- Keep PRs focused; update `CHANGELOG.md` under `[Unreleased]`.
- Update `docs/PROGRESS.md` when you complete a roadmap task.
- All CI checks (lint, format, type-check, tests) must pass.

## Code style

`ruff` and `mypy --strict` are authoritative; their configuration lives in
`pyproject.toml`. New code targets Python 3.12 idioms.
