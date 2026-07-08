# De-vendoring the SQL-safety kernel — plan

**Roadmap:** 18.1 (`docs/feature-shortlist.md` §18).
**Approach:** plan-first (this doc), then a small number of **large** PRs
— chosen deliberately to minimise repeated doc-update / snapshot-regen /
regression-test churn.

## Goal

Replace the vendored `crystaldba/postgres-mcp` SQL-safety kernel
(`src/mcpg/_vendor/sql/`) with a first-party implementation MCPg owns
outright, removing the **last third-party runtime *code*** MCPg ships.
(`pglast`, `psycopg`, `psycopg-pool` remain — they're upstream *libraries*
we depend on, not vendored source. This effort is about owning the ~1.3k
lines of copied kernel code, not re-implementing a SQL parser.)

Supersedes [ADR-0001](../adr/0001-build-approach.md) (the hard-fork /
vendor decision) with a new ADR recording the first-party rebuild.

## Why now

- The vendored kernel is carved out of every quality gate — the coverage
  gate (`omit = ["src/mcpg/_vendor/*"]`), `mypy` (`exclude` + a
  `mcpg._vendor.*` `ignore_errors` override), `ruff`, and `bandit` all
  skip it (`pyproject.toml` lines 173 / 184 / 198 / 201 / 228). ~1.3k
  lines of security-critical code (the SQL allowlist!) run unchecked.
- Re-syncs from upstream are manual and re-apply local mods by hand
  (`src/mcpg/_vendor/README.md`). Owning it ends that dance.
- It's the last item blocking a "zero vendored code" claim.

## Current state (verified against the tree)

`src/mcpg/_vendor/sql/` — 6 files, ~2,457 LOC:

| File | LOC | On the used path? |
|---|---:|---|
| `safe_sql.py` | 1036 | **Yes** — `SafeSqlDriver`, the `pglast`-AST allowlist validator. |
| `sql_driver.py` | 276 | **Yes** — `SqlDriver`, `DbConnPool`, `obfuscate_password`. |
| `__init__.py` | 31 | Seam — re-exports 13 names. |
| `bind_params.py` | 816 | **No — dead.** |
| `extension_utils.py` | 246 | **No — dead.** |
| `index.py` | 52 | **No — dead.** |

