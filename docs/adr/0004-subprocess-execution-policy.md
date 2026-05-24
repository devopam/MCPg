# ADR-0004: Subprocess execution policy for data movement

- **Status:** accepted
- **Date:** 2026-05-24

## Context

Batch D (Phase 24) needs `pg_dump` / `pg_restore` wrappers plus `export_query`
/ `export_table` and `import_csv`/`import_json` tools. The first three almost
certainly mean shelling out to the PostgreSQL `pg_dump` / `pg_restore` binaries
— there is no in-process equivalent that produces wire-compatible dumps.

MCPg has never executed external processes before. The full file/network
attack surface is currently empty — adding subprocess execution is the single
biggest security expansion since v0.1.0. The wire protocol passes user input
straight from an agent's tool call into MCPg, so any path that interpolates
agent-controlled strings into a command line is a remote-execution vulnerability
waiting to happen.

This ADR specifies how MCPg will gate, sandbox, and audit subprocess
execution before Batch D's first commit lands.

## Options considered

1. **No subprocess: emulate `pg_dump` in Python.** Possible for trivial
   `COPY ... TO` exports but cannot reproduce `pg_dump`'s catalog logic,
   custom-format binary output, or `--schema-only` shape that downstream
   tooling expects. Rejected — Batch D's stated value is "what the Postgres
   ecosystem already understands".
2. **Subprocess via `shell=True` for ergonomics.** Trivially exploitable;
   one space-containing identifier and the agent owns the host. Rejected.
3. **Subprocess via `asyncio.create_subprocess_exec(*argv)`** with an
   allowlisted binary set, an explicit argv list (never a shell string), a
   hard timeout, an output-byte cap, and a global opt-in setting.
4. **Subprocess inside a container** — strongest isolation, but ships
   container infrastructure and dependencies along with MCPg, which is
   currently a pure-Python distribution. Rejected for v1; revisit if Batch D
   matures into a hosted service.

## Decision

**Option 3.** Subprocess execution is added behind a new `MCPG_ALLOW_SHELL`
env var that **defaults to false**. When enabled, MCPg may invoke
binaries from a fixed allowlist via `asyncio.create_subprocess_exec` only,
with the following guarantees:

- `MCPG_ALLOW_SHELL` is required in addition to `unrestricted` access mode and
  the existing `MCPG_ALLOW_DDL` setting; subprocess wrappers register only
  when **all three** are on. A `Capability.SHELL` enum entry will live next
  to `Capability.DDL` so the policy table is the single source of truth.
- The allowlist is `{"pg_dump", "pg_restore", "psql"}`. No others. Binary
  lookup uses `shutil.which()` against `$PATH`; the resolved absolute path is
  what gets exec'd. A missing binary surfaces a clear error.
- Arguments are always passed as a list (`subprocess_exec(binary, *argv)`).
  Identifiers (schema, table) interpolated into argv are checked against the
  existing `[A-Za-z_][A-Za-z0-9_]*` allowlist used by `mcpg.textsearch._quoted`
  / `mcpg.prisma._check_identifier`. Connection strings are constructed via
  libpq's standard env-var path (`PGHOST`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`)
  with credentials passed in the environment, not the command line, so they
  never appear in `ps`.
- Every invocation is wrapped with a hard timeout (default 60s, configurable
  via `MCPG_SHELL_TIMEOUT_SEC`) and a cumulative stdout cap (default
  64 MiB, configurable via `MCPG_SHELL_MAX_OUTPUT_BYTES`). Output past the
  cap is dropped and the result flags `output_truncated=true`.
- When `MCPG_AUDIT_PERSIST` is on, every subprocess invocation appends to
  `mcpg_audit.events`: tool, argv (with credential env-vars redacted), exit
  code, stderr tail, output byte count. This reuses the recursive `_redact`
  helper shipped in Phase 21.
- The new tools (`dump_database`, `restore_database`, `export_query`,
  `export_table`, `copy_table_between_databases`) all share a single
  `subprocess_runner` helper so the policy lives in one place.

## Consequences

What becomes easier:

- `pg_dump`-shaped exports become a first-class tool, agent-driven.
- The new `Capability.SHELL` slot generalises — future tools that need a
  binary (PostGIS `shp2pgsql`, `pg_basebackup`) just add to the allowlist.

What becomes harder:

- A new attack surface; the policy above must hold or MCPg becomes RCE-as-a-
  service. Every new binary or argument MUST go through this policy.
- The CI matrix has to ensure `pg_dump`/`pg_restore` are installed in the
  test image; `pgvector/pgvector:pgN` already includes them.
- Imports (`import_csv`/`import_json`) read agent-supplied data; the file
  argument must be an in-memory buffer streamed to `psycopg`'s `COPY FROM
  STDIN`, never a host filesystem path. Out of scope for Batch D's first cut;
  flagged as a follow-on.

Follow-ups:

- Define the test strategy for subprocess error paths (timeout, oversize
  output, missing binary, non-zero exit) — likely a thin wrapper test fixture
  that swaps `asyncio.create_subprocess_exec` for a fake.
- Decide whether `psql` belongs in the allowlist long-term; included for
  symmetry but the SQL surface is already covered by `run_select`/`run_write`.
