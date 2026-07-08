"""SQL-safety **mechanism** — parse a query and walk it against the allowlist.

:class:`SafeSqlDriver` wraps any :class:`~mcpg.sql.driver.SqlDriver` and only
lets read-only statements (SELECT / EXPLAIN / SHOW / VACUUM / ANALYZE /
cursor + prepared-statement management / allowlisted CREATE EXTENSION)
through, after a deep ``pglast`` AST validation. Every other statement type
(DDL, DML, multi-statement, disallowed function, unknown node) is rejected
before it reaches the database.

The *policy* — the permitted statement / node / function / extension sets —
lives in :mod:`mcpg.sql.allowlist` (data). This module is the *mechanism*
that reads that policy; it can't widen it. Re-authored from the vendored
``crystaldba/postgres-mcp`` ``safe_sql.py`` (MIT); behaviour is pinned
identical by the adversarial suite + differential parity test.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, ClassVar, LiteralString

import pglast
from pglast.ast import (
    A_Const,
    A_Expr,
    CreateExtensionStmt,
    DefElem,
    ExplainStmt,
    FuncCall,
    Node,
    RawStmt,
    SelectStmt,
)
from pglast.enums import A_Expr_Kind
from psycopg.sql import SQL, Composable, Literal

from mcpg.sql.allowlist import (
    ALLOWED_EXTENSIONS,
    ALLOWED_FUNCTIONS,
    ALLOWED_NODE_TYPES,
    ALLOWED_STMT_TYPES,
    LIKE_PATTERN,
    PG_CATALOG_PATTERN,
)
from mcpg.sql.driver import SqlDriver

logger = logging.getLogger(__name__)


class SafeSqlDriver(SqlDriver):
    """A ``SqlDriver`` wrapper that only permits read-only statements.

    Uses ``pglast`` to parse and deep-validate each statement against the
    :mod:`mcpg.sql.allowlist` policy before execution. DDL / DML and any
    disallowed node or function are rejected.
    """

    # Policy lives in mcpg.sql.allowlist; these class attributes alias it so
    # both ``self.X`` (the walker) and ``SafeSqlDriver.X`` (callers) resolve
    # to the single auditable data table.
    PG_CATALOG_PATTERN: ClassVar[re.Pattern[str]] = PG_CATALOG_PATTERN
    LIKE_PATTERN: ClassVar[re.Pattern[str]] = LIKE_PATTERN
    ALLOWED_STMT_TYPES: ClassVar[set[type]] = ALLOWED_STMT_TYPES
    ALLOWED_FUNCTIONS: ClassVar[set[str]] = ALLOWED_FUNCTIONS
    ALLOWED_NODE_TYPES: ClassVar[set[type]] = ALLOWED_NODE_TYPES
    ALLOWED_EXTENSIONS: ClassVar[set[str]] = ALLOWED_EXTENSIONS

    def __init__(self, sql_driver: SqlDriver, timeout: float | None = None) -> None:
        """Wrap ``sql_driver``; ``timeout`` (seconds) bounds each execution."""
        self.sql_driver = sql_driver
        self.timeout = timeout

    def _validate_node(self, node: Node) -> None:
        """Recursively validate a node and all of its children."""
        if not isinstance(node, tuple(self.ALLOWED_NODE_TYPES)):
            raise ValueError(f"Node type {type(node)} is not allowed")

        # LIKE / ILIKE patterns must be constant strings.
        if isinstance(node, A_Expr) and node.kind in (
            A_Expr_Kind.AEXPR_LIKE,
            A_Expr_Kind.AEXPR_ILIKE,
        ):
            if (
                isinstance(node.rexpr, A_Const)
                and node.rexpr.val is not None
                and hasattr(node.rexpr.val, "sval")
                and node.rexpr.val.sval is not None
            ):
                pass  # constant string pattern — allowed
            else:
                raise ValueError("LIKE pattern must be a constant string")

        # Function calls must be on the allowlist.
        if isinstance(node, FuncCall):
            func_name = ".".join([str(n.sval) for n in node.funcname]).lower() if node.funcname else ""
            match = self.PG_CATALOG_PATTERN.match(func_name)  # strip pg_catalog. prefix
            unqualified_name = match.group(1) if match else func_name
            if unqualified_name not in self.ALLOWED_FUNCTIONS:
                raise ValueError(f"Function {func_name} is not allowed")

        # No locking clauses on SELECT (SELECT ... FOR UPDATE etc.).
        if isinstance(node, SelectStmt) and getattr(node, "lockingClause", None):
            raise ValueError("Locking clause on select is prohibited")

        # No EXPLAIN ANALYZE (it would execute the query).
        if isinstance(node, ExplainStmt):
            for option in node.options or []:
                if isinstance(option, DefElem) and option.defname == "analyze":
                    raise ValueError("EXPLAIN ANALYZE is not supported")

        # CREATE EXTENSION only for allowlisted extensions.
        if isinstance(node, CreateExtensionStmt):
            if node.extname not in self.ALLOWED_EXTENSIONS:
                raise ValueError(f"CREATE EXTENSION {node.extname} is not supported")

        # Recurse into every child node.
        for attr_name in node.__slots__:
            if attr_name.startswith("_"):
                continue
            try:
                attr = getattr(node, attr_name)
            except AttributeError:
                continue  # normal in pglast

            if isinstance(attr, list):
                for item in attr:
                    if isinstance(item, Node):
                        self._validate_node(item)
            elif isinstance(attr, tuple):
                for item in attr:
                    if isinstance(item, Node):
                        self._validate_node(item)
            elif isinstance(attr, Node):
                self._validate_node(attr)

    def _validate(self, query: str) -> None:
        """Parse ``query`` and validate every statement is safe to execute."""
        try:
            parsed = pglast.parse_sql(query)
            try:
                for stmt in parsed:
                    if isinstance(stmt, RawStmt):
                        if not isinstance(stmt.stmt, tuple(self.ALLOWED_STMT_TYPES)):
                            raise ValueError(
                                "Only SELECT, ANALYZE, VACUUM, EXPLAIN, SHOW and other read-only "
                                "statements are allowed. Received raw statement: " + str(stmt.stmt)
                            )
                    else:
                        if not isinstance(stmt, tuple(self.ALLOWED_STMT_TYPES)):
                            raise ValueError(
                                "Only SELECT, ANALYZE, VACUUM, EXPLAIN, SHOW and other read-only "
                                "statements are allowed. Received: " + str(stmt)
                            )
                    self._validate_node(stmt)
            except Exception as e:
                raise ValueError(f"Error validating query: {query}") from e
        except pglast.parser.ParseError as e:
            raise ValueError("Failed to parse SQL statement") from e

    async def execute_query(
        self,
        query: LiteralString,
        params: list[Any] | None = None,
        force_readonly: bool = True,  # ignored — SafeSqlDriver always forces read-only
    ) -> list[SqlDriver.RowResult] | None:
        """Validate ``query`` is safe, then execute it read-only."""
        self._validate(query)

        # Always force read-only regardless of the argument.
        if self.timeout:
            try:
                async with asyncio.timeout(self.timeout):
                    return await self.sql_driver.execute_query(
                        f"/* crystaldba */ {query}",
                        params=params,
                        force_readonly=True,
                    )
            except TimeoutError as e:
                logger.warning("Query execution timed out after %s seconds: %s...", self.timeout, query[:100])
                raise ValueError(
                    f"Query execution timed out after {self.timeout} seconds in restricted mode. "
                    "Consider simplifying your query or increasing the timeout."
                ) from e
            except Exception as e:
                logger.error("Error executing query: %s", e)
                raise
        return await self.sql_driver.execute_query(
            f"/* crystaldba */ {query}",
            params=params,
            force_readonly=True,
        )

    @staticmethod
    def sql_to_query(sql: Composable) -> str:
        """Render a ``psycopg.sql`` composable to a query string."""
        return sql.as_string()

    @staticmethod
    def param_sql_to_query(query: str, params: list[Any]) -> str:
        """Interpolate ``params`` into ``query`` as ``psycopg`` literals."""
        sql_params = [p if isinstance(p, Composable) else Literal(p) for p in params]
        return SafeSqlDriver.sql_to_query(SQL(query).format(*sql_params))

    @staticmethod
    async def execute_param_query(
        sql_driver: SqlDriver, query: LiteralString, params: list[Any] | None = None
    ) -> list[SqlDriver.RowResult] | None:
        """Bind ``params`` then execute — a convenience over ``execute_query``."""
        if params:
            query_params = SafeSqlDriver.param_sql_to_query(query, params)
            return await sql_driver.execute_query(query_params)
        return await sql_driver.execute_query(query)
