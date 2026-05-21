"""PostgreSQL extension management.

``enable_extension`` runs ``CREATE EXTENSION``. Because an extension name is
a SQL identifier (not a bindable value), it cannot be parameterised — so only
names on a curated allowlist may be enabled. That allowlist is the injection
guard.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpg._vendor.sql import SqlDriver

# Extensions MCPg will enable on request: well-known, widely-used extensions.
# CREATE EXTENSION takes an identifier, so this allowlist guards against
# injection — names outside it are rejected before any SQL is built.
ENABLEABLE_EXTENSIONS = frozenset(
    {
        "pg_trgm",
        "vector",
        "unaccent",
        "fuzzystrmatch",
        "citext",
        "hstore",
        "pgcrypto",
        "uuid-ossp",
        "ltree",
        "btree_gin",
        "btree_gist",
        "pg_stat_statements",
        "pgstattuple",
        "tablefunc",
        "intarray",
        "cube",
        "earthdistance",
        "postgis",
    }
)


class ExtensionError(Exception):
    """Raised when an extension cannot be enabled."""


async def extension_installed(driver: SqlDriver, name: str) -> bool:
    """Return whether the named extension is installed in the database."""
    rows = await driver.execute_query(
        "SELECT 1 AS present FROM pg_extension WHERE extname = %s",
        params=[name],
        force_readonly=True,
    )
    return bool(rows)


@dataclass(frozen=True, slots=True)
class EnableExtensionResult:
    """The outcome of an enable_extension call."""

    name: str
    enabled: bool


async def enable_extension(driver: SqlDriver, name: str) -> EnableExtensionResult:
    """Enable a known PostgreSQL extension (``CREATE EXTENSION IF NOT EXISTS``).

    Only extensions on :data:`ENABLEABLE_EXTENSIONS` may be enabled. The call
    is idempotent.

    Raises:
        ExtensionError: If the extension is not on the allowlist, or creation
            fails (e.g. the extension's files are not present on the server).
    """
    if name not in ENABLEABLE_EXTENSIONS:
        raise ExtensionError(f"extension {name!r} is not on the allowlist of enableable extensions")
    try:
        await driver.execute_query(f'CREATE EXTENSION IF NOT EXISTS "{name}"', force_readonly=False)
    except Exception as exc:
        raise ExtensionError(str(exc)) from exc
    return EnableExtensionResult(name=name, enabled=True)
