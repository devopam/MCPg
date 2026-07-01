"""Tests for live-operations introspection and the list_active_queries tool."""

from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.liveops import (
    ActiveQuery,
    BackendActionResult,
    IndexBuildProgress,
    _safe_pct,
    cancel_query,
    list_active_queries,
    monitor_index_build,
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


async def test_cancel_query_never_marks_the_call_force_readonly() -> None:
    # A PID names a backend on a specific physical server; force_readonly
    # would make this eligible for replica routing, which could target the
    # wrong server. Must always go through the primary-bound path.
    driver = FakeDriver([{"ok": True}])

    await cancel_query(driver, 4242)

    assert driver.calls[0][2] is False


async def test_cancel_query_reports_failure_for_an_unknown_backend() -> None:
    result = await cancel_query(FakeDriver([{"ok": False}]), 999999)

    assert result.succeeded is False


async def test_terminate_backend_never_marks_the_call_force_readonly() -> None:
    driver = FakeDriver([{"ok": True}])

    await terminate_backend(driver, 7000)

    assert driver.calls[0][2] is False


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


# --- monitor_index_build --------------------------------------------------


def test_safe_pct_handles_zero_and_overshoot() -> None:
    assert _safe_pct(0, 0) is None
    assert _safe_pct(5, 0) is None
    assert _safe_pct(0, 10) == 0.0
    assert _safe_pct(5, 10) == 50.0
    # PG sometimes reports counters that briefly exceed the planner's
    # estimate — cap them at 100 rather than handing back >100 to agents.
    assert _safe_pct(20, 10) == 100.0
    # Negative inputs are clamped to 0 (defensive — shouldn't happen).
    assert _safe_pct(-1, 10) == 0.0


def _progress_row(**overrides: object) -> dict[str, object]:
    """A pg_stat_progress_create_index row with sensible defaults."""
    row: dict[str, object] = {
        "pid": 12345,
        "schema": "app",
        "relation": "widget",
        "index_name": "widget_embedding_idx",
        "command": "CREATE INDEX CONCURRENTLY",
        "phase": "building index: scanning table",
        "blocks_done": 5000,
        "blocks_total": 10000,
        "tuples_done": 0,
        "tuples_total": 0,
        "partitions_done": 0,
        "partitions_total": 0,
    }
    row.update(overrides)
    return row


async def test_monitor_index_build_returns_empty_when_no_builds_are_running() -> None:
    driver = FakeDriver([])
    assert await monitor_index_build(driver) == []  # type: ignore[arg-type]


async def test_monitor_index_build_computes_block_level_progress() -> None:
    driver = FakeDriver([_progress_row(blocks_done=2500, blocks_total=10000)])

    builds = await monitor_index_build(driver)  # type: ignore[arg-type]

    assert builds == [
        IndexBuildProgress(
            pid=12345,
            schema="app",
            relation="widget",
            index_name="widget_embedding_idx",
            command="CREATE INDEX CONCURRENTLY",
            phase="building index: scanning table",
            progress_pct=25.0,
            blocks_done=2500,
            blocks_total=10000,
            tuples_done=0,
            tuples_total=0,
            partitions_done=0,
            partitions_total=0,
        )
    ]


async def test_monitor_index_build_falls_back_to_tuple_progress_when_blocks_absent() -> None:
    # Some phases (e.g. "loading tuples in tree") report only tuple
    # counters — block_total is 0, so use tuples instead.
    driver = FakeDriver([_progress_row(blocks_done=0, blocks_total=0, tuples_done=750, tuples_total=1000)])

    [build] = await monitor_index_build(driver)  # type: ignore[arg-type]

    assert build.progress_pct == 75.0
    assert build.blocks_total == 0
    assert build.tuples_total == 1000


async def test_monitor_index_build_returns_null_progress_when_neither_denominator_is_known() -> None:
    # Initial "initializing" phase — counters all zero, so progress is
    # genuinely unknown. Don't fabricate a number.
    driver = FakeDriver([_progress_row(blocks_done=0, blocks_total=0, tuples_done=0, tuples_total=0)])

    [build] = await monitor_index_build(driver)  # type: ignore[arg-type]

    assert build.progress_pct is None


async def test_monitor_index_build_propagates_null_catalog_lookups() -> None:
    # If the relation lives in a schema we can't see, the LEFT JOIN
    # produces NULL — the dataclass models that as None and the tool
    # still returns the row (so the agent at least sees the pid + phase).
    driver = FakeDriver([_progress_row(schema=None, relation=None, index_name=None, phase="initializing")])

    [build] = await monitor_index_build(driver)  # type: ignore[arg-type]

    assert build.schema is None
    assert build.relation is None
    assert build.index_name is None
    assert build.phase == "initializing"


async def test_monitor_index_build_orders_builds_by_pid() -> None:
    # The SQL ORDER BY p.pid; FakeDriver returns rows in insertion order
    # so we verify the query actually includes ORDER BY rather than the
    # result ordering (which would be a tautology against FakeDriver).
    driver = FakeDriver([_progress_row(pid=p) for p in (10, 20, 30)])

    await monitor_index_build(driver)  # type: ignore[arg-type]

    sql = driver.calls[0][0]
    assert "ORDER BY p.pid" in sql
    assert driver.calls[0][2] is True  # force_readonly


async def test_monitor_index_build_tool_is_listed_and_callable_in_read_mode() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver([])))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "monitor_index_build" in listed
        result = await client.call_tool("monitor_index_build", {})

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["result"] == []
