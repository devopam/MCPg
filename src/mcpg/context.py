"""Shared application context exposed to every tool invocation."""

from __future__ import annotations

from dataclasses import dataclass

from mcpg.config import Settings
from mcpg.database import Database


@dataclass(frozen=True, slots=True)
class AppContext:
    """State shared with every tool invocation for the server's lifetime."""

    settings: Settings
    database: Database
