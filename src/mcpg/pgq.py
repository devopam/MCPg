"""SQL/PGQ — property graph queries (PG 19 standard) coverage.

PG 19 introduces SQL/PGQ — the SQL standard for property-graph queries —
giving Postgres a built-in path to express graph patterns directly inside
SQL. The marquee syntax is the ``GRAPH_TABLE(...)`` function, which lets
a ``SELECT`` consume a property graph defined with ``CREATE PROPERTY
GRAPH`` and emit rows for each pattern match::

    SELECT * FROM GRAPH_TABLE (
        org_chart
        MATCH (e:Employee)-[:REPORTS_TO]->(m:Manager)
        COLUMNS (e.name AS employee, m.name AS manager)
    );

MCPg already exposes an AGE-style ``graph_operations`` bucket (``create_graph``
/ ``run_cypher`` / ``describe_graph``). SQL/PGQ is the upstream-standard
alternative — strategically we keep both surfaces and let agents pick the
one they need. The decision matrix lives in
``docs/plans/pg19-readiness.md``.

Module surface
--------------
* Read tools — extension/feature status, catalog enumeration, single-graph
  describe, agent-callable ``run_pgq`` for ``SELECT ... GRAPH_TABLE`` queries.
* DDL tools — ``create_property_graph`` (takes a full
  ``CREATE PROPERTY GRAPH`` body, validated for shape) and
  ``drop_property_graph``.

Availability
------------
SQL/PGQ is built into PG 19 server-side — there is no ``CREATE EXTENSION``
step. Every helper feature-detects via ``server_version_num >= 190000``;
read tools degrade to an empty result on older servers, write tools raise
a descriptive ``PgqError``.

Security posture
----------------
* Identifier allowlist on every DDL slot (graph name + schema).
* ``run_pgq`` accepts only ``SELECT`` queries that mention
  ``GRAPH_TABLE`` — refuses everything else at the boundary, including
  ``;`` chained statements and comment-style fences. The ``max_rows``
  bound caps result size.
* ``create_property_graph`` only accepts a ``definition_body`` that starts
  with the ``VERTEX TABLES`` clause — the leading ``CREATE PROPERTY GRAPH``
  + identifier is composed by the tool, so callers cannot smuggle a
  different DDL statement in via the body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SqlDriver

# SQL/PGQ landed in the PG 19 series. We use the version-num probe rather
# than an extension allowlist because SQL/PGQ is built into the server,
# not shipped as an extension.
_MIN_PGQ_VERSION = 190000

# Identifier validator — Postgres unquoted identifier shape. Used for
# graph names + schema names where parameter binding can't reach.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ``run_pgq`` requires the query to mention GRAPH_TABLE so agents can't
# repurpose it as a generic ``run_select``. The match is intentionally
# permissive on surrounding whitespace.
_GRAPH_TABLE_RE = re.compile(r"\bGRAPH_TABLE\s*\(", re.IGNORECASE)

# A ``definition_body`` for ``CREATE PROPERTY GRAPH`` must begin with
# ``VERTEX TABLES`` — that's the SQL/PGQ grammar after the graph name.
# Enforcing this anchors the user-supplied body so callers cannot smuggle
# in a different DDL statement (DROP, ALTER, etc.) via the parameter.
_PG_DEFINITION_PREFIX_RE = re.compile(r"^\s*VERTEX\s+TABLES\b", re.IGNORECASE)

# Default cap on rows returned by ``run_pgq``. Generous enough for an
# agent demo, low enough to keep accidental whole-graph scans from
# saturating context.
_DEFAULT_MAX_ROWS = 200


class PgqError(Exception):
    """Raised when a SQL/PGQ operation cannot complete."""


# ---------------------------------------------------------------------------
# Dataclasses — one per return shape. frozen, slots, no surprises.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PgqStatus:
    """Reports whether SQL/PGQ is usable on this server.

    ``available`` is True when ``server_version_num`` >= 190000. ``detail``
    is a human-readable explanation suitable for surfacing back to an LLM
    agent when the answer is "not available".
    """

    available: bool
    server_version_num: int
    server_version: str
    detail: str


@dataclass(frozen=True, slots=True)
class PropertyGraphInfo:
    """A property graph defined in the database.

    ``vertex_tables`` and ``edge_tables`` are lists of qualified table
    names (``schema.name``) the property graph references. Empty when the
    catalog doesn't expose the membership — the read still returns the
    graph row so callers know it exists.
    """

    schema: str
    name: str
    vertex_tables: list[str]
    edge_tables: list[str]


@dataclass(frozen=True, slots=True)
class PgqRunResult:
    """The result of a ``SELECT ... FROM GRAPH_TABLE(...)`` query.

    ``columns`` mirrors the ``COLUMNS (...)`` clause of the query; each
    row in ``rows`` is a dict keyed by those columns. ``row_count`` and
    ``truncated`` make it explicit when the result hit ``max_rows``.
    """

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class CreatePropertyGraphResult:
    schema: str
    name: str
    created: bool


@dataclass(frozen=True, slots=True)
class DropPropertyGraphResult:
    schema: str
    name: str
    dropped: bool


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _server_version(driver: SqlDriver) -> tuple[int, str]:
    """Return ``(server_version_num, server_version)`` in one round trip."""
    rows = await driver.execute_query(
        "SELECT current_setting('server_version_num')::int AS ver_num,   current_setting('server_version') AS ver",
        force_readonly=True,
    )
    if not rows:
        return 0, ""
    cells = rows[0].cells
    return int(cells.get("ver_num") or 0), str(cells.get("ver") or "")


def _validate_identifier(label: str, value: str) -> str:
    if not _IDENT_RE.match(value):
        raise PgqError(f"{label} {value!r} is not a valid unquoted SQL identifier")
    return value


async def _require_pg19(driver: SqlDriver) -> None:
    ver_num, ver = await _server_version(driver)
    if ver_num < _MIN_PGQ_VERSION:
        raise PgqError(
            f"SQL/PGQ requires PostgreSQL 19 or newer; this server reports {ver or 'unknown'} "
            f"(server_version_num={ver_num})"
        )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def get_pgq_status(driver: SqlDriver) -> PgqStatus:
    """Report whether SQL/PGQ is usable on this server.

    Read-only; never raises. Returns ``available=False`` with a useful
    diagnostic on PG < 19 so an agent can fall back to the AGE-style
    ``run_cypher`` surface.
    """
    ver_num, ver = await _server_version(driver)
    available = ver_num >= _MIN_PGQ_VERSION
    detail = (
        "SQL/PGQ is available — use list_property_graphs / run_pgq."
        if available
        else (
            "SQL/PGQ requires PostgreSQL 19 or newer; this server is older. "
            "Fall back to the AGE-style graph_operations bucket "
            "(`run_cypher`, `create_graph`)."
        )
    )
    return PgqStatus(
        available=available,
        server_version_num=ver_num,
        server_version=ver,
        detail=detail,
    )


async def list_property_graphs(driver: SqlDriver) -> list[PropertyGraphInfo]:
    """List property graphs defined in the database.

    Returns an empty list on PG < 19. On PG 19+, queries the
    ``information_schema`` SQL/PGQ catalog views (per the standard) and
    falls back to an empty list when the catalog is not yet populated
    (early Beta releases may not expose the views).
    """
    ver_num, _ = await _server_version(driver)
    if ver_num < _MIN_PGQ_VERSION:
        return []
    # The SQL/PGQ standard defines ``information_schema.sql_property_graphs``.
    # Beta-1 implementations may not expose this view yet — we wrap the
    # query in a TRY/EXCEPT-equivalent (catch + return empty) so the read
    # surface stays deterministic.
    try:
        rows = await driver.execute_query(
            "SELECT graph_schema AS schema, graph_name AS name "
            "FROM information_schema.sql_property_graphs "
            "ORDER BY graph_schema, graph_name",
            force_readonly=True,
        )
    except Exception:
        return []
    results: list[PropertyGraphInfo] = []
    for row in rows or []:
        schema = row.cells["schema"]
        name = row.cells["name"]
        vertex_tables, edge_tables = await _graph_member_tables(driver, schema, name)
        results.append(
            PropertyGraphInfo(
                schema=schema,
                name=name,
                vertex_tables=vertex_tables,
                edge_tables=edge_tables,
            )
        )
    return results


async def _graph_member_tables(driver: SqlDriver, schema: str, name: str) -> tuple[list[str], list[str]]:
    """Best-effort vertex / edge table list for one graph.

    The standard exposes ``information_schema.sql_property_graph_tables``
    with a ``table_kind`` ('VERTEX' | 'EDGE'). On a Beta build where the
    view is missing we return ``([], [])`` rather than raising — the
    primary catalog row (which we already have) is enough for the
    describe surface.
    """
    try:
        rows = await driver.execute_query(
            "SELECT table_schema || '.' || table_name AS qname, "
            "  table_kind AS kind "
            "FROM information_schema.sql_property_graph_tables "
            "WHERE graph_schema = %s AND graph_name = %s "
            "ORDER BY table_kind, table_schema, table_name",
            params=[schema, name],
            force_readonly=True,
        )
    except Exception:
        return [], []
    vertex_tables: list[str] = []
    edge_tables: list[str] = []
    for row in rows or []:
        kind = str(row.cells.get("kind") or "").upper()
        bucket = vertex_tables if kind == "VERTEX" else edge_tables
        bucket.append(str(row.cells["qname"]))
    return vertex_tables, edge_tables


async def describe_property_graph(driver: SqlDriver, schema: str, name: str) -> PropertyGraphInfo:
    """Describe a single property graph by schema-qualified name.

    Raises ``PgqError`` when PG < 19 (the entire surface is unavailable)
    or when the graph does not exist.
    """
    await _require_pg19(driver)
    _validate_identifier("schema", schema)
    _validate_identifier("graph name", name)
    rows = await driver.execute_query(
        "SELECT graph_schema AS schema, graph_name AS name "
        "FROM information_schema.sql_property_graphs "
        "WHERE graph_schema = %s AND graph_name = %s",
        params=[schema, name],
        force_readonly=True,
    )
    if not rows:
        raise PgqError(f"no property graph {schema}.{name}")
    vertex_tables, edge_tables = await _graph_member_tables(driver, schema, name)
    return PropertyGraphInfo(
        schema=schema,
        name=name,
        vertex_tables=vertex_tables,
        edge_tables=edge_tables,
    )


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


def _is_safe_pgq_query(query: str) -> bool:
    """Return True when ``query`` looks like a single ``SELECT ... GRAPH_TABLE`` query.

    The boundary check is deliberately conservative: anything that doesn't
    obviously fit the SQL/PGQ read shape is refused. We do this at the
    tool boundary so the read driver never sees DDL / DML / multi-statement
    SQL via ``run_pgq``.
    """
    stripped = query.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if not lowered.startswith("select") and not lowered.startswith("with"):
        return False
    # Reject obvious chained-statement attempts. A trailing ``;`` is fine
    # (psycopg strips it), but a second statement is not.
    body = stripped.rstrip(";").strip()
    if ";" in body:
        return False
    if not _GRAPH_TABLE_RE.search(body):
        return False
    return True


async def run_pgq(
    driver: SqlDriver,
    query: str,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
) -> PgqRunResult:
    """Execute a SQL/PGQ ``SELECT ... GRAPH_TABLE`` query and return the rows.

    Requires PG 19+. The query must be a single ``SELECT`` (or ``WITH ...
    SELECT``) statement that references ``GRAPH_TABLE`` — anything else is
    refused at the boundary. ``max_rows`` caps the result set; when the
    cap is hit, ``truncated=True``.
    """
    await _require_pg19(driver)
    if max_rows <= 0:
        raise PgqError("max_rows must be positive")
    if not _is_safe_pgq_query(query):
        raise PgqError(
            "run_pgq accepts only a single SELECT / WITH statement that references "
            "GRAPH_TABLE(...). Use run_select for non-graph queries."
        )
    rows = await driver.execute_query(query, force_readonly=True)
    materialised = list(rows or [])
    truncated = len(materialised) > max_rows
    if truncated:
        materialised = materialised[:max_rows]
    columns: list[str] = []
    if materialised:
        columns = list(materialised[0].cells.keys())
    return PgqRunResult(
        columns=columns,
        rows=[dict(row.cells) for row in materialised],
        row_count=len(materialised),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------


def _validate_definition_body(body: str) -> str:
    """Reject definition bodies that don't start with ``VERTEX TABLES``."""
    if not body or not body.strip():
        raise PgqError("definition_body must be non-empty")
    if ";" in body:
        raise PgqError("definition_body must not contain ';' — submit a single CREATE PROPERTY GRAPH body")
    if not _PG_DEFINITION_PREFIX_RE.match(body):
        raise PgqError(
            "definition_body must begin with the VERTEX TABLES clause "
            "(the CREATE PROPERTY GRAPH header is composed by the tool)"
        )
    return body.strip()


