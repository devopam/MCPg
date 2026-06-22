# Adding a new tool / capability to MCPg

Every new capability in MCPg follows the same shape. Don't reinvent — this guide documents the conventions so the surface stays uniform for LLM agents and the codebase stays readable. Follow each section in order.

> Claude Code users: this playbook is also installed as a [`mcpg-add-tool`](../../) skill at `~/.claude/skills/mcpg-add-tool/SKILL.md`. The skill auto-triggers when the user asks to add a new tool or expose a new extension's surface, and reads the same content as this file.

## 0a. Backward compatibility — no deprecations

**Adding new surface never removes existing surface.** This is a hard rule, not a soft preference. Every tool that ships today must keep working on PG 14-18, and a user upgrading their database to PG 19 (or beyond) must keep every tool they relied on at PG 18.

When a new feature *would* obsolete an existing tool (e.g. SQL/PGQ vs the AGE-style Cypher tools, or in-server `REPACK` vs the pg_repack shell-out path), the right answer is **coexist** — add a new tool with a new name; let agents pick via a status probe (`get_pgq_status`-style). Deprecation conversations are gated on telemetry and land behind their own SemVer-major release. Don't pre-empt them.

Concretely when writing a PR:

- **Don't delete a tool** — the `tests/contract/test_tool_surface_snapshot.py` contract test will trip if you do.
- **Don't rename a tool** — bind the new behaviour to a new name.
- **Don't change a tool's return shape** — add new fields with safe defaults; never remove or rename existing ones. The `tests/contract/test_tool_return_shapes.py` snapshot pins the dataclass field set per tool, so any rename / removal lights up red in CI.
- **Version-detect inside the tool when a faster PG ≥ 19 path is available** — keep the PG ≤ 18 fallback in the same function, e.g. `if server_version_num >= 190000: use pg_get_acl() else: use the catalogue-walking query`. The result shape doesn't change; only the SQL underneath does.

The end-to-end PG 19 readiness policy lives in [`docs/plans/pg19-readiness.md`](../plans/pg19-readiness.md).

## 0. Branch and commit cadence

- Branch off `main`: `git checkout -b claude/<feature>-coverage`.
- **Commit and push after every logical slice** — module, tool registrations, bucket, tests, snapshot regen, changelog. The prior context-loss incident was caused by trying to land a 5-batch refactor in one shot. Smaller commits survive context resets.
- Run the full lint / type / test gate before every commit.

## 1. Module layout — `src/mcpg/<feature>.py`

Pattern from `src/mcpg/redis_fdw.py`, `src/mcpg/pg_prewarm.py`, `src/mcpg/cron.py`:

```python
"""<Feature> coverage — <one-line summary of read/write/advisor surface>.

<2-4 paragraph context: what the feature is, why operators reach for it,
what gap MCPg fills. End with a 'Security posture' block if the feature
is DDL-touching or credentials-touching.>
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

# Module-level constants (mode allowlists, defaults). Inline so the
# tool descriptions and the validator share the same source of truth.
_VALID_MODES = frozenset({"a", "b"})
_DEFAULT_LIMIT = 20


class <Feature>Error(Exception):
    """Raised when a <feature> operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses — one per return shape. ALL frozen=True, slots=True.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class <Thing>:
    """Short docstring explaining the shape."""
    field_a: str
    field_b: int | None
    items: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Read helpers — return dataclasses; deterministic fallback when feature absent.
# ---------------------------------------------------------------------------

async def list_<thing>(driver: SqlDriver) -> list[<Thing>]:
    if not await extension_installed(driver, "<extension_name>"):
        return []
    rows = await driver.execute_query("SELECT ... FROM ...", force_readonly=True)
    return [<Thing>(field_a=row.cells["a"], ...) for row in rows or []]


# ---------------------------------------------------------------------------
# DDL/write helpers — validate identifiers, then parameter-bind.
# ---------------------------------------------------------------------------

def _validate_identifier(label: str, value: str) -> str:
    if not _IDENT_RE.match(value):
        raise <Feature>Error(f"{label} {value!r} is not a valid unquoted SQL identifier")
    return value

async def create_<thing>(driver: SqlDriver, *, name: str, ...) -> CreateResult:
    _validate_identifier("name", name)
    if not await extension_installed(driver, "<extension_name>"):
        raise <Feature>Error("<extension> is not installed; call enable_extension('<extension>') first")
    await driver.execute_query(
        f'CREATE ... "{name}" ...',
        force_readonly=False,
    )
    return CreateResult(name=name, ..., created=True)


# ---------------------------------------------------------------------------
# Advisor (when applicable) — read-only; sorts by impact; respects budget.
# ---------------------------------------------------------------------------

async def recommend_<thing>(
    driver: SqlDriver,
    *,
    limit: int = _DEFAULT_LIMIT,
    budget_pct: float = 60.0,
) -> RecommendResult:
    ...


__all__ = [
    "<Feature>Error",
    "<Thing>",
    "list_<thing>",
    "create_<thing>",
    "recommend_<thing>",
]
```

