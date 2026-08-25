# Project Incubation Baseline

**Project:** MCPg
**Baseline created:** 2026-08-25
**Last audited:** 2026-08-25 (this invocation — baseline and audit run together, since no prior baseline existed)
**project-incubation skill version:** 0.2.0 (from `.claude-plugin/plugin.json` at incubation time)

## Project shape

- **Path:** software
- **Purpose:** A production-grade PostgreSQL Model Context Protocol (MCP) server — 254 MCP tools exposing
  database introspection, query execution, migrations, observability, and administration to LLM agents,
  with a first-party AST-validated SQL-safety kernel.
- **Team size at incubation:** solo / small team (single primary author + CODEOWNERS; user-confirmed)
- **Expected scale / lifespan:** production, long-lived (user-confirmed) — corroborated by the repo itself:
  PyPI/GHCR/MCP-registry/Smithery releases, an active CI/CD and release pipeline, and multiple dated
  security-review documents.
- **Compliance / regulatory constraints:** none stated in the repo (no PCI/HIPAA/GDPR references found in
  README, SECURITY.md, or docs/) — not independently verified beyond a repo-content search.

*Note: this baseline was written retroactively against an established repo, not at true inception — see
[Audit mode](#step-1-re-check-llmagent-component-status) results below for what was actually verified
against the existing codebase, since Phases 1–6 here are reconstructed from repo evidence and the user's
confirmation, not a live inception Q&A.*

## Stack category (software path only)

- **Primary category:** Agentic & MCP Platforms
- **Reasoning:** The project *is* an MCP server (the `mcp[cli]` SDK is a direct dependency, `server.py`
  implements the MCP protocol surface, `packaging/mcpb/` ships a Claude Desktop extension bundle,
  `publish-mcp-registry` in `publish.yml` registers it with the MCP Registry). User-confirmed.

## Architecture template (software path only)

- **Primary pattern:** Modular monolith — single deployable (`mcpg` package/process), ~100 source modules
  with clear internal boundaries, no service-boundary splits.
- **Overlays / composed elements:**
  - **Hexagonal-flavored core for the SQL-safety kernel** — `sql/allowlist.py` (policy-as-data) is
    explicitly separated from `sql/safety.py` (mechanism: the `pglast` parse/validate path) and
    `sql/driver.py` (pool/execution), per the project's own ADR-0007 and `CLAUDE.md`. This is a real
    ports/adapters boundary, not an incidental module split — the project's own documentation frames it
    that way.
  - **Hexagonal-flavored core for secrets** — `secrets.py`'s `SecretsProvider` protocol with five
    interchangeable backends (env/file/vault/aws/gcp) is a textbook swappable-adapter pattern behind one
    port.
  - **Event-driven overlay for LISTEN/NOTIFY** — `listen.py`'s pub-sub tool surface is the one genuinely
    asynchronous, decoupled-communication subdomain in an otherwise request/response tool-call system.
- **Reasoning:** Single small team + single deployable target (a process an MCP host spawns/connects to)
  rule out microservices outright — there's no multi-team release-cadence problem to solve. Domain
  complexity in the SQL-safety kernel specifically (an AST allowlist that must be provably correct, with
  its own fuzz-tested threat model) is exactly the "protect this from infrastructure churn" signal the
  decision framework names for applying hexagonal to *that* core, not the whole system. This matches the
  stack-category pairing table's own bias for Agentic & MCP Platforms ("hexagonal core... + event-driven
  overlay") closely, arrived at independently from the repo's own structure and ADRs rather than from that
  table.
- **ADR:** Not written as part of *this* skill invocation (the pattern was inferred from existing structure
  and ADRs, not decided fresh) — the project's own `docs/adr/0007-first-party-sql-kernel.md` is the closest
  existing document recording the hexagonal-core decision for the SQL kernel specifically.

## Common architecture principles applied

- **Principles doc version referenced:** `references/architecture-principles.md` as shipped in
  agent-skills v0.2.0.
- **LLM/agent component:** yes
  - **Basis:** asked directly (user-confirmed) — MCPg's NL→SQL feature (`nl2sql.py`) calls
    Anthropic/OpenAI/Gemini directly, and the project's entire purpose is serving an LLM agent as an MCP
    tool provider.
  - **The LLM-conditional principles section applies.** Spot-checked against the codebase in this same
    audit pass — see Step 3 below.
- **Notable deviations from the standard principle set:** none identified as a hard deviation; see Step 3
  findings for gaps found (not deviations by design).

## Preferred libraries snapshot (software path only)

- **Category reference used:** `references/preferred-libraries/agentic-mcp-platforms.md`
- **Snapshot date at incubation:** 2026-08-25 (this audit — the reference doc's own "last reviewed" dates
  per entry are checked in Step 4 below)
- **Key library choices (from the repo, not prescribed by this baseline):** `mcp[cli]` (MCP SDK),
  `psycopg[binary]` + `psycopg-pool` (async Postgres driver/pool), `pglast` (SQL AST parsing — the
  safety-kernel's core dependency), `pyjwt[crypto]` (OIDC/JWT), `httpx` (LLM provider + OIDC discovery
  calls), `hatchling` (build backend), `ruff`/`mypy --strict`/`bandit`/`pip-audit` (quality/security
  tooling).

## License

- **Chosen license:** MIT
- **Reasoning:** Already the repo's license (`LICENSE`, `pyproject.toml` `license = "MIT"`) — permissive,
  matches the MIT-licensed upstream MCPg forked from (per ADR-0001), no reasoning re-derived here since the
  choice predates this baseline.

## Drift log

- 2026-08-25: Baseline created retroactively (no prior `docs/project-incubation-baseline.md` existed) and
  first audit run in the same invocation. Findings from that audit (structure gaps: no root
  `CODE_OF_CONDUCT.md`/`.editorconfig`; ~20+ exception classes with no shared base, a Maintainability/DRY
  finding also raised independently by the same-day `python-code-review` run; NL→SQL provenance gap —
  `TranslationResult` records which model/provider produced a translation but not the schema-context
  evidence it saw; preferred-libraries snapshot confirmed current, 5 days old at audit time) were reported
  to the user in conversation, not duplicated into this file. No fixes were auto-applied per this skill's
  "never bulk-apply" rule — re-run the audit after any of them are addressed to update this log.