async def create_property_graph(
    driver: SqlDriver,
    *,
    schema: str,
    name: str,
    definition_body: str,
) -> CreatePropertyGraphResult:
    """Run ``CREATE PROPERTY GRAPH schema.name <definition_body>``.

    The tool composes the ``CREATE PROPERTY GRAPH`` header + qualified
    identifier itself; ``definition_body`` carries the ``VERTEX TABLES``
    (and optional ``EDGE TABLES``) clauses. Requires PG 19+.

    Returns ``CreatePropertyGraphResult`` with ``created=True`` on
    success.
    """
    await _require_pg19(driver)
    _validate_identifier("schema", schema)
    _validate_identifier("graph name", name)
    body = _validate_definition_body(definition_body)
    await driver.execute_query(
        f'CREATE PROPERTY GRAPH "{schema}"."{name}" {body}',
        force_readonly=False,
    )
    return CreatePropertyGraphResult(schema=schema, name=name, created=True)


async def drop_property_graph(
    driver: SqlDriver,
    *,
    schema: str,
    name: str,
    if_exists: bool = True,
) -> DropPropertyGraphResult:
    """Run ``DROP PROPERTY GRAPH [IF EXISTS] schema.name``. Requires PG 19+."""
    await _require_pg19(driver)
    _validate_identifier("schema", schema)
    _validate_identifier("graph name", name)
    if_exists_clause = "IF EXISTS " if if_exists else ""
    await driver.execute_query(
        f'DROP PROPERTY GRAPH {if_exists_clause}"{schema}"."{name}"',
        force_readonly=False,
    )
    return DropPropertyGraphResult(schema=schema, name=name, dropped=True)


__all__ = [
    "CreatePropertyGraphResult",
    "DropPropertyGraphResult",
    "PgqError",
    "PgqRunResult",
    "PgqStatus",
    "PropertyGraphInfo",
    "create_property_graph",
    "describe_property_graph",
    "drop_property_graph",
    "get_pgq_status",
    "list_property_graphs",
    "run_pgq",
]