### Rules

- **Every public return shape is a `@dataclass(frozen=True, slots=True)`** — never raw dicts. The tool layer calls `asdict()` on the dataclass to produce the MCP response.
- **Reads return empty + are silent when the extension is absent**; writes raise a descriptive `<Feature>Error` saying to call `enable_extension('<name>')` first.
- **Parameter-bind every value** (`%s` placeholders). Where an identifier can't be bound (e.g. `CREATE SERVER name`, `CREATE EXTENSION name`), validate against an allowlist or `^[A-Za-z_][A-Za-z0-9_]*$` regex first — that's the injection guard.
- **Never f-string a value into a SQL string** unless you've already validated against an allowlist. bandit B608 will flag it; the validator at the boundary is the answer.
- **Secrets are always indirection**: write tools that need a password / token take a `secret_ref` arg and resolve it through `mcpg.secrets.build_secrets_provider(env)`. Never accept the raw value at the tool boundary.

## 2. Identifier / SQL-injection patterns

Where a value can be bound: use `%s`.

Where it can't (DDL identifier slots):
- Validate against `_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")` for names.
- Validate against `frozenset({"a", "b", ...})` for enum-like fields (modes, key types, etc).
- For string options inside `OPTIONS (...)`: reject `'`, `"`, `;`, `\`, `\n`, `\r` at the boundary, then double-escape `'` -> `''`.
- For schema-filter parameters that can be NULL: use `(%s::text IS NULL OR n.nspname = %s)` not f-string concatenation. Keeps bandit happy.

## 3. Tool registration — `src/mcpg/tools.py`

### Import the module

Add to the alphabetical import block at the top of `src/mcpg/tools.py`:

```python
from mcpg import (
    ...,
    <feature>,
    ...,
)
```

### Add `_register_*` functions

Two or three functions per feature:
- `_register_<feature>_reads(server)` — registered under `Capability.READ`.
- `_register_<feature>_writes(server)` — registered under `Capability.WRITE`.
- `_register_<feature>_ddl(server)` — registered under `Capability.DDL` + `settings.allow_ddl`.

### Tool decoration shape

There are two shapes — pick the **typed-return** shape for any new tool you write, so the auto-derived MCP `outputSchema` becomes available to LangChain / LangGraph / typed-state agent clients. The legacy `dict[str, Any]` shape stays valid for existing tools until we sweep them over.

**Typed-return shape (preferred for new tools):**

```python
@server.tool(
    name="get_<thing>",
    description=_with_example(
        "<One-paragraph description ending with the wire shape — "
        "Returns an object with `field_a` (description), `field_b` (description), ...>",
        "get_<thing>(arg='value')",
    ),
)
async def get_<thing>(ctx: _Ctx, arg: str) -> <feature>.ThingResult:
    # Returns the dataclass directly — FastMCP auto-derives the
    # outputSchema from the type annotation. Don't call asdict().
    return await <feature>.get_<thing>(_driver(ctx), arg)
```

Two rules for the dataclass that the tool returns:

- **No `slots=True`** on the dataclass — the slot descriptors leak into Pydantic's introspection and FastMCP falls back to `outputSchema = None`. Use `@dataclass(frozen=True)` only.
- **Avoid Pydantic-reserved field names** — at minimum: `schema`, `model_*`, `copy`, `dict`, `parse_obj`. If your domain field is named `schema` (a SQL schema identifier, for example), rename to `table_schema` and document the rename in the tool's `Returns an object with …` sentence. The contract test at `tests/contract/test_tool_output_schemas.py` catches the shadow warning.

**Legacy `dict[str, Any]` shape (existing tools — sweep when convenient):**

```python
@server.tool(
    name="list_<thing>",
    description=_with_example(
        "<…description…> Returns a list of objects with `field_a` …",
        "list_<thing>(arg='value')",
    ),
)
async def list_<thing>(ctx: _Ctx, arg: str) -> list[dict[str, Any]]:
    async def _run() -> list[dict[str, Any]]:
        items = await <feature>.list_<thing>(_driver(ctx), arg)
        return [asdict(i) for i in items]
    return await _cached_call(ctx, "list_<thing>", _run, arg)
```

