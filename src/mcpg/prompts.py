"""MCP prompts — pre-built interrogation playbooks for common DBA tasks.

Companion to :mod:`mcpg.resources` (which preloads catalog context) and
:mod:`mcpg.tools` (which exposes operations). Prompts package a
deterministic investigation flow so the agent doesn't have to
reconstruct one from scratch on every session.

Each builder returns a ready-to-render user-role message. The
``mcpg.tools._register_prompts`` registrar wires the functions onto
FastMCP via ``@server.prompt(...)`` so they appear in the standard
``prompts/list`` / ``prompts/get`` MCP protocol surface — clients
pick them up automatically.

Surface inventory:

* ``diagnose_slow_query(sql)`` — investigation plan for a single
  slow statement; routes through ``explain_query``,
  ``analyze_query_plan``, ``recommend_indexes``, then
  ``analyze_workload`` for the broader context.
* ``bisect_slow_migration(migration_id, baseline_schema, current_schema)``
  — narrows the cause of a migration that regressed performance;
  walks ``read_migration_history`` /
  ``list_unapplied_migration_scripts`` first, then
  ``compare_schemas(baseline, current)``, then targeted
  ``analyze_query_plan`` calls on the suspect tables.
* ``review_rls_policy(schema, table)`` — RLS coverage audit:
  ``describe_table`` for column inventory, ``list_policies`` for
  current rules, then ``audit_database`` for the broader
  security stance.

Design choices:

* **Prompts are name-addressed**, not URI-addressed (unlike
  resources). The protocol-level surface is ``prompts/list`` plus
  ``prompts/get`` — clients call by name.
* **Each prompt body lists the tools by canonical name** so an
  agent that follows the plan literally just works against the
  current MCPg surface.
* **No mutation suggested** — every prompt is a *diagnosis* flow.
  The agent + user decide together whether to follow up with a
  write tool.
"""

from __future__ import annotations

# Prompt bodies are user-facing markdown — each Step bullet is one
# logical line so renderers don't split paragraphs mid-sentence.
# Implicit string concatenation keeps source lines under the 120-char
# ruff limit without inserting hard line breaks the renderer would
# preserve.


def _build_diagnose_slow_query(sql: str) -> str:
    """Investigation plan for a single slow SQL statement.

    The body is plain Markdown so renderers that pretty-print user
    messages (Claude.ai, the MCP Inspector, etc.) format it cleanly.
    """
    return (
        "You are diagnosing a slow PostgreSQL query. Follow this investigation plan in order; "
        "report findings as you go and stop early if a step yields the diagnosis.\n\n"
        "**Statement under investigation:**\n\n"
        "```sql\n"
        f"{sql}\n"
        "```\n\n"
        "**Step 1 — Plan shape.** Call `explain_query` with the statement above. Read the plan "
        "top-down: note any `Seq Scan` on tables larger than ~10k rows, any `Sort` / `Hash` "
        "nodes spilling to disk, and any wide rows-returned vs rows-estimated gaps.\n\n"
        "**Step 2 — Runtime metrics.** Call `analyze_query_plan` with the same statement. This "
        "runs `EXPLAIN (ANALYZE, BUFFERS, TIMING)` server-side and surfaces per-node actual "
        "times + buffer reads. Compare `Buffers: shared read=...` (cold cache) against "
        "`shared hit=...` (warm cache) — high `read` on a query that should be cached signals "
        "working-set pressure.\n\n"
        "**Step 3 — Index opportunities.** Call `recommend_indexes` (workload-wide — it "
        "scans `pg_stat_user_tables` for high-seq-scan tables, no per-query argument). Filter "
        "the result to the tables this statement touches. For each candidate, validate by "
        "re-running `analyze_query_plan` after creating the suggested index on a shadow schema "
        "before applying to production.\n\n"
        "**Step 4 — Workload context.** Call `analyze_workload`. If the same statement appears "
        "in the top-N by total time, the fix is high-leverage; if it's a one-off, prioritise "
        "correctness over micro-optimisation.\n\n"
        "**Reporting.** After completing the plan, write up: (a) the most likely root cause "
        "(`seq_scan_on_large_table` / `missing_index` / `bad_statistics` / "
        "`function_in_where_clause` / `cross_join` / `unbounded_sort`), (b) the recommended "
        "fix (with exact SQL), and (c) the expected impact (rows scanned before vs after, "
        "plan node changes)."
    )


