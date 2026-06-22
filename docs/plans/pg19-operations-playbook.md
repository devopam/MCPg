# PostgreSQL 19 operations playbook

Behavioural changes shipped in PostgreSQL 19 that operators of an
MCPg-managed cluster will want to know about — even when MCPg itself
isn't the surface that exposes them. Companion to
[`pg19-readiness.md`](pg19-readiness.md), which tracks the MCPg-side
tool work; this file is the **operator-side reference**.

Each entry follows the same shape: **what changed → who's affected →
what to do**. Cite the matching Phase 3 PR when MCPg already exposes
the new surface.

> **Scope.** This is the small-tools / docs-sweep deliverable from
> Phase 3 PR-12. It does not cover the headline features (SQL/PGQ,
> AIO, REPACK, etc.) — those have their own dedicated module docs
> reachable from `pg19-readiness.md`.

## Default-changing knobs

### JIT is off by default

**What changed.** PG 19 flips `jit = off` in the shipped
`postgresql.conf`. Pre-19, `jit = on` was the default — meaning
operators got JIT compilation for free on long-running plans whose
estimated cost exceeded `jit_above_cost`.

**Who's affected.** Workloads that had been silently benefitting
from JIT compilation. Upgrading clusters in place keep their
existing setting (the GUC value on disk wins over the new default);
fresh installs / cluster restores from `pg_dumpall` lose JIT until
the operator opts back in.

**What to do.**

- If your workload is OLAP-shaped (long-running plans, heavy filter /
  aggregate work), set `jit = on` explicitly in `postgresql.conf`
  and reload.
- For OLTP workloads JIT is a net loss — the off-by-default flip
  matches what most operators already configured by hand.
- Use `EXPLAIN (ANALYZE, BUFFERS)` to spot plans that were
  benefitting from JIT before. The `JIT:` block at the bottom of
  the output is the breadcrumb.

### LZ4 is the default TOAST compression

**What changed.** PG 19 ships `default_toast_compression = lz4`. The
prior default was `pglz`.

**Who's affected.** Fresh tables created on PG 19 use LZ4 for any
TOASTed columns. Existing tables keep their existing compression
setting on a per-column basis — until the operator runs
`VACUUM FULL` or the column is rewritten.

**What to do.**

- No action required for backward compatibility — LZ4 and pglz can
  coexist, and the planner reads either transparently.
- If you have a workload that was hand-tuned around pglz behaviour
  (rare), pin the prior default with
  `ALTER SYSTEM SET default_toast_compression = pglz;`.
- LZ4 generally produces smaller TOAST tables for typical text
  payloads. Expect a 5–15% disk-space win on heavy-text workloads.
- The MCPg `compare_schemas` tool surfaces compression settings; use
  it to spot drift between environments after the flip.

## Authentication changes

### RADIUS auth has been removed

**What changed.** PG 19 removed support for `pg_hba.conf` lines that
use `radius` as the auth method. Pre-19 setups with RADIUS users
will fail to authenticate after upgrade.

**Who's affected.** Any deployment whose `pg_hba.conf` contained a
`radius` line. Per the PG 19 release notes, RADIUS had been
deprecated for several releases; PG 19 finalises the removal.

**What to do.**

- Audit `pg_hba.conf` for `radius` entries before the upgrade.
  `grep -n radius /etc/postgresql/*/main/pg_hba.conf` is the
  one-liner.
- Replace with a supported method — `ldap`, `cert`, `scram-sha-256`,
  or the new OAuth flow (below) depending on what the RADIUS server
  was federating.
- MCPg's `verify_connection_encryption` doesn't assume RADIUS, so
  the tool surface itself is unaffected.

### OAuth in `pg_hba.conf`

**What changed.** PG 19 ships an `oauth` auth method for
`pg_hba.conf`. Lines like
`host all all 0.0.0.0/0 oauth issuer=https://auth.example.com/`
let an OAuth provider mint connection tokens. The token validator
runs in the backend.

**Who's affected.** Operators standardising on OAuth / OIDC for
human user authentication who want the database to validate tokens
directly (rather than via a connection pooler that brokers).

**What to do.**

- See the `pg_hba.conf` documentation for the per-issuer config
  required (issuer URL, JWKS endpoint, audience).
- Pair with `MCPG_AUTH_MODE=oidc` if you want both the MCPg server
  and the database to share an OAuth provider — see
  [`docs/security-hardening.md`](../security-hardening.md) for the
  MCPg-side flow.
- The MCPg `oidc` module's existing validator does not require any
  PG 19-specific changes; the cluster-side OAuth is independent.

### MD5 password auth deprecation warnings

**What changed.** PG 19 logs a `WARNING` at backend startup for
every connection authenticated via MD5. The auth method itself
keeps working — the warning is a documented heads-up for the
forthcoming PG 20 removal.

**Who's affected.** Clusters that still have any MD5-hashed entries
in `pg_authid.rolpassword`. SCRAM-SHA-256 has been the default
since PG 14, but role passwords created on a pre-14 cluster and
carried forward through pg_dump may still be MD5.

**What to do.**

- Run `SELECT rolname FROM pg_authid WHERE rolpassword LIKE 'md5%';`
  to list affected roles.
- Reset each role's password with `ALTER ROLE foo PASSWORD '…';`
  while connected to a server where `password_encryption =
  scram-sha-256` (the default since PG 14). The new password lands
  as SCRAM.
- The MCPg `audit_database` tool surface will be extended in a
  future PR to flag these automatically.

## Logging changes

### Per-process log levels

**What changed.** PG 19 introduces `log_min_messages` overrides on
a per-backend / per-process-class basis. An operator can set
`log_min_messages = info` globally but lower it to `debug2` just
for autovacuum workers, or just for parallel workers, etc.

**Who's affected.** Anyone tuning log verbosity for specific worker
classes — typically Sam-the-SRE looking for "noisy autovacuum
without a fire-hose of every NOTICE in the cluster".

**What to do.**

- The new GUCs follow the pattern
  `log_min_messages.<process_class>` — see the PG 19 docs for the
  full list of classes.
- For dev / staging clusters, set
  `log_min_messages.autovacuum = debug1` to capture autovacuum's
  decision-making without raising the global noise floor.
- The MCPg `obs_logging` module already parses standard PG
  log-line shapes; per-process levels affect verbosity, not the
  line format, so no MCPg-side change is required.

## JSONpath additions

**What changed.** PG 19 adds new string functions to the SQL/JSON
path language: `lower`, `upper`, `initcap`, `replace`, `split_part`,
`trim`. These are inline within `jsonb_path_*` calls — e.g.
`jsonb_path_query(j, '$.name.lower()')`.

**Who's affected.** Workloads that do meaningful string
normalisation inside jsonpath expressions — RAG pipelines indexing
case-insensitive lookups by `jsonb_path_query`, for instance.

**What to do.**

- Pre-19, the workaround was to extract the JSON value and apply
  the SQL function: `lower((j ->> 'name'))`. That still works on
  PG 19 — the new inline functions are additive.
- On PG 19 the inline form lets the planner push the predicate down
  into the index when an expression index exists, so it's a
  noticeable win for indexed-jsonb workloads.
- The MCPg `nl2sql` translator will gain emission patterns for
  these in a future PR (#18 in the Phase 2 audit).

## Reference

- PostgreSQL 19 Beta 1 release notes:
  <https://www.postgresql.org/about/news/postgresql-19-beta-1-released-3313/>
- Tracking issue: #120
- Phase 3 PR map: [`pg19-readiness.md`](pg19-readiness.md)
