"""MCPgError is the common ancestor every domain-specific error subclasses."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import mcpg
from mcpg.errors import MCPgError


def _iter_mcpg_modules() -> list[str]:
    return [
        name
        for _, name, is_pkg in pkgutil.walk_packages(mcpg.__path__, prefix="mcpg.")
        if not is_pkg and "_vendor" not in name
    ]


def test_every_domain_error_class_subclasses_mcpg_error() -> None:
    offenders: list[str] = []
    for module_name in _iter_mcpg_modules():
        module = importlib.import_module(module_name)
        for obj_name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and obj_name.endswith("Error")
                and obj.__module__ == module_name
                and issubclass(obj, Exception)
                and obj is not MCPgError
                and not issubclass(obj, MCPgError)
            ):
                offenders.append(f"{module_name}.{obj_name}")
    assert not offenders, f"Exception classes not subclassing MCPgError: {offenders}"


def test_mcpg_error_is_a_plain_exception_subclass() -> None:
    assert issubclass(MCPgError, Exception)
    assert MCPgError.__doc__  # documented, not a bare pass-through
