"""The common ancestor for every MCPg-raised exception.

Every domain-specific error class in this package (``ConfigError``,
``DatabaseError``, ``CursorError``, and the rest) subclasses this instead of
``Exception`` directly, so calling code that wants to catch "any error MCPg's
own logic raised" — as distinct from an unexpected bug surfacing from a
dependency — has one type to catch instead of an enumerated list kept in sync
by hand.

This is a pure marker base: it adds no behavior, no new attributes, and no
change to any existing exception's message format or call sites. Catching a
specific subclass (``except ConfigError:``) behaves exactly as it did before;
``except MCPgError:`` is the new capability this adds.
"""

from __future__ import annotations


class MCPgError(Exception):
    """Base class for every exception MCPg's own logic raises.

    Not raised directly — always through one of its domain-specific
    subclasses (``ConfigError``, ``DatabaseError``, etc.). Catch this
    directly only when the intent is genuinely "any MCPg-internal error,"
    not a specific failure mode.
    """
