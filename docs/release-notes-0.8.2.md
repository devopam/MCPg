# MCPg v0.8.2 — release notes

**Released:** 2026-09-04
**Tool surface:** **254** tools (256 all-flags-on maximal with
`MCPG_DYNAMIC_SESSION_INTENT` enabled) — unchanged from 0.8.1. This release
adds no tools and changes no tool's registered name, schema, or count; it
fixes input validation inside one existing tool.
**Tests:** 3013 passed, 1 skipped (`tests/unit` + `tests/contract`, zero
snapshot drift) on this exact commit ahead of the tag; lint, `ruff format`,
`mypy src/mcpg`, and `bandit` all clean.
**Runtime:** Python 3.12–3.14 (`requires-python >=3.12`; CI/mypy target
3.14)

A **0.8.1 → 0.8.2** bug-fix release resolving the project's first
user-reported issue, [#329](https://github.com/devopam/MCPg/issues/329):
`dump_database` refused to export a schema whose name isn't a plain
identifier — and, on inspection, so did ~20 other tools that shared the same
over-strict identifier check. No breaking changes; no configuration changes.

## Fixed: `dump_database` now accepts quoted PostgreSQL schema names

Exporting a schema such as `adm-pgbench` failed before `pg_dump` was ever
spawned:

```text
ShellError: invalid schema name: 'adm-pgbench'
```

The `schemas` parameter was checked against the plain-identifier allowlist
(`[A-Za-z_][A-Za-z0-9_]*`), which excludes hyphens, spaces, mixed case, and
every other character PostgreSQL supports through delimited (double-quoted)
identifiers.

`pg_dump --schema` takes a *pattern*, not a literal name — it follows the
same rules as psql's `\d` commands, where an unquoted `*` / `?` / `[` acts
as a wildcard and bare letters fold to lowercase. MCPg now encodes each
entry in `schemas` as a **double-quoted literal `pg_dump` pattern** rather
than validating it against the shared allowlist. Inside the quotes every
character — pattern metacharacters included — is matched literally and
case-sensitively, so:

- the actual schema you name is the one dumped (`adm-pgbench`, `My Schema`,
  `MixedCase` all work);
- a name containing `*`, `?`, or `[abc]` can never accidentally expand into
  a wildcard match;
- an embedded double quote is doubled (`weird"name` → `"weird""name"`) per
  the same quoting rules.

Callers still pass the **bare** schema name — no SQL or shell quoting
required. Only an empty name or one containing an embedded NUL byte is
rejected up-front. MCPg's subprocess layer already invokes `pg_dump` via
argv (never a shell — see `mcpg.shell`), so quoting the pattern is both
necessary and sufficient; there is no shell-injection surface to widen.

### Scope, deliberately narrow

- **Two encodings, one for each contract.** The new `_encode_schema_pattern`
  helper lives in `data_movement.py` and is used by the two tools that drive
  `pg_dump` / `pg_restore` via argv patterns: `dump_database` (`--schema`)
  and `copy_table_between_databases` (`--schema` **and** `--table`). The
  in-process SQL tools — `export_table`, `import_csv` / `import_json` /
  `import_vectors`, and the ~20 others below — splice names into SQL text
  instead, a different contract, so they're migrated separately to
  `mcpg.identifiers.quote_identifier` rather than to this helper; see
  "The sweep" below for that list.
- **Access mode unchanged.** `dump_database` still requires `unrestricted`
  mode **and** `MCPG_ALLOW_SHELL=true`.
- **Contract unchanged.** `DumpResult` is identical, including SQL text in
  `content` when `format="plain"`.

Regression tests cover hyphens, spaces, mixed case, embedded double quotes,
pattern metacharacters, and semicolons, plus the empty-name / NUL-byte
rejection path.

## The sweep: same bug, ~20 other tools

Investigating #329 surfaced that the rejecting regex
(`[A-Za-z_][A-Za-z0-9_]*`) was copy-pasted across roughly twenty modules,
each using it to guard names it then spliced into SQL as `"{name}"`. That
regex was doing double duty: it kept the interpolation injection-safe (by
forbidding the `"` that could close the identifier) **and** it rejected every
schema, table, column, role, or channel name PostgreSQL only supports through
*delimited* (double-quoted) identifiers. So the exact same "valid name
refused" bug lurked well beyond `dump_database`.

### One shared, escape-based quoter

The fix separates the two concerns into `mcpg.identifiers`:

- **`quote_identifier(name)`** doubles embedded double quotes (`"` → `""`)
  and wraps the result, so any delimited identifier is spliced safely — the
  value can't break out of its quotes.
- **`ensure_identifier(name)`** rejects only what isn't an addressable
  identifier at all: the empty string, an embedded NUL, or a name past
  PostgreSQL's 63-byte limit (which the server would silently truncate).

It ships with an adversarial test suite (`tests/unit/test_identifiers.py`)
covering break-out attempts, doubled quotes, and byte-vs-codepoint length.

### Migrated (delimited names now work, safely escaped)

Data movement (`export_table`, `import_csv` / `import_json` /
`import_vectors`; `copy_table_between_databases` encodes its `pg_dump
--table` the same way `dump_database` does `--schema`), the pgvector suites
(`vector_ops`, `vector_tuning`, `rag_efficiency`, `pg_search`, `turboquant`),
`textsearch`, `composite`, row-level-security and multi-tenant roles (`rls`,
`tenancy`, `config` — `SET LOCAL ROLE "my-role"` now works), logical
replication, staged migrations, test-data generators, `redis_fdw`, LISTEN/
NOTIFY channels, and TimescaleDB. TimescaleDB was the subtle one: its
`create_hypertable` / policy functions take the relation as a `regclass`
*text* argument, so the double-quoted relation is itself wrapped in a
single-quoted literal — both layers are now escaped.

### Deliberately left strict

Some checks stay strict because the name is **not** used as a PostgreSQL
identifier there, so relaxing would be wrong (or a separate feature):

- **Apache AGE graph tools** (`graph`, `graph_projection`, `cypher`,
  `graph_diagram`) — labels follow AGE's own naming rules.
- **ORM / query-builder code generators** (`prisma`, `drizzle`, `diesel`,
  `ecto`, `ent`, `jooq`, `sqlc`, `sqlalchemy_export`) — the name becomes an
  identifier in generated Go / Rust / Elixir / Python source, which can't
  hold arbitrary characters without a name-mapping layer.
- **SQL/PGQ** property-graph names (`pgq`).
- The one **`regconfig`** value `textsearch` embeds in a string literal
  (`to_tsvector('<config>', …)`), where the plain-identifier rule is the
  safety boundary.

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.8.2   # or :latest
```

Or grab `mcpg-0.8.2.mcpb` from this release and double-click it into
Claude Desktop.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.8.2]` for the complete
itemised list.
