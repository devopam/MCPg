"""Shared application context exposed to every tool invocation."""

from __future__ import annotations

from dataclasses import dataclass

from mcpg.config import Settings
from mcpg.cursors import CursorManager
from mcpg.database import Database
from mcpg.listen import ListenManager


@dataclass(frozen=True, slots=True)
class AppContext:
    """State shared with every tool invocation for the server's lifetime."""

    settings: Settings
    database: Database
    listen_manager: ListenManager
    cursor_manager: CursorManager
