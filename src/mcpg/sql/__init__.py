"""MCPg's first-party SQL kernel — connection pool, driver, safety allowlist.

Public surface (the seam the rest of ``mcpg`` depends on):

* :class:`SqlDriver` — execute-query adapter + ``RowResult``.
* :class:`DbConnPool` — async ``psycopg`` connection pool.
* :func:`obfuscate_password` — credential redaction.
* :class:`SafeSqlDriver` — ``pglast`` parse + default-deny node allowlist.

Re-authored from the formerly-vendored ``crystaldba/postgres-mcp`` kernel
(MIT); see ADR-0001's successor. Policy (:mod:`mcpg.sql.safety`) is kept
separate from I/O (:mod:`mcpg.sql.driver`).
"""

from __future__ import annotations

from mcpg.sql.driver import DbConnPool, SqlDriver, obfuscate_password

__all__ = [
    "DbConnPool",
    "SqlDriver",
    "obfuscate_password",
]
