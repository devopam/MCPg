"""Access-mode policy: which tool capabilities each access mode permits.

Tools are grouped by :class:`Capability`. ``register_tools`` consults this
policy so that, for example, write tools are exposed only in unrestricted
mode. Read-only is the safe default (see ``mcpg.config``).
"""

from __future__ import annotations

from enum import StrEnum

from mcpg.config import AccessMode


class Capability(StrEnum):
    """The class of operation a tool performs."""

    READ = "read"
    WRITE = "write"
    DDL = "ddl"
    SHELL = "shell"
    LISTEN = "listen"
    MIGRATE = "migrate"


# Capabilities permitted in each access mode:
#   read-only    — reads only (the safe default).
#   restricted   — reads + data writes (DML/WRITE), but NOT schema changes
#                  (DDL), subprocess/shell, LISTEN/NOTIFY, or migrations.
#                  The "safe read-write" tier: an agent can INSERT/UPDATE/
#                  DELETE and run maintenance, but cannot alter structure or
#                  reach outside the database.
#   unrestricted — everything: writes, DDL, shell, listen, migrate.
# DDL/shell/listen/migrate additionally require their per-feature opt-in
# (MCPG_ALLOW_DDL / _SHELL / _LISTEN; migrate piggybacks on MCPG_ALLOW_DDL
# since the underlying ops are DDL). Those gates are enforced where tools
# register, not here, so this policy table stays the single source of truth.
_PERMITTED: dict[AccessMode, frozenset[Capability]] = {
    AccessMode.READ_ONLY: frozenset({Capability.READ}),
    AccessMode.RESTRICTED: frozenset({Capability.READ, Capability.WRITE}),
    AccessMode.UNRESTRICTED: frozenset(
        {
            Capability.READ,
            Capability.WRITE,
            Capability.DDL,
            Capability.SHELL,
            Capability.LISTEN,
            Capability.MIGRATE,
        }
    ),
}


def permitted_capabilities(access_mode: AccessMode) -> frozenset[Capability]:
    """Return the set of capabilities the given access mode permits."""
    return _PERMITTED[access_mode]


def is_permitted(access_mode: AccessMode, capability: Capability) -> bool:
    """Return whether ``access_mode`` permits ``capability``."""
    return capability in _PERMITTED[access_mode]


class PermissionError(Exception):
    """Raised when an operation is requested but the access mode does not permit it."""


def check_permission(capability: Capability, access_mode: AccessMode) -> None:
    """Raise PermissionError if capability is not permitted in access_mode."""
    if not is_permitted(access_mode, capability):
        raise PermissionError(f"access mode {access_mode.value} requires {capability.value.upper()} capability")
