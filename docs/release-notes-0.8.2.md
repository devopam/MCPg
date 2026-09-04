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

A focused **0.8.1 → 0.8.2** bug-fix release resolving the project's first
user-reported issue, [#329](https://github.com/devopam/MCPg/issues/329):
`dump_database` refused to export a schema whose name isn't a plain
identifier. No breaking changes; no configuration changes.

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

- **Dump-specific.** The new `_encode_schema_pattern` helper lives in
  `data_movement.py` and is used *only* by `dump_database`. The
  plain-identifier validator that the in-process SQL paths rely on
  (`export_table`, `import_csv` / `import_json` / `import_vectors`,
  `copy_table_between_databases`) is unchanged — those splice names into
  SQL text, a different contract, so relaxing them was explicitly avoided.
- **Access mode unchanged.** `dump_database` still requires `unrestricted`
  mode **and** `MCPG_ALLOW_SHELL=true`.
- **Contract unchanged.** `DumpResult` is identical, including SQL text in
  `content` when `format="plain"`.

Regression tests cover hyphens, spaces, mixed case, embedded double quotes,
pattern metacharacters, and semicolons, plus the empty-name / NUL-byte
rejection path.

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
