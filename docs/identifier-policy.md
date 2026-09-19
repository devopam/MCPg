---
title: Identifier policy
---

# Identifier policy

Which object names MCPg accepts, and why a few tools are stricter than the
rest. Everything below was checked against the code (`src/mcpg/identifiers.py`
and the modules named in each row); if you change a validator, update this page.

## The rule for SQL tools

Any name PostgreSQL accepts as a *delimited* (double-quoted) identifier works
in tools whose name reaches PostgreSQL as a SQL identifier — hyphens
(`adm-pgbench`), spaces, mixed case, unicode, even an embedded `"`.

`mcpg.identifiers.quote_identifier` makes that safe by **escaping**, not
forbidding: an embedded `"` is doubled (`"` → `""`), so the value cannot close
the quoted identifier and inject SQL, and the name round-trips to the exact
object it names.

Only values that are not addressable identifiers at all are rejected
(`ensure_identifier`):

| Rejected | Why |
| --- | --- |
| empty string | `""` is not a legal identifier |
| embedded NUL (`\x00`) | cannot cross the wire protocol |
| longer than 63 bytes (UTF-8) | PostgreSQL silently truncates, so `…_v1` and `…_v2` would become the same object |

The error reads `invalid <kind> name: …`.

## Plain-identifier-only sinks

Some tools deliberately keep the strict `[A-Za-z_][A-Za-z0-9_]*` rule, because
the name is not (only) spliced as a quoted SQL identifier:

| Sink | Modules | Why it stays strict |
| --- | --- | --- |
| ORM / schema exporters (`generate_prisma_schema`, Drizzle, SQLAlchemy, sqlc, Diesel, jOOQ, Ent, Ecto) | `prisma`, `drizzle`, `sqlalchemy_export`, `sqlc`, `diesel`, `jooq`, `ent`, `ecto` | The name becomes an identifier in **generated source code** (Rust, Go, Elixir, TypeScript, …), where a hyphen or space is not valid. |
| Apache AGE labels and graph projection | `graph`, `graph_projection` | Labels/graph names are catalog-derived and interpolated into generated Cypher / SQL. |
| SQL/PGQ property graphs | `pgq` | Schema and graph names feed a `CREATE PROPERTY GRAPH` statement. |
| NL→SQL schema selection and audit | `nl2sql`, `audit_nl2sql` | Kept strict so a schema name cannot smuggle text into the LLM prompt; reader-role / backend names go into DDL. |
| Text-search configuration | `textsearch` | The `regconfig` name is embedded as a single-quoted *string literal* (`to_tsvector('<config>', …)`); the plain rule is what keeps that literal from being closed. Table and column arguments *are* quoted. |
| Filesystem paths and database names for scheduled backups | `cron` | Reach a shell command line, so they use a path allowlist (`[A-Za-z0-9_./-]`) instead. |

A rejection from an exporter says so explicitly:

```
invalid schema name 'adm-pgbench'; this exporter only accepts plain identifiers
[A-Za-z_][A-Za-z0-9_]* because the name becomes a source identifier in generated
code. SQL tools (export_table, dump_database, …) accept delimited names via
quoting — see docs/identifier-policy.md.
```

If you need to export a schema whose name is not a plain identifier, use a SQL
tool (`export_table`, `dump_database`) for it, or rename the object.

## For contributors

- New SQL-identifier splices: use `quote_identifier` / `ensure_identifier`
  from `mcpg.identifiers`. Do not add another copy of the plain-identifier
  regex just to keep injection out — quoting already does that.
- Keep a plain-identifier check only when the name leaves SQL-identifier
  position (source code, a string literal, a shell argument). Say why in a
  comment and add the sink to the table above.
- Cover both the "delimited name works" and "hostile name is inert" cases;
  see `tests/unit/test_identifiers.py`.
