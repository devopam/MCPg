# MCPg v0.6.7 — release notes

**Released:** 2026-07-03
**Tool surface:** **252** tools across 19 capability buckets
**Tests:** 2741 pass (PG 14 / 15 / 16 / 17 / 18 / 19 / WarehousePG)
**Runtime:** Python 3.14

This is a **patch-level bump (0.6.6 → 0.6.7)** focused on the client
experience: the safety classification now reaches the wire, a curated
demo dataset gives new users a rich first five minutes, and the runtime
moves to Python 3.14. No tool signatures changed; everything is
backward-compatible.

## Headline: MCPg's safety model is now on the wire

Every one of the 252 tools now publishes **MCP `ToolAnnotations`**:

- **`readOnlyHint`** — `true` for the 185 read tools, `false` for the
  67 write-capable ones. Derived mechanically from the same
  READ / WRITE / DDL / SHELL / LISTEN capability gates that enforce
  access, so a tool moved between gates can never ship a stale hint.
  A contract test pins the derivation exhaustively: the hinted
  read-only set must equal the tool surface actually reachable in
  read-only access mode.
- **`openWorldHint`** — `false` everywhere except `translate_nl_to_sql`
  (the one tool that calls an external LLM API); everything else talks
  only to the connected PostgreSQL server.
- `destructiveHint` is deliberately left unset for write-capable tools:
  the MCP default (true) is the cautious reading.

Clients like Claude Desktop use these hints to decide which calls to
auto-approve — until now MCPg's "safe by default" story was internal;
now it's visible to every MCP client. The 3 MCP prompts' arguments all
carry wire-visible descriptions too.

## Try MCPg in two minutes: the demo dataset

```bash
MCPG_DATABASE_URL=postgresql://... mcpg --demo       # seed
MCPG_DATABASE_URL=postgresql://... mcpg --demo-drop  # remove
```

`mcpg --demo` seeds a small, deterministic, **curated** e-commerce
dataset (400 customers, 120 products, 3,000 orders, 900 reviews) into
an `mcpg_demo` schema — engineered so the pivotal tools all have
something real to find on first contact: an un-indexed foreign key for
`analyze_query_plan` / `recommend_indexes`, PII-shaped columns for
`find_sensitive_columns`, a camelCase naming violation, searchable
review prose, and an optional pgvector embedding column when the
extension is installed. The seed is a single transaction, re-seeding
refuses rather than clobbers, and `--demo-drop` only removes a schema
carrying the MCPg ownership marker.

The companion [guided tour](demo.md) is *captured, not written* — every
output block is a real tool run against the seeded dataset, and
integration tests pin the planted findings so the walkthrough can't
silently rot.

## Also in this release

- **Runtime moves to Python 3.14** — Docker image, the full CI matrix,
  and the publish pipeline. `requires-python` stays `>=3.12`;
  installing on 3.12/3.13 is unchanged.
- **Startup warning noise eliminated** — the wall of benign pydantic
  "Field name `schema` shadows an attribute in parent BaseModel"
  warnings on first `tools/list` is suppressed (narrowly, by message
  pattern), with a contract test guarding the full tool surface.
- **Release-pipeline hardening** — the TestPyPI smoke test now polls
  the PEP 503 simple index (what `pip install` actually reads) instead
  of the JSON API, eliminating the propagation race that required a
  manual re-run during the v0.6.6 release.

## Upgrade

```bash
pip install --upgrade mcpg
docker pull ghcr.io/devopam/mcpg:0.6.7   # or :latest
```

No configuration changes required.

## Full changelog

See [`../CHANGELOG.md`](../CHANGELOG.md) `[0.6.7]` for the complete
itemised list.
