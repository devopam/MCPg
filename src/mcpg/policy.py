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


# Capabilities permitted in each access mode. read-only and restricted both
# allow reads only; restricted additionally constrains execution (timeouts,
# row caps) at the tool level. Unrestricted adds writes.
_PERMITTED: dict[AccessMode, frozenset[Capability]] = {
    AccessMode.READ_ONLY: frozenset({Capability.READ}),
    AccessMode.RESTRICTED: frozenset({Capability.READ}),
    AccessMode.UNRESTRICTED: frozenset({Capability.READ, Capability.WRITE}),
}


def permitted_capabilities(access_mode: AccessMode) -> frozenset[Capability]:
    """Return the set of capabilities the given access mode permits."""
    return _PERMITTED[access_mode]


def is_permitted(access_mode: AccessMode, capability: Capability) -> bool:
    """Return whether ``access_mode`` permits ``capability``."""
    return capability in _PERMITTED[access_mode]
