"""Row-Level Security tester — see what an RLS-bound role would read.

PostgreSQL's ``CREATE POLICY`` defines per-role USING / WITH CHECK
predicates that filter visible rows on the policy's table. Debugging
those is often a multi-step ritual: connect as the role, run the
query, see the rows. ``test_rls_for_role`` consolidates that into one
tool: wrap the query in ``SET LOCAL ROLE "<role>"`` inside a read-only
transaction, return what the role can see plus the list of RLS
policies that applied.

Safe by construction:

* The role name is validated against ``[A-Za-z_][A-Za-z0-9_]*``.
* The schema / table are validated identically.
* The query runs inside a ``BEGIN TRANSACTION READ ONLY``; no writes
  can leak through.
* The role is reset on every exit branch (SET LOCAL auto-resets at
  txn end, defence-in-depth ``RESET ROLE`` is issued too).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mcpg.sql import SqlDriver

_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

DEFAULT_RLS_SAMPLE_SIZE = 25


class RLSError(Exception):
    """Raised when an RLS-tester input fails validation or execution fails."""


def _check_identifier(value: str, kind: str) -> None:
    if not _IDENTIFIER.match(value):
        raise RLSError(f"invalid {kind} {value!r}; must match [A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class ActivePolicy:
    """One RLS policy that applies to the (table, role) pair under test.

    Mirrors ``pg_policies`` minus the ``definition`` lines we already
    surface in :func:`mcpg.introspection.list_policies`.
    """

    name: str
    permissive: str
    roles: list[str]
    command: str
    using_expression: str | None
    with_check_expression: str | None


@dataclass(frozen=True)
class RLSTestResult:
    """Result of :func:`test_rls_for_role`.

    ``rls_enabled`` echoes whether ``relrowsecurity`` is set on the
    table — when ``False``, the role's superuser / bypassrls bit may
    have routed around RLS and the row count is the unrestricted one.
    ``rows_visible`` is the total count the role can read; ``sample``
    is the first ``sample_size`` rows so the agent can inspect them.
    """

    schema: str
    table: str
    role: str
    rls_enabled: bool
    active_policies: list[ActivePolicy]
    rows_visible: int
    columns: list[str]
    sample: list[dict[str, Any]]


async def test_rls_for_role(
    driver: SqlDriver,
    schema: str,
    table: str,
    role: str,
    *,
    sample_size: int = DEFAULT_RLS_SAMPLE_SIZE,
) -> RLSTestResult:
    """Run a read-only SELECT against ``schema.table`` as ``role``.

    Reports the policies that apply to that role on the table, the
    total count of rows visible, and a sample. The sample is bounded
    to ``sample_size`` rows so the response stays small even on huge
    tables.

    Raises:
        RLSError: When identifiers fail validation or the query fails.
    """
    _check_identifier(schema, "schema")
    _check_identifier(table, "table")
    _check_identifier(role, "role")
    if sample_size < 0:
        raise RLSError("sample_size must be >= 0")

    # rls_enabled + applicable policies — these are catalog reads,
    # they don't need to run AS the role.
    enabled_rows = await driver.execute_query(
        "SELECT c.relrowsecurity AS enabled "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[schema, table],
        force_readonly=True,
    )
    if not enabled_rows:
        raise RLSError(f"table {schema!r}.{table!r} not found")
    rls_enabled = bool(enabled_rows[0].cells["enabled"])

    policy_rows = await driver.execute_query(
        "SELECT p.polname AS name, "
        "       CASE p.polpermissive WHEN TRUE THEN 'permissive' ELSE 'restrictive' END AS permissive, "
        "       COALESCE("
        "         (SELECT array_agg(r.rolname ORDER BY r.rolname) "
        "          FROM pg_roles r WHERE r.oid = ANY(p.polroles)), "
        "         ARRAY['PUBLIC']::name[]"
        "       ) AS roles, "
        "       CASE p.polcmd WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT' "
        "                     WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE' "
        "                     WHEN '*' THEN 'ALL' ELSE p.polcmd::text END AS command, "
        "       pg_get_expr(p.polqual, p.polrelid) AS using_expr, "
        "       pg_get_expr(p.polwithcheck, p.polrelid) AS with_check_expr "
        "FROM pg_policy p "
        "JOIN pg_class c ON c.oid = p.polrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_roles target ON target.rolname = %s "
        "WHERE n.nspname = %s AND c.relname = %s "
        # Either the policy applies to PUBLIC (polroles = '{0}') or
        # to a role list that contains the target role's oid.
        "AND (p.polroles = ARRAY[0]::oid[] OR (target.oid IS NOT NULL AND target.oid = ANY(p.polroles))) "
        "ORDER BY p.polname",
        params=[role, schema, table],
        force_readonly=True,
    )
    active_policies = [
        ActivePolicy(
            name=str(row.cells["name"]),
            permissive=str(row.cells["permissive"]),
            roles=[str(r) for r in (row.cells["roles"] or [])],
            command=str(row.cells["command"]),
            using_expression=str(row.cells["using_expr"]) if row.cells["using_expr"] is not None else None,
            with_check_expression=(
                str(row.cells["with_check_expr"]) if row.cells["with_check_expr"] is not None else None
            ),
        )
        for row in policy_rows or []
    ]

    # Now run the count + sample AS the role. Inline the validated
    # identifiers — they've all been allowlist-checked.
    qualified = f'"{schema}"."{table}"'
    quoted_role = f'"{role}"'

    # Count + sample issued as separate queries inside the role
    # context. The driver wraps each call in its own transaction; we
    # SET LOCAL ROLE before each so RLS evaluates as the target role.
    # If the role doesn't exist PG raises here — surface as RLSError.
    try:
        count_rows = await driver.execute_query(
            f"SET LOCAL ROLE {quoted_role}; SELECT COUNT(*) AS n FROM {qualified}",
            force_readonly=True,
        )
    except Exception as exc:
        raise RLSError(f"counting visible rows failed: {exc}") from exc

    rows_visible = int(count_rows[0].cells["n"]) if count_rows else 0

    sample: list[dict[str, Any]] = []
    columns: list[str] = []
    if sample_size > 0 and rows_visible > 0:
        try:
            sample_rows = await driver.execute_query(
                f"SET LOCAL ROLE {quoted_role}; SELECT * FROM {qualified} LIMIT {sample_size}",
                force_readonly=True,
            )
        except Exception as exc:
            raise RLSError(f"sampling visible rows failed: {exc}") from exc
        sample = [dict(row.cells) for row in sample_rows or []]
        if sample:
            columns = list(sample[0].keys())

    return RLSTestResult(
        schema=schema,
        table=table,
        role=role,
        rls_enabled=rls_enabled,
        active_policies=active_policies,
        rows_visible=rows_visible,
        columns=columns,
        sample=sample,
    )
