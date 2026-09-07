# Identifier policy

This page is the canonical description of how MCPg treats schema, table,
column, role, and other names. Tool descriptions and error messages should
stay consistent with it so agents can choose tools and recover from rejections.

MCPg follows a **match the sink** rule: naming limits depend on where the
value is used, not on a single global "safe name" style.

## SQL and `pg_dump` / `pg_restore` paths

Tools that address real PostgreSQL objects (query, export, import, dump,
replication, roles used in `SET ROLE`, and similar) accept any **addressable**
PostgreSQL identifier. Values are encoded with `quote_identifier` (or dump
pattern encoding for `pg_dump`), so injection is prevented by **escaping**,
not by forbidding hyphens.

| Example | Notes |
|---------|--------|
| `adm-pgbench` | Hyphen — common real schema name (issue #329) |
| `My Schema` | Space |
| `Users` | Mixed case preserved only when quoted |
| `2fa_tokens` | Leading digit requires quoting in SQL |
| `app$cfg` | `$` is allowed in PostgreSQL unquoted identifiers; still fine when quoted |
| `données` | Non-ASCII letters |
| `order` / `user` | Reserved words — must be quoted as identifiers |
| `weird"name` | Embedded `"` is doubled to `""` inside quotes |

**Always rejected** (not valid addressable identifiers): empty string, embedded
NUL (`\x00`), names longer than **63 bytes** (PostgreSQL's `NAMEDATALEN - 1`;
the server would otherwise **silently truncate**).

Callers pass the **bare** name (e.g. `adm-pgbench`). Do not add SQL quotes
yourself.

Beyond the hyphen, the important cases are spaces, case preservation, leading
digits, `$`, Unicode letters, SQL reserved words used as names, and embedded
double quotes — all legal in PostgreSQL delimited identifiers when encoded
correctly.

## Plain-identifier-only tools (deliberate)

Some tools feed the name into a **different** language or channel. Those keep
a stricter allowlist, typically `[A-Za-z_][A-Za-z0-9_]*`:

| Sink | Examples | Why |
|------|----------|-----|
| Generated source | Prisma, Drizzle, Diesel, Ecto, Ent, jOOQ, sqlc, SQLAlchemy export | Name becomes an identifier in TypeScript / Go / Rust / Elixir / Python |
| Graph labels | Apache AGE / graph projection labels | Extension label rules, not PG delimited identifiers |
| Shell / host command | `schedule_logical_backup` paths, some `COPY TO PROGRAM` args | Shell metacharacters are a real injection surface |
| Some SQL *literals* | `regconfig` in text-search helpers | Embedded as a string literal, not as a delimited identifier |
| Config keys | `MCPG_SECONDARY_DATABASE_URLS` names | MCPg's own ids (`[a-z0-9_]+`), not PG object names |

If such a tool rejects a name that SQL tools accept, the error should state the
**sink** and point at SQL/dump tools for the real object name.

### Suggested error shape

```text
PrismaExportError: invalid schema name 'adm-pgbench'; this tool only accepts
plain identifiers [A-Za-z_][A-Za-z0-9_]* because the name becomes a source
identifier in generated Prisma schema. Use a plain-named schema, or map the
PostgreSQL name in your own layer. SQL tools (export_table, dump_database, …)
accept delimited names via quoting.
```

### Suggested tool-description blurb

```text
Names must be plain PostgreSQL identifiers: [A-Za-z_][A-Za-z0-9_]*.
Hyphens, spaces, and other delimited-identifier characters are not supported
in this tool because the value is used as <SINK>, not only as a SQL identifier.
See docs/identifier-policy.md.
```

## What agents should do

1. Prefer tool descriptions and error text over assumptions from the README alone.
2. On a plain-identifier rejection, retry with SQL-oriented tools (`export_table`,
   `dump_database`, `run_select`, …) using the same bare name.
3. Do not strip hyphens or rename production schemas solely to satisfy an ORM
   exporter — map names in the generator layer if you need both.

## README note

The main README **Why MCPg** bullet should not claim that all identifier
interpolation still uses a global `[A-Za-z_][A-Za-z0-9_]*` allowlist. That was
true historically; since 0.8.2 / issue #329, SQL and `pg_dump` paths use
encoding. Link here from the README when that bullet is updated.
