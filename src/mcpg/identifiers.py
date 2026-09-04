"""Safe SQL identifier quoting — the shared, escape-based replacement for
the per-module ``[A-Za-z_][A-Za-z0-9_]*`` allowlists.

For most of MCPg's history each tool module carried its own copy of a
plain-identifier regex and validated schema / table / column / role names
against it *before* splicing them into SQL as ``f'"{name}"'``. That regex
was doing double duty: it kept the interpolation injection-safe (by
forbidding the ``"`` that could close the quoted identifier), but it also
rejected every schema PostgreSQL supports only through *delimited*
(double-quoted) identifiers — hyphens, spaces, mixed case, unicode. A user
with a schema literally named ``adm-pgbench`` could not dump, export, or
operate on it at all (issue #329).

This module separates the two concerns. :func:`quote_identifier` makes any
value that PostgreSQL accepts as a quoted identifier safe to splice, by
doubling embedded double quotes rather than by forbidding them — so the
name round-trips to the exact object it names, and the result still cannot
break out of the identifier. Callers that genuinely need a *bare* (unquoted)
identifier — Apache AGE labels, or a name that becomes an identifier in
generated Go/Rust/Elixir source — keep their own stricter validation; this
helper is only for names that reach PostgreSQL as SQL identifiers.

The rejections are the few things that are not addressable identifiers at
all rather than a style choice:

- the empty string (``""`` is not a legal identifier);
- an embedded NUL (``\x00`` cannot cross the wire protocol);
- anything longer than 63 bytes (``NAMEDATALEN - 1``), which PostgreSQL
  *silently truncates* — a footgun that turns ``a_very_long…_v1`` and
  ``a_very_long…_v2`` into the same object, so we refuse it explicitly.
"""

from __future__ import annotations

from mcpg.errors import MCPgError

# PostgreSQL's default NAMEDATALEN is 64; identifiers are capped at 63 bytes
# and anything longer is truncated server-side without error.
MAX_IDENTIFIER_BYTES = 63


class IdentifierError(MCPgError):
    """Raised when a value cannot be used as a SQL identifier at all.

    Not raised for names that merely need quoting — those are exactly what
    :func:`quote_identifier` accepts. Raised only for the empty string, an
    embedded NUL, or a name past PostgreSQL's 63-byte identifier limit.
    """


def ensure_identifier(name: str, kind: str = "identifier") -> str:
    """Return ``name`` unchanged after checking it is an addressable identifier.

    Raises :class:`IdentifierError` (message ``"invalid {kind} name: …"``)
    for the empty string, an embedded NUL, or a name longer than
    :data:`MAX_IDENTIFIER_BYTES`. Every other value — including one needing
    delimited quoting — passes through untouched, so a caller can build a
    dotted or otherwise composite reference and quote the parts itself.
    """
    if not name:
        raise IdentifierError(f"invalid {kind} name: must not be empty")
    if "\x00" in name:
        raise IdentifierError(f"invalid {kind} name: {name!r} contains a NUL byte")
    if len(name.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        raise IdentifierError(
            f"invalid {kind} name: {name!r} exceeds PostgreSQL's {MAX_IDENTIFIER_BYTES}-byte identifier limit"
        )
    return name


def quote_identifier(name: str, kind: str = "identifier") -> str:
    r"""Return ``name`` as a safely double-quoted SQL delimited identifier.

    Embedded double quotes are doubled (``"`` → ``""``) per the SQL
    delimited-identifier rules, so the value cannot terminate the quoted
    identifier and inject SQL. Every character PostgreSQL permits in a
    quoted identifier is accepted and preserved verbatim, case included:

    >>> quote_identifier("adm-pgbench")
    '"adm-pgbench"'
    >>> quote_identifier('weird"name')
    '"weird""name"'

    Raises :class:`IdentifierError` for the values :func:`ensure_identifier`
    rejects (empty, embedded NUL, over 63 bytes).
    """
    return '"' + ensure_identifier(name, kind).replace('"', '""') + '"'