When sweeping a legacy tool onto the typed-return shape:

1. Drop `slots=True` from the dataclass.
2. Change the handler's return annotation to the dataclass type.
3. Replace `return asdict(result)` with `return result`.
4. Add the tool's name + expected field set to the manifest in `tests/contract/test_tool_output_schemas.py`.
5. Regenerate the surface + return-shape snapshots (`MCPG_REGENERATE_TOOL_SNAPSHOT=1` / `MCPG_REGENERATE_TOOL_RETURN_SHAPES=1`).
6. Bump the `_FLOOR` constant in `test_tool_output_schemas.py::test_converted_tool_count_grows_monotonically`.

Write/DDL tools clear the cache after running:

```python
@server.tool(name="create_<thing>", description=_with_example("...", "..."))
async def create_<thing>(ctx: _Ctx, name: str, ...) -> dict[str, Any]:
    result = await <feature>.create_<thing>(_driver(ctx), name=name, ...)
    await ctx.request_context.lifespan_context.cache.clear()
    return asdict(result)
```

### Wire into the dispatch block at the bottom of `tools.py`

```python
if is_permitted(settings.access_mode, Capability.READ):
    ...
    _register_<feature>_reads(server)
if is_permitted(settings.access_mode, Capability.WRITE):
    ...
    _register_<feature>_writes(server)
if is_permitted(settings.access_mode, Capability.DDL) and settings.allow_ddl:
    ...
    _register_<feature>_ddl(server)
```

## 4. Description conventions — the "Returns ..." sentence

This is the headline fix from PR #121 (Phase A finding #2). Every tool description ends with a `Returns ...` sentence that lists the **actual** dataclass fields (not generic "an object").

```
Returns an object with `name`, `address`, `port`, `database`, `tls` (bool),
`password_configured` (bool), and `options` (the full server-options dict).
```

For lists:

```
Returns a list of objects with `name` (the index name), `method`
(btree / gin / gist / brin / hash / spgist / hnsw / ivfflat / …),
`definition`, and `partitioned`.
```

For nested shapes, say so explicitly:

```
Returns an object with `candidates` — a list of objects with `schema`,
`relation`, `reason` (one of `read_only_lookup_table` / `small_hot_relation` /
`read_heavy_low_write` / `moderate_read_dominant`), and `ready_to_run_sql`.
```

### Why this matters

The Phase B LLM observation harness measured how often Claude correctly inferred return shape from description. Tools without the Returns sentence had hit rates ~40% lower than tools with it. The sentence is the single biggest lever on agent reliability.

## 5. Naming conventions

| Pattern | Use for | Examples |
|---|---|---|
| `list_<thing>` | Catalog enumeration | `list_redis_foreign_servers`, `list_prewarmed_relations`, `list_indexes` |
| `describe_<thing>` | Single-object detail | `describe_redis_cache_table`, `describe_table` |
| `get_<thing>` | One-shot status / metric | `get_prewarm_extension_status`, `get_server_info` |
| `recommend_<thing>` | Advisor output | `recommend_redis_cache_targets`, `recommend_prewarm_targets`, `recommend_indexes` |
| `analyze_<thing>` | Analytical computation | `analyze_query_plan`, `analyze_reranker_lift` |
| `enable_<extension>` | Wrap CREATE EXTENSION | `enable_redis_fdw` |
| `create_<thing>` | DDL writes | `create_redis_cache_server`, `create_hypertable` |
| `schedule_<thing>` / `unschedule_<thing>` | pg_cron registration pairs | `schedule_autowarm`, `schedule_cron_job` |
| `prewarm_<thing>` / `dump_<thing>` | Action verbs | `prewarm_relation`, `dump_database` |

Avoid generic verbs like `do_*`, `process_*`, `handle_*` — they don't help an LLM pick. Prefer specific verbs tied to the operation.

