"""Tests for live-operations introspection and the list_active_queries tool."""

from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.liveops import ActiveQuery, list_active_queries
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


def _row(pid: int, **overrides: object) -> dict[str, object]:
    """A pg_stat_activity row with sensible defaults for one active backend."""
    row: dict[str, object] = {
        "pid": pid,
        "username": "app_user",
        "application": "psql",
        "state": "active",
        "wait_event": None,
        "duration_seconds": 1.5,
        "query": "SELECT 1",
        "blocked_by": [],
    }
    row.update(overrides)
    return row


async def test_list_active_queries_maps_rows() -> None:
    driver = FakeDriver([_row(101, query="SELECT * FROM widget")])

    assert await list_active_queries(driver) == [
        ActiveQuery(
            pid=101,
            username="app_user",
            application="psql",
            state="active",
            wait_event=None,
            duration_seconds=1.5,
            query="SELECT * FROM widget",
            blocked_by=[],
        )
    ]


async def test_list_active_queries_reports_a_blocked_backend() -> None:
    driver = FakeDriver([_row(102, wait_event="Lock:relation", blocked_by=[101])])

    result = await list_active_queries(driver)

    assert result[0].wait_event == "Lock:relation"
    assert result[0].blocked_by == [101]


async def test_list_active_queries_returns_empty_when_the_server_is_idle() -> None:
    assert await list_active_queries(FakeDriver([])) == []


async def test_list_active_queries_tool_is_callable_from_a_client() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver([])))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("list_active_queries", {})

    assert result.isError is False
