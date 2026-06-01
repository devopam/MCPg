from dataclasses import dataclass, field

from mcpg.cache import CacheManager
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
    cache: CacheManager = field(default_factory=lambda: CacheManager(enabled=False))