**Key finding — three modules are dead weight (delete, don't rebuild).**
`safe_sql.py` and `sql_driver.py` do **not** import `bind_params`,
`extension_utils`, or `index`, and **nothing outside `_vendor/`** imports
`SqlBindParams` / `ColumnCollector` / `TableAliasVisitor` /
`IndexDefinition` / `check_extension` / `get_postgres_version` (grep:
zero hits in `src/mcpg` and `tests/`). So ~1,114 LOC + their exports are
removed outright — the first-party rebuild is only `safe_sql` +
`sql_driver`.

**Consumer surface — 4 names, a narrow, stable seam:**

| Export | src consumers | The contract |
|---|---:|---|
| `SqlDriver` | 74 files | `execute_query(sql, params=None, force_readonly=False) -> list[RowResult]`; nested `RowResult` (`.cells`); `connect()`. The universal driver type. |
| `obfuscate_password` | 8 files | Redacts credentials from DSN / error strings. |
| `SafeSqlDriver` | 4 files | `__init__(sql_driver, timeout=None)`, `_validate(query)`, `_validate_node(node)`, `async execute_query(...)`, static `execute_param_query` / `sql_to_query` / `param_sql_to_query`. |
| `DbConnPool` | 3 files | `__init__(connection_url=None, min_size=1, max_size=5)`, `pool_connect`, `close`, `is_valid`, `last_error`. |

**Local modifications to carry forward:** only ADR-0003 pool sizing
(`min_size` / `max_size` on `DbConnPool.__init__` + `pool_connect`) —
marked in `sql_driver.py:66/95/96`. Everything else is upstream-verbatim.

**Acceptance spec already exists:** `tests/vendor/sql/` — `test_safe_sql.py`
(760 LOC of adversarial SQL-injection / allowlist cases), `test_sql_driver.py`
(367), `test_obfuscate_password.py` (104). These are the behavioural
contract the first-party code must satisfy.

## Target design

A first-party package `src/mcpg/sql/`:

```
mcpg/sql/__init__.py      # exports SqlDriver, SafeSqlDriver, DbConnPool, obfuscate_password
mcpg/sql/driver.py        # SqlDriver + RowResult + DbConnPool + obfuscate_password (from sql_driver.py)
mcpg/sql/safety.py        # SafeSqlDriver: pglast parse + default-deny node allowlist (from safe_sql.py)
```

- **Same four-name export surface**, so the 74 consumers change only the
  import path (`mcpg._vendor.sql` → `mcpg.sql`) — a mechanical sweep, no
  logic edits, tool-surface + return-shape snapshots unchanged.
- The validator stays a **`pglast` AST walker** with an explicit
  **default-deny** node-type allowlist (the current model). We re-author
  it as first-party code (clearer structure, typed, doc-commented), *not*
  a blind copy — but the allowlist set is derived from, and pinned by, the
  ported adversarial tests so coverage can't silently narrow or widen.
- Once first-party, it enters the **coverage gate + `mypy --strict` +
  `ruff` + `bandit`** like the rest of `mcpg`.

## Re-architecture (own it, restructure it)

"Faithful re-author" pins the *security behaviour* identical — it does
**not** mean a line-for-line copy. Owning the code is the moment to fix
its architecture, because the current `safe_sql.py` is a 1,036-LOC
monolith that interleaves three concerns: parsing, the allowlist policy,
and execution. First-party, we split them so the security surface is
auditable and testable in isolation:

| Module | Concern | Why separate |
|---|---|---|
| `mcpg/sql/driver.py` | Connection / pool / execution (`SqlDriver`, `DbConnPool`, `RowResult`, `obfuscate_password`). | Pure I/O; carries **no** policy. |
| `mcpg/sql/allowlist.py` | The permitted-node **policy as data** — an explicit table of allowed `pglast` node types + per-node rules, plus the statement-type gate. | The entire security decision surface lives in **one auditable place** (feeds the security review below), instead of being scattered through walker branches. |
| `mcpg/sql/safety.py` | `SafeSqlDriver` — orchestrates parse → walk-against-allowlist → execute. The walker is generic; it reads the policy from `allowlist.py`. | Mechanism separated from policy: the walker can't silently widen what's allowed. |

Guardrail: **the accept/reject verdict for every case in the adversarial
suite is byte-identical to the vendored implementation** (proven by the
differential harness in the security-review gate). The restructure is
internal; behaviour does not move.

## Non-negotiable constraints

1. **No safety regression.** The ported `test_safe_sql.py` (every
   injection / stacking / DDL-escape case) stays **100% green** against
   `mcpg.sql.SafeSqlDriver`. The tool-surface and return-shape contract
   snapshots do **not** change (this touches internals, not the tool API).
2. **API parity.** The four-name surface keeps identical signatures /
   behaviour, or every call site is migrated in the same PR. No consumer
   outside `mcpg.sql` keeps a `mcpg._vendor` import.
3. **Gates fold in.** Remove every `_vendor` carve-out
   (`pyproject.toml` 173/184/198/201/228) so the new code is fully linted,
   typed (`--strict`), coverage-gated, and bandit-scanned.
4. **Licensing / provenance.** Drop `src/mcpg/_vendor/LICENSE` +
   `README.md`; remove the `license-files` entry (line 12); update root
   `NOTICE`. Supersede ADR-0001 with a new ADR-00NN ("first-party SQL
   kernel") recording the rebuild + why. Attribution to crystaldba
   (MIT) stays where honest (the new ADR notes the design lineage).

## Phased PRs (large by design)

### PR 1 — First-party kernel, proven at parity (additive)
Land `src/mcpg/sql/` (driver + safety + `__init__`) as new first-party
code. **`_vendor/` stays in place and in use** — nothing swings yet.
Port `tests/vendor/sql/*` to `tests/unit/test_sql_safety.py` /
`test_sql_driver.py` / `test_obfuscate_password.py` pointed at `mcpg.sql`,
and require them green — this *proves* behavioural parity before any
consumer moves. New code is written to pass `mypy --strict` + coverage
from day one. **Reviewable payload: the new kernel + its adversarial
suite, in isolation.** No snapshot changes.

### Security-review gate (between PR 1 and PR 2 — mandatory)
The kernel is the SQL allowlist that stands between an agent and the
database, so it does **not** swing live on a green test suite alone. A
dedicated security review runs on the PR 1 diff and must pass before PR 2:

1. **Differential verdict test.** A temporary parametrised harness feeds
   the *entire* adversarial corpus (plus a fuzz set) through **both** the
   vendored `SafeSqlDriver` and the first-party one, and asserts an
   **identical accept/reject verdict on every input**. Zero divergence is
   the bar. (The harness is deleted in PR 2 with `_vendor`.)
2. **Allowlist audit.** Line-by-line review of the permitted-node table in
   `allowlist.py` against the threat model: confirm **default-deny**, that
   no unintended node type is admitted, and that statement stacking, DDL /
   DCL, writes, `COPY … PROGRAM`, and set-returning-function escapes are
   rejected. Having the policy as one data table (the re-architecture
   above) is what makes this audit tractable.
3. **Fuzz / adversarial pass.** Malformed SQL, comment-smuggling,
   deeply-nested CTEs / sublinks, unicode-homoglyph identifiers, oversized
   inputs → must reject safely with no crash / no unbounded work.
4. **`/security-review`.** Run the repo's security-review skill over the
   PR 1 diff; triage every finding.
5. **Threat-model note** captured in the new ADR: what the validator
   defends against, and its explicit **non-goals** (it is defence-in-depth
   in front of, not a substitute for, DB-side least-privilege).

Output: a short security-review sign-off appended to the PR 1 thread (or a
`docs/reviews/` note) linked from the new ADR. Only then does PR 2 proceed.

### PR 2 — Swing consumers, delete the vendor, close the gates
Mechanical: rewrite the 74 `mcpg._vendor.sql` imports → `mcpg.sql`
(the 4 names only). Then delete `src/mcpg/_vendor/` **entirely** (incl.
the 3 dead modules) and `tests/vendor/`. Remove every `_vendor` carve-out
in `pyproject.toml`. Update docs: regenerate the `architecture.md` module
map (the generated table — `mcpg._vendor` row disappears, `mcpg.sql`
appears), rewrite the "vendored SQL-safety kernel" sections in
`architecture.md` + `CLAUDE.md`, drop the vendor callouts in
`docs/adr/0001` (mark superseded) + add the new ADR, refresh
`security.md` / `security-hardening.md` SQL-injection references. Full
suite green; `git grep _vendor` returns nothing. **This is the big,
mostly-mechanical PR** — one pass, so the doc/snapshot churn happens once.

*(Optional PR 3 — only if PR 2 grows unwieldy: split "delete + gates +
docs" out from "swing imports". Prefer to keep it in PR 2 to avoid a
half-migrated `main`.)*

## Test strategy

- The ported adversarial suite is the **gate**: it must pass unchanged
  (same assertions) against the first-party validator. Any case that
  needs an assertion change is a **red flag** to investigate, not edit.
- Add first-party unit tests for the pieces the vendored suite under-tests
  once we can see coverage (the kernel enters the 90% gate).
- Integration matrix (PG 14–18 + 19 + WarehousePG) exercises the real
  driver/pool path end-to-end — no change needed, it just now runs
  against first-party code.
- Contract snapshots (`tool_surface`, `tool_return_shapes`) must be
  **byte-identical** before/after — the canary that the tool API didn't
  move.

## Risks & mitigations

- **Subtle allowlist drift** (the validator accepts/rejects a case the
  copy did). → The ported adversarial suite pins behaviour; run it against
  *both* implementations during PR 1 to diff (a temporary parametrised
  harness), then drop the old one in PR 2.
- **`pglast` version coupling** — the AST node types are `pglast`-version-
  specific. → Keep the `pglast==7.15` pin; the node imports move verbatim.
- **A hidden consumer of a "dead" export.** → Re-run the zero-hit grep at
  PR 2 time (not just now) before deleting `bind_params` et al.
- **Big-PR review fatigue** → PR 1 carries the *thinking* (new kernel +
  tests) in isolation; PR 2 is mostly mechanical (imports + deletes +
  generated-doc regen), reviewable by the green suite + empty `_vendor`
  grep.

## Rollback

Each PR is independently revertable. Until PR 2 merges, `_vendor` is
still the live path, so PR 1 is pure addition (trivially revertable).
Post-PR-2, a revert restores `_vendor/` wholesale from git history.

## Definition of done

- `src/mcpg/_vendor/` and `tests/vendor/` deleted; `git grep _vendor` in
  `src/` + `pyproject.toml` returns nothing.
- The SQL-safety kernel is inside the coverage / `mypy --strict` / `ruff`
  / `bandit` gates like all other `mcpg` code.
- Kernel re-architected into `driver` / `allowlist` (policy-as-data) /
  `safety` modules — policy separated from mechanism.
- Adversarial SQL-safety suite green against first-party `mcpg.sql`, **and**
  the security-review gate passed: differential verdict test (0 divergence
  vs the vendored validator), allowlist audit, fuzz pass, `/security-review`,
  and a threat-model note in the new ADR.
- Tool-surface + return-shape snapshots unchanged.
- ADR-0001 marked superseded; new ADR added; `CLAUDE.md` +
  `architecture.md` "vendored kernel" sections rewritten; `NOTICE` /
  `license-files` updated.
- `docs/feature-shortlist.md` 18.1 flipped to ✅.
