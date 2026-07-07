"""Tests for the access-mode capability policy."""

import pytest

from mcpg.config import AccessMode
from mcpg.policy import Capability, is_permitted, permitted_capabilities


@pytest.mark.parametrize("access_mode", list(AccessMode))
def test_every_access_mode_has_a_policy(access_mode: AccessMode) -> None:
    # Every mode must resolve without a KeyError, and reads are always allowed.
    assert Capability.READ in permitted_capabilities(access_mode)


@pytest.mark.parametrize("access_mode", list(AccessMode))
def test_read_capability_is_permitted_in_every_mode(access_mode: AccessMode) -> None:
    assert is_permitted(access_mode, Capability.READ) is True


def test_write_capability_is_permitted_in_restricted_and_unrestricted_modes() -> None:
    # restricted is the "safe read-write" tier: reads + DML writes.
    assert is_permitted(AccessMode.UNRESTRICTED, Capability.WRITE) is True
    assert is_permitted(AccessMode.RESTRICTED, Capability.WRITE) is True
    assert is_permitted(AccessMode.READ_ONLY, Capability.WRITE) is False


def test_ddl_capability_is_permitted_only_in_unrestricted_mode() -> None:
    # restricted allows writes but NOT schema changes — DDL stays unrestricted.
    assert is_permitted(AccessMode.UNRESTRICTED, Capability.DDL) is True
    assert is_permitted(AccessMode.RESTRICTED, Capability.DDL) is False
    assert is_permitted(AccessMode.READ_ONLY, Capability.DDL) is False


def test_restricted_mode_permits_only_read_and_write() -> None:
    assert permitted_capabilities(AccessMode.RESTRICTED) == frozenset({Capability.READ, Capability.WRITE})


def test_shell_listen_migrate_are_unrestricted_only() -> None:
    # The structural / out-of-database capabilities never appear in restricted.
    for cap in (Capability.SHELL, Capability.LISTEN, Capability.MIGRATE):
        assert is_permitted(AccessMode.RESTRICTED, cap) is False
        assert is_permitted(AccessMode.READ_ONLY, cap) is False
        assert is_permitted(AccessMode.UNRESTRICTED, cap) is True


def test_unrestricted_mode_permits_all_capabilities() -> None:
    assert permitted_capabilities(AccessMode.UNRESTRICTED) == frozenset(
        {
            Capability.READ,
            Capability.WRITE,
            Capability.DDL,
            Capability.SHELL,
            Capability.LISTEN,
            Capability.MIGRATE,
        }
    )
