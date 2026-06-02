"""Tests for live-operations introspection and the list_active_queries tool."""

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.liveops import (
    ActiveQuery,
    BackendActionResult,
    cancel_query,
    list_active_queries,
    terminate_backend,
    verify_connection_encryption,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})
_UNRESTRICTED = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)


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


async def test_cancel_query_reports_the_signal_outcome() -> None:
    driver = FakeDriver([{"ok": True}])

    result = await cancel_query(driver, 4242)

    assert result == BackendActionResult(pid=4242, action="cancel_query", succeeded=True)
    assert driver.calls[0][1] == [4242]


async def test_cancel_query_reports_failure_for_an_unknown_backend() -> None:
    result = await cancel_query(FakeDriver([{"ok": False}]), 999999)

    assert result.succeeded is False


async def test_terminate_backend_reports_the_signal_outcome() -> None:
    driver = FakeDriver([{"ok": True}])

    result = await terminate_backend(driver, 7000)

    assert result == BackendActionResult(pid=7000, action="terminate_backend", succeeded=True)
    assert driver.calls[0][1] == [7000]


async def test_backend_control_tools_are_callable_in_unrestricted_mode() -> None:
    server = create_server(_UNRESTRICTED, database=FakeDatabase(FakeDriver([{"ok": True}])))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        cancelled = await client.call_tool("cancel_query", {"pid": 100})
        terminated = await client.call_tool("terminate_backend", {"pid": 100})

    assert cancelled.isError is False
    assert terminated.isError is False


# --- verify_connection_encryption -----------------------------------------


async def test_verify_connection_encryption_reports_an_encrypted_link() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_backend_pid()": [{"ssl": True, "version": "TLSv1.3", "cipher": "TLS_AES_256_GCM_SHA384", "bits": 256}],
            "count(*) AS total": [{"total": 5, "encrypted": 4}],
        }
    )

    result = await verify_connection_encryption(driver)  # type: ignore[arg-type]

    assert result.ssl is True
    assert result.version == "TLSv1.3"
    assert result.cipher == "TLS_AES_256_GCM_SHA384"
    assert result.bits == 256
    assert result.total_connections == 5
    assert result.encrypted_connections == 4
    assert result.unencrypted_connections == 1


async def test_verify_connection_encryption_nulls_cipher_when_plaintext() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_backend_pid()": [{"ssl": False, "version": None, "cipher": None, "bits": None}],
            "count(*) AS total": [{"total": 2, "encrypted": 0}],
        }
    )

    result = await verify_connection_encryption(driver)  # type: ignore[arg-type]

    assert result.ssl is False
    assert result.version is None and result.cipher is None and result.bits is None
    assert result.unencrypted_connections == 2


async def test_verify_connection_encryption_tool_is_registered_in_read_mode() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_backend_pid()": [{"ssl": True, "version": "TLSv1.3", "cipher": "X", "bits": 256}],
            "count(*) AS total": [{"total": 1, "encrypted": 1}],
        }
    )
    server = create_server(_SETTINGS, database=FakeDatabase(driver))  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "verify_connection_encryption" in listed
        result = await client.call_tool("verify_connection_encryption", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["ssl"] is True