def _build_bisect_slow_migration(
    migration_id: str,
    baseline_schema: str,
    current_schema: str,
) -> str:
    """Narrow the cause of a migration that regressed performance.

    Uses MCPg's migration introspection plus schema-diff to scope the
    blast radius before falling back to per-query analysis.
    """
    return (
        "You are bisecting a performance regression introduced by a migration. Follow this "
        "plan in order; each step narrows the suspect surface.\n\n"
        f"- **Migration id:** `{migration_id}`\n"
        f"- **Baseline schema (pre-migration shape):** `{baseline_schema}`\n"
        f"- **Current schema (post-migration shape):** `{current_schema}`\n\n"
        f"**Step 1 — Confirm the migration ran.** Call `read_migration_history` and verify "
        f"`{migration_id}` is in the applied list. If it isn't, the regression is in an "
        "unapplied or partial migration — call `list_unapplied_migration_scripts` to surface "
        "the script body and stop here (the bug is in the rollout, not the schema).\n\n"
        "**Step 2 — Scope what changed.** Call `compare_schemas(source_schema="
        f"'{baseline_schema}', target_schema='{current_schema}')`. The diff returns tables "
        "added/dropped, columns added/dropped/altered, indexes added/dropped, and constraints "
        "changed. Focus on:\n\n"
        '  - **Dropped indexes** — the leading suspect. Even a "redundant" index drop '
        "changes plan shapes.\n"
        "  - **Altered column types** — narrowing widens, widening can break index opclasses.\n"
        "  - **Added foreign keys without an index on the child side** — every INSERT/UPDATE "
        "now triggers a sequential scan on the parent.\n"
        "  - **Changed `NOT NULL` / `DEFAULT`** — can invalidate planner statistics until the "
        "next ANALYZE.\n\n"
        "**Step 3 — Confirm via runtime metrics.** For each suspect table from Step 2, call "
        "`analyze_workload` and pick the slowest statement that touches it. Run "
        "`analyze_query_plan` against that statement on the current schema. Compare row "
        "estimates against actuals — if they diverge by >10x, the table needs `ANALYZE` to "
        "refresh statistics.\n\n"
        "**Step 4 — Decide the remediation.** Pick one:\n\n"
        "  - **Forward-fix** (preferred when the migration's intent was right but the index "
        "plan was wrong): add the missing index via `recommend_indexes`, re-run "
        "`analyze_query_plan` to confirm.\n"
        "  - **Rollback** (when the migration introduced semantic breakage): construct the "
        "inverse SQL by hand from the `compare_schemas` diff (added → drop, dropped → "
        "recreate from the baseline DDL, altered → revert), then validate the rollback "
        "against a shadow schema via `prepare_migration` + `validate_migration_schema` "
        "before applying.\n"
        "  - **No-op** (when the regression is real but expected): document via "
        "`prepare_migration` so the next migration carries forward the new floor.\n\n"
        "**Reporting.** Summarise: (a) which schema-diff entries are causally implicated, "
        "(b) the per-query before/after numbers from `analyze_query_plan`, and (c) the chosen "
        "remediation with its rollback path."
    )


def _build_review_rls_policy(schema: str, table: str) -> str:
    """RLS coverage audit for one table — surfaces gaps and missing policies."""
    return (
        f"You are reviewing row-level security (RLS) coverage on `{schema}.{table}`. Follow "
        "this audit plan in order; report findings as you go.\n\n"
        f"**Step 1 — Inventory the surface.** Call `describe_table(schema='{schema}', "
        f"table='{table}')`. Note: column count, whether any columns look identity-bearing "
        "(e.g. `tenant_id`, `org_id`, `owner_id`, `user_id`), and whether the table has a "
        "primary key. RLS on a PK-less table is feasible but rare and warrants comment.\n\n"
        f"**Step 2 — Read the current policies.** Call `list_policies(schema='{schema}', "
        f"table='{table}')`. For each policy, capture: `policyname`, `cmd` (`SELECT` / "
        "`INSERT` / `UPDATE` / `DELETE` / `ALL`), `roles`, `qual` (the `USING` clause), and "
        "`with_check`. If the list is empty AND RLS is enabled on the table, *every* read "
        "returns zero rows — that's almost always a bug.\n\n"
        "**Step 3 — Identify the gaps.** Cross-check Step 1's identity-bearing columns "
        "against Step 2's policy expressions:\n\n"
        "  - **Missing policies for a CRUD verb.** A table with a `SELECT` policy but no "
        "`UPDATE` policy is read-isolated but write-promiscuous.\n"
        "  - **Policies that don't reference the identity column.** A `tenant_id`-bearing "
        "table whose `USING` clause doesn't mention `tenant_id` is leaking across tenants.\n"
        "  - **Asymmetric `USING` vs `WITH CHECK`.** A row passing `USING` (visible) but "
        "failing `WITH CHECK` (forbidden write) can be a deliberate audit pattern, or a "
        "footgun — confirm with the user.\n"
        "  - **`roles = '{public}'`** — the policy applies to every role. Often intentional, "
        "but flag for the user.\n\n"
        f"**Step 4 — Cross-check the cluster posture.** Call `audit_database(schema='{schema}')`. "
        "Returns a multi-category report; focus on the security category for: tables with RLS "
        "enabled but no policies, tables with policies but RLS disabled (policies inert), "
        "superusers bypassing every policy (PG default).\n\n"
        "**Reporting.** Produce: (a) a per-policy summary (verb + role + intent), (b) an "
        'explicit "gaps" list with severity (`critical` / `warning` / `info`), and (c) for '
        "each critical gap, a proposed `CREATE POLICY` statement the user can review before "
        "applying. Stop short of applying — RLS changes belong behind explicit human approval."
    )


__all__ = [
    "_build_bisect_slow_migration",
    "_build_diagnose_slow_query",
    "_build_review_rls_policy",
]
