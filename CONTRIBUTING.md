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

## Adding new tools / capabilities

The canonical playbook for adding a new MCPg capability — module layout, naming
conventions, validation patterns, tool registration, capability bucket, tests,
snapshot regen, commit cadence — lives in
[`docs/contributing/adding-tools.md`](docs/contributing/adding-tools.md). Read
it before opening a PR that touches `src/mcpg/tools.py` or adds a new module
under `src/mcpg/`.

Claude Code users: the same playbook is installed as a `mcpg-add-tool` skill
and auto-triggers when you ask to add a new tool or expose a new extension's
surface.

### Backward compatibility — no deprecations

**Adding new surface never removes existing surface.** Every tool that
ships today must keep working on PG 14-18, and a user upgrading their
database to PG 19 (or beyond) must keep every tool they relied on at PG 18.

When a new feature *would* obsolete an existing tool (e.g. SQL/PGQ vs the
AGE-style Cypher tools), the right answer is **coexist** — add a new tool
with a new name; let agents pick via a status probe (`get_pgq_status`-style).
Deprecation is a separate conversation, gated on telemetry, and would land
behind its own SemVer-major release.

Two contract tests in `tests/contract/` are the operational guards:

- `test_tool_surface_snapshot.py` — pins every tool's name, description,
  and JSON input schema; trips on any removal or signature change.
- `test_tool_return_shapes.py` — auto-derives every tool's underlying
  dataclass field set by AST-walking `src/mcpg/tools.py`, and pins the
  field names in a sibling snapshot; trips on any rename or removal of
  a field on the helper-module dataclass.

PG 19 readiness work is explicitly **additive only**; see
[`docs/plans/pg19-readiness.md`](docs/plans/pg19-readiness.md) for the
end-to-end policy.

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

## Git workflow

### Branches

- Develop feature work on `claude/<short-name>` branches off `main`
  (e.g. `claude/over-indexed-detector`, `claude/http-mtls`). One PR
  per branch; squash-merge on land.
- Never push directly to `main` and never force-push to a branch you
  don't own.
- When CI on your PR turns red after a rebase or fix push, treat it as
  the same iteration — keep pushing fixes to the same branch instead of
  opening a new PR.

### Parallel work

When several PRs land in the same window, [`docs/parallel-roadmap.md`](docs/parallel-roadmap.md)
is the conflict map and batching plan. Three rules from there worth
calling out:

- The `[Unreleased]` block in `CHANGELOG.md` is the most-conflicted
  file in any batch. Add your bullet at the top of `### Added`; when
  you hit a conflict on rebase, resolve it by keeping **both** bullets
  and ordering yours by feature impact.
- Tool descriptions in `src/mcpg/tools.py` register inside the
  existing family registrar (`_register_query`, `_register_liveops`,
  `_register_data_movement_*`, …) — don't introduce a new registrar
  unless the cluster is genuinely new. Wrap the description with
  [`_with_example(desc, "tool(arg=value)")`](src/mcpg/tools.py) so
  agents have a concrete invocation; the F2 contract test asserts
  every example's `kwarg=` names match the tool's real `inputSchema`.
- **Do NOT bump the tool count in a feature PR.** `docs/tour.md`,
  `docs/user-guide.md`, and `docs/tools.md` all carry the same
  `NNN tools` number — N parallel PRs each bumping it = guaranteed
  N-way conflict. Add the tool's *line* to `tour.md` but leave the
  count alone. A periodic doc-sync PR reconciles the number from
  `grep -c '@server.tool' src/mcpg/tools.py`.

### Commits and PRs

- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`, `ci:`. The
  scope after the type (e.g. `feat(pgvector): …`) helps the
  changelog rollups.
- **Run Local Pre-PR Code Reviews:** Always execute the AST-driven
  static reviewer script before committing or pushing a branch, to
  catch psycopg3, SQL injection, and capability gate issues before
  they trigger CI/PR review failures:
  ```bash
  uv run scratch/pr_review.py
  ```
- Keep PRs focused; update `CHANGELOG.md` under `[Unreleased]`.
- Update `docs/PROGRESS.md` when you complete a roadmap task and flip
  the row in `docs/feature-shortlist.md` / `docs/parallel-roadmap.md`
  to ✅.
- All CI checks (lint, format, type-check, tests) must pass.

### Responding to PR review

We rely heavily on automated reviewers (gemini-code-assist, sourcery).
When a review thread flags something:

- **Fix confidently and silently when the call is obviously right**
  (typo, missing test, wrong parameter name) — push a fixup commit on
  the same branch, reply with a one-liner pointing at the commit SHA,
  resolve the thread.
- **Ask before doing anything architecturally significant.** A
  reviewer suggesting "wrap this in a class hierarchy" is a question,
  not a directive — flag it back to the maintainer.
- **Push back when the reviewer is wrong.** Several real-world
  examples in our history: the OTel review claimed
  `OTEL_RESOURCE_ATTRIBUTES` takes precedence over programmatic
  `Resource.create` (it doesn't, only `OTEL_SERVICE_NAME` does);
  empirical verification beats deferred consensus.
- Don't silently swallow reviewer feedback even if you disagree —
  reply explaining the reasoning so the next person who reads the
  thread sees why the suggestion didn't land.

## Keep the living documentation current

Several documents are **living docs** — update them in the same change that
alters the behaviour they describe:

- A new or changed tool → [`docs/tools.md`](docs/tools.md) and, if it affects
  usage, [`docs/user-guide.md`](docs/user-guide.md). Add the tool's line
  to [`docs/tour.md`](docs/tour.md) under the right section, but **leave
  the tool count alone** (see the parallel-work rules above).
- A new setting or install/run change → [`docs/installation.md`](docs/installation.md).
- A new module, component, or structural change → [`docs/architecture.md`](docs/architecture.md).
- A security-relevant change → [`docs/security.md`](docs/security.md).

## Code style

`ruff` and `mypy --strict` are authoritative; their configuration lives in
`pyproject.toml`. New code targets Python 3.12 idioms.