snake_case everywhere. No leading underscores on tool names (they're public API).

## 6. Capability bucket — `src/mcpg/about.py`

If the feature is a distinct operational area, add a new `Capability(...)` entry in the `CAPABILITIES` tuple. Otherwise map the new tools into an existing bucket via `_TOOL_TO_BUCKET_OVERRIDES`.

### New bucket pattern

```python
Capability(
    id="<bucket_id>",                          # snake_case, stable forever
    name="<Display name>",                     # title case
    summary=(
        "<One sentence (≤140 chars), agent-friendly summary of what the "
        "bucket covers.>"
    ),
    detail=(
        "<2-3 sentences with concrete tool names called out. This is what "
        "an LLM sees when it expands the bucket.>"
    ),
    headline_tools=(
        "<the 3-6 tools an agent should reach for first>",
    ),
),
```

### Override every new tool name

Even if the tool name *would* be classified by a `_TOOL_TO_BUCKET_PATTERNS` regex, add an explicit override row in `_TOOL_TO_BUCKET_OVERRIDES`. This makes the mapping audit-friendly and prevents pattern-match drift.

```python
_TOOL_TO_BUCKET_OVERRIDES: dict[str, str] = {
    ...
    "list_<thing>": "<bucket_id>",
    "describe_<thing>": "<bucket_id>",
    "recommend_<thing>": "<bucket_id>",
    "create_<thing>": "<bucket_id>",
    ...
}
```

The contract test `tests/unit/test_about.py::test_every_registered_tool_classifies_into_a_bucket` will fail loudly if you miss one.

## 7. Tests — `tests/unit/test_<feature>.py`

Use `FakeRoutingDriver({substring: rows})` from `tests/unit/_fakes.py`. Pattern:

```python
"""Tests for the <feature> coverage module."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.<feature> import (
    <Feature>Error,
    <Thing>,
    list_<thing>,
    create_<thing>,
    recommend_<thing>,
)


async def test_list_returns_empty_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": []})
    assert await list_<thing>(driver) == []  # type: ignore[arg-type]


async def test_list_parses_rows() -> None:
    driver = FakeRoutingDriver({
        "FROM pg_extension WHERE extname": [{"present": 1}],
        "FROM <main_table>": [{"name": "a", ...}],
    })
    result = await list_<thing>(driver)  # type: ignore[arg-type]
    assert result == [<Thing>(name="a", ...)]


async def test_create_emits_expected_ddl() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    await create_<thing>(driver, name="x", ...)  # type: ignore[arg-type]
    queries = " | ".join(call[0] for call in driver.calls)
    assert 'CREATE ... "x"' in queries


async def test_create_rejects_bad_identifier() -> None:
    driver = FakeRoutingDriver({"FROM pg_extension WHERE extname": [{"present": 1}]})
    with pytest.raises(<Feature>Error, match="identifier"):
        await create_<thing>(driver, name="bad; DROP")  # type: ignore[arg-type]


async def test_recommend_classifies_correctly() -> None:
    # One row per classifier branch — verify each `reason` code is emitted.
    ...
```

### Coverage targets

For every public function:
- **Happy path** — happy input, expected output.
- **Extension-absent path** — confirms graceful fallback (empty list for reads, error for writes).
- **Validation rejection** — confirms each `<Feature>Error` raise path.
- **Edge cases** — empty input, zero-size relation (div-by-zero), boundary values.

For every advisor:
- **All classifier branches** — one input row that exercises each `reason` code.
- **Budget / threshold enforcement** — confirm filtering boundaries.
- **Sort order** — confirm the documented ranking.

For every DDL helper:
- **SQL shape** — assert specific substrings in `driver.calls`.
- **Injection guard** — confirm `'`, `"`, `;`, etc are rejected.
- **Idempotency** — `IF NOT EXISTS` / parameterized re-run safe.

Aim for 20-30 tests per medium-sized module (redis_fdw shipped 25, pg_prewarm shipped 24).

`# type: ignore[arg-type]` after passing a fake driver is the existing convention — mypy doesn't follow our SqlDriver protocol perfectly, but mypy runs only on `src/mcpg` in CI so the unused-ignore warnings on tests don't gate anything.

## 8. Snapshot regen + contract tests

Two contract snapshots live in `tests/contract/`. Regenerate **both** when adding or changing a tool, and commit both diffs alongside the source change.

```bash
# Surface snapshot — name / description / inputSchema for every tool.
MCPG_REGENERATE_TOOL_SNAPSHOT=1 python -m pytest tests/contract/test_tool_surface_snapshot.py

# Return-shape snapshot — dataclass field set for every tool the AST walk
# can classify. Regenerate whenever a dataclass field set changes (added
# field, renamed field, etc) — the no-deprecation rule still applies, so
# renames / removals should be deliberate and reviewed.
MCPG_REGENERATE_TOOL_RETURN_SHAPES=1 python -m pytest tests/contract/test_tool_return_shapes.py
```

Then run all contract tests without regen to confirm a clean diff:

```bash
python -m pytest tests/contract/ tests/unit/test_about.py
```

If `test_tool_surface_matches_snapshot` fails after a Gemini review suggestion edited `src/mcpg/tools.py`, regenerate again — the pattern from PR #121.

The return-shape snapshot is the operational guard for the no-deprecation rule on dataclasses: rename or remove a field on `RepackResult` (say) and the snapshot diff makes it visible in review. The auto-derivation works whenever the tool handler follows the standard pattern (`asdict(<module>.<helper>(...))` for scalars, `[asdict(i) for i in <module>.<helper>(...)]` for lists). For ad-hoc-dict handlers, the snapshot records `"opaque"` — that's fine for the handful of code-emitting tools (ORM generators, `describe_self`, etc.) but **stick to the asdict pattern by default** so the new tool is covered by the guard.

## 9. Quality gate before every commit

```bash
source .venv/bin/activate
ruff check src/mcpg/<feature>.py tests/unit/test_<feature>.py
ruff format src/mcpg/<feature>.py tests/unit/test_<feature>.py
mypy src/mcpg/<feature>.py
bandit -q -r src/mcpg/<feature>.py
python -m pytest tests/unit/test_<feature>.py tests/contract/
```

All five must be green. Bandit medium+ findings are blockers — the redis_fdw branch tripped on `0.0.0.0` in a loopback-host set (B104) and on a schema-filter f-string (B608); both were quickly fixed at validation boundaries.

## 10. CHANGELOG.md

Add a feature entry under `## [Unreleased]`:

```markdown
### Added

- **`<feature>` coverage** (`mcpg.<feature>`). N new tools across read +
  write surfaces: `list_<thing>`, `describe_<thing>`, ...

  <Paragraph on the headline advisor / why this matters.>

  Security posture:
  - <validation guard>
  - <credential plumbing>

  New `<bucket_id>` capability bucket in `mcpg.about` so `describe_self`
  advertises the coverage cleanly. `<extension>` added to
  `ENABLEABLE_EXTENSIONS`. Closes #N.
```

## 11. PR body shape

Markdown structure that has reviewed cleanly:

```
## Summary

Closes #N. <One-sentence description.>

### New tools (N)

<table | bullet list with surface column + purpose>

### Security posture

- <bullet per guard>

### Module + plumbing

- New `src/mcpg/<feature>.py` (~N lines) — <breakdown>.
- <extension> added to `ENABLEABLE_EXTENSIONS`.
- New `<bucket_id>` capability bucket in `mcpg.about`; every tool mapped via `_TOOL_TO_BUCKET_OVERRIDES`.
- Snapshot regenerated; contract test passes.

### Tests

- N unit tests in `tests/unit/test_<feature>.py` covering ...
- Full unit + contract suite: <N> tests pass locally.

## Test plan

- [x] `python -m pytest tests/unit/test_<feature>.py` — N / N pass
- [x] `python -m pytest tests/unit/ tests/contract/` — <total> pass
- [x] `ruff check` / `mypy` / `bandit` — clean
```

## 12. Recurring gotchas

- **Snapshot drift after Gemini review**: Gemini suggestions touch tool descriptions directly via GitHub UI, which desyncs the snapshot. Pull the branch, run snapshot regen, push the diff.
- **Cron job names need a `mcpg_` prefix**: so `list_autowarm_jobs` / similar can filter them deterministically.
- **`pg_cron` returns booleans as `True`/`False` not 1/0**: use `bool(rows[0].cells["removed"])` not `rows[0].cells["removed"] == 1`.
- **`pg_buffercache` joins use `pg_relation_filenode(c.oid)` not `c.relfilenode`** — the latter is 0 for partitioned parents.
- **`shared_buffers` is human-readable** (`"128MB"`) — convert via `pg_size_bytes(current_setting('shared_buffers')) / current_setting('block_size')::int`.
- **`extension_installed()` is a single round-trip** — call it once per function, not in a loop.

## 13. Cross-references

- Real-world worked examples to copy from:
  - `src/mcpg/redis_fdw.py` + `tests/unit/test_redis_fdw.py` (PR #122) — FDW + DDL + advisor + secrets backend
  - `src/mcpg/pg_prewarm.py` + `tests/unit/test_pg_prewarm.py` (PR pending) — extension status + buffer reads + advisor + cron scheduler
  - `src/mcpg/cron.py` + `tests/unit/test_cron.py` — pg_cron pattern, write tools
  - `src/mcpg/extensions.py` — allowlist guard for `CREATE EXTENSION`
  - `src/mcpg/about.py` — capability buckets + overrides
- Style: `CONTRIBUTING.md`
- Phase A static analyser (helps spot tools whose descriptions still need a Returns sentence): `tools/static_tool_facts.py`
