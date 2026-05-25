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


def test_write_capability_is_permitted_only_in_unrestricted_mode() -> None:
    assert is_permitted(AccessMode.UNRESTRICTED, Capability.WRITE) is True
    assert is_permitted(AccessMode.RESTRICTED, Capability.WRITE) is False
    assert is_permitted(AccessMode.READ_ONLY, Capability.WRITE) is False


def test_ddl_capability_is_permitted_only_in_unrestricted_mode() -> None:
    assert is_permitted(AccessMode.UNRESTRICTED, Capability.DDL) is True
    assert is_permitted(AccessMode.RESTRICTED, Capability.DDL) is False
    assert is_permitted(AccessMode.READ_ONLY, Capability.DDL) is False


def test_unrestricted_mode_permits_all_capabilities() -> None:
    assert permitted_capabilities(AccessMode.UNRESTRICTED) == frozenset(
        {Capability.READ, Capability.WRITE, Capability.DDL, Capability.SHELL, Capability.LISTEN}
    )
