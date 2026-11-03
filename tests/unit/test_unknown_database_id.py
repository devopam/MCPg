"""GH #367: an unknown ``database`` id must reach the client with a useful message."""

from __future__ import annotations

from _fakes import FakePool
from _mcp_test_helpers import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.database import Database
from mcpg.server import create_server

_PRIMARY = "postgresql://u:p@localhost:5432/episteme"


async def _call_with_database(env: dict[str, str], database: str) -> str:
    settings = load_settings({"MCPG_DATABASE_URL": _PRIMARY, **env})
    db = Database(
        settings,  # type: ignore[arg-type]
        pool=FakePool(),  # type: ignore[arg-type]
        secondary_pools={"analytics": FakePool()} if env else None,  # type: ignore[arg-type]
    )
    await db.connect()
    server = create_server(settings, database=db)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("run_select", {"sql": "select 1", "database": database})
    assert result.is_error is True
    return "".join(getattr(block, "text", "") for block in result.content)


async def test_unknown_database_id_names_the_valid_ids_primary_only() -> None:
    text = await _call_with_database({}, "episteme_test")
    assert "unknown database id 'episteme_test'" in text
    assert "episteme" in text


async def test_unknown_database_id_says_when_no_secondaries_are_configured() -> None:
    text = await _call_with_database({}, "episteme_test")
    assert "no secondary databases are configured" in text
    assert "MCPG_SECONDARY_DATABASE_URLS" in text


async def test_unknown_database_id_lists_secondaries_when_configured() -> None:
    text = await _call_with_database({"MCPG_SECONDARY_DATABASE_URLS": "analytics=postgresql://u:p@localhost/a"}, "nope")
    assert "valid ids: episteme, analytics" in text
    assert "no secondary databases are configured" not in text
    assert "list_databases" in text
