# MCPg v0.3.0 — Catalog completeness, visualisation, and diff

**Released:** 2026-05-23 · 7 PRs since 0.2.0 · 45 MCP tools (was 33) ·
PostgreSQL 14 / 15 / 16 / 17 in CI

v0.3.0 closes **Batch A** of the post-0.2.0 roadmap: an agent can now
see every shape of object PostgreSQL exposes, render the schema as a
diagram, and diff it against another schema. The diff is also the
structural foundation Phase 27 (shadow migrations) will build on.

## Headlines

### 1. Twelve new introspection and visualisation tools

| Group | Tool | What it does |
| --- | --- | --- |
| **Custom types** | `list_enums` | Enum types in a schema with labels in sort order |
| | `list_domains` | Domains with base type, default, and check constraints |
| | `list_composite_types` | Standalone composite types with attributes |
| **Foreign data** | `list_foreign_data_wrappers` | Installed FDWs (handler/validator/options) |
| | `list_foreign_servers` | Foreign servers + their wrappers + options |
| | `list_foreign_tables` | Foreign tables in a schema |
| | `list_user_mappings` | Role-to-server mappings; `PUBLIC` surfaces as `user="public"` |
| **Logical replication** | `list_publications` | Publications + per-pub operations + qualified table list |
| | `list_subscriptions` | Subscriptions (superuser-gated, by PG design) |
| **Structural relations** | `list_foreign_keys` | FKs resolved to columns, aligned by ordinal position |
| **Visualisation** | `generate_schema_diagram` | Mermaid ER diagram (PK/FK markers, parent→child edges) |
| **Diff** | `compare_schemas` | Typed structural diff between two schemas |

### 2. Mermaid ER diagrams

`generate_schema_diagram` returns a Mermaid `erDiagram` block an agent
can paste into any Mermaid-aware renderer (GitHub, Mermaid Live, IDEs).
Entities carry PK/FK column markers; edges point from referenced parent
to referencing child. Views and foreign tables are excluded; partitions
are excluded by default (toggle with `include_partitions=true`).
Cross-schema FKs and edges to filtered-out tables are skipped rather
than left dangling.

### 3. Structural schema diff

`compare_schemas(left_schema, right_schema)` returns a typed diff:
tables added / removed / changed, and per-changed-table the same
trichotomy for columns, indexes, constraints, and foreign keys. Column
changes carry a `fields_changed` list of differing `ColumnInfo` fields,
so reviewers can see exactly what shifted. Identity is by name —
renames surface as a paired add + remove (no guessing). All list
orderings are stable (alphabetical) so repeated runs produce identical
output. This is the structural piece Phase 27 shadow migrations need.

### 4. Trust improvements

- **`postgres_fdw` added to the enableable-extensions allowlist** —
  agents can now install the wrapper they can already introspect
  (gated on unrestricted mode + `MCPG_ALLOW_DDL`).
- **"Every tool is callable" wiring check moved to integration** — the
  unit-level fake-driver version was replaced with a real-PG smoke test
  that runs across the PG 14–17 CI matrix. The fakes still earn their
  keep for catalog quirks (NULL elements in `text[]`), error injection,
  and DB-independent contract testing; but every tool's end-to-end
  wiring is now proven against actual catalog data.
- **Tool-count drift corrected** — `docs/PROGRESS.md` had been undercounting
  since Phase 16; v0.3.0 audits and restates the surface as **45 tools**.

## Roadmap context

The post-0.2.0 roadmap (`PLAN.md` §11) groups twelve themes into six
deliverable batches:

- **Batch A** — Catalog / visualisation / diff. ✅ **Done in 0.3.0.**
- **Batch B** — Advisors / lint + audit trail.
- **Batch C** — `pg_cron` / `pg_partman` / pgvector tuning.
- **Batch D** — Data movement (CSV/JSON + `pg_dump`/`pg_restore`). _ADR-0004 first._
- **Batch E** — Logical replication management + `LISTEN`/`NOTIFY` bridge. _ADR-0005 first._
- **Batch F** — Migrations with shadow workflow. _ADR-0006 first; depends on the v0.3.0 schema diff._
- **Batch G — ORM bridges (USP)** — `generate_prisma_schema` then
  sibling Drizzle / SQLAlchemy / sqlc exporters. No other PG MCP server
  bridges to an ORM DSL today.

## Upgrade notes

- **No breaking changes.** Tool names are stable; dataclass field
  names are additive only.
- **Settings are stable.** No new required env variables; the existing
  `MCPG_DATABASE_URL` / `MCPG_ACCESS_MODE` / `MCPG_ALLOW_DDL` knobs
  cover the new tools (`postgres_fdw` enable lives behind the existing
  `MCPG_ALLOW_DDL` gate).
- **Wire protocol** is unchanged; existing clients keep working.

## Acknowledgements

Sourcery reviewed every PR (Sourcery findings closed PRs #4 and #5).
Pre-commit hooks, ruff, mypy `--strict`, the 90%-coverage gate, and
the PG 14–17 service-container matrix ran on every change.
