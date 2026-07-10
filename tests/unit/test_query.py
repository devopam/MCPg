"""Tests for safe read-only query execution and the run_select tool."""

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.query import (
    ExplainResult,
    ParallelQueryResult,
    QueryError,
    QueryPlanAnalysis,
    QueryResult,
    analyze_query_plan,
    explain_query,
    run_select,
    run_select_parallel,
    run_select_tuned,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_run_select_returns_rows_columns_and_count() -> None:
    driver = FakeDriver([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    result = await run_select(driver, "SELECT id, name FROM widget")

    assert result == QueryResult(
        columns=["id", "name"],
        rows=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        row_count=2,
        truncated=False,
    )


async def test_run_select_on_empty_result_has_no_columns() -> None:
    result = await run_select(FakeDriver([]), "SELECT id FROM widget")

    assert result == QueryResult(columns=[], rows=[], row_count=0, truncated=False)


async def test_run_select_caps_rows_and_flags_truncation() -> None:
    driver = FakeDriver([{"id": n} for n in range(5)])

    result = await run_select(driver, "SELECT id FROM widget", max_rows=3)

    assert result.row_count == 3
    assert result.rows == [{"id": 0}, {"id": 1}, {"id": 2}]
    assert result.truncated is True


async def test_run_select_not_truncated_when_under_the_cap() -> None:
    driver = FakeDriver([{"id": n} for n in range(3)])

    result = await run_select(driver, "SELECT id FROM widget", max_rows=3)

    assert result.truncated is False


async def test_run_select_rejects_non_positive_max_rows() -> None:
    with pytest.raises(QueryError, match="max_rows"):
        await run_select(FakeDriver(), "SELECT 1", max_rows=0)


@pytest.mark.parametrize(
    "unsafe_sql",
    [
        "DROP TABLE widget",
        "DELETE FROM widget",
        "INSERT INTO widget (id) VALUES (1)",
        "UPDATE widget SET id = 1",
    ],
)
async def test_run_select_rejects_non_read_statements(unsafe_sql: str) -> None:
    with pytest.raises(QueryError):
        await run_select(FakeDriver(), unsafe_sql)


async def test_run_select_rejects_unparseable_sql() -> None:
    with pytest.raises(QueryError):
        await run_select(FakeDriver(), "this is not sql ;;;")


async def test_run_select_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([{"one": 1}]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("run_select", {"sql": "SELECT 1 AS one", "max_rows": 1})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["row_count"] == 1
    assert result.structuredContent["truncated"] is False


# --- run_select_tuned (roadmap 2.9) ----------------------------------------


async def test_run_select_tuned_runs_and_returns_query_result() -> None:
    driver = FakeDriver([{"a": 1, "n": 2}])

    result = await run_select_tuned(driver, "SELECT a, count(*) AS n FROM t GROUP BY a", work_mem="256MB")

    assert result == QueryResult(columns=["a", "n"], rows=[{"a": 1, "n": 2}], row_count=1, truncated=False)


async def test_run_select_tuned_emits_set_local_prefix_in_one_call() -> None:
    driver = FakeDriver([{"a": 1}])

    await run_select_tuned(
        driver,
        "SELECT a FROM t",
        work_mem="128MB",
        maintenance_work_mem="512MB",
    )

    # The tuned knobs + the SELECT must travel in a single execute_query
    # call so SET LOCAL applies to the same transaction; force_readonly on.
    assert len(driver.calls) == 1
    sql, _params, force_readonly = driver.calls[0]
    assert sql.startswith("SET LOCAL work_mem = '128MB'; ")
    assert "SET LOCAL maintenance_work_mem = '512MB'; " in sql
    assert sql.endswith("SELECT a FROM t")
    assert force_readonly is True


async def test_run_select_tuned_omits_maintenance_knob_when_unset() -> None:
    driver = FakeDriver([{"a": 1}])

    await run_select_tuned(driver, "SELECT a FROM t", work_mem="64MB")

    sql, _params, _force = driver.calls[0]
    assert "maintenance_work_mem" not in sql


@pytest.mark.parametrize(
    "bad_work_mem",
    [
        "256",  # no unit
        "256 MB",  # space
        "256mb; DROP TABLE t",  # injection attempt
        "0MB",  # non-positive
        "-1MB",  # negative (regex rejects)
        "4GB",  # over the 2GB cap
        "2097153kB",  # one kB over 2GiB
    ],
)
async def test_run_select_tuned_rejects_invalid_or_oversized_work_mem(bad_work_mem: str) -> None:
    with pytest.raises(QueryError):
        await run_select_tuned(FakeDriver(), "SELECT 1", work_mem=bad_work_mem)


async def test_run_select_tuned_accepts_the_2gb_boundary() -> None:
    driver = FakeDriver([{"x": 1}])

    result = await run_select_tuned(driver, "SELECT 1 AS x", work_mem="2GB")

    assert result.row_count == 1


async def test_run_select_tuned_validates_maintenance_work_mem_too() -> None:
    with pytest.raises(QueryError, match="maintenance_work_mem"):
        await run_select_tuned(FakeDriver(), "SELECT 1", work_mem="64MB", maintenance_work_mem="9GB")


@pytest.mark.parametrize(
    "unsafe_sql",
    ["DROP TABLE t", "DELETE FROM t", "UPDATE t SET a = 1", "INSERT INTO t (a) VALUES (1)"],
)
async def test_run_select_tuned_rejects_non_select(unsafe_sql: str) -> None:
    with pytest.raises(QueryError):
        await run_select_tuned(FakeDriver(), unsafe_sql, work_mem="64MB")


async def test_run_select_tuned_rejects_non_positive_max_rows() -> None:
    with pytest.raises(QueryError, match="max_rows"):
        await run_select_tuned(FakeDriver(), "SELECT 1", work_mem="64MB", max_rows=0)


async def test_run_select_tuned_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([{"one": 1}]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "run_select_tuned",
            {"sql": "SELECT 1 AS one", "work_mem": "128MB"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["row_count"] == 1


async def test_explain_query_returns_the_plan() -> None:
    plan = [{"Plan": {"Node Type": "Result"}}]
    driver = FakeDriver([{"QUERY PLAN": plan}])

    result = await explain_query(driver, "SELECT 1")

    assert result == ExplainResult(plan=plan)


async def test_explain_query_parses_a_json_string_plan() -> None:
    driver = FakeDriver([{"QUERY PLAN": '[{"Plan": {"Node Type": "Result"}}]'}])

    result = await explain_query(driver, "SELECT 1")

    assert result == ExplainResult(plan=[{"Plan": {"Node Type": "Result"}}])


async def test_explain_query_rejects_a_write() -> None:
    with pytest.raises(QueryError):
        await explain_query(FakeDriver(), "DROP TABLE widget")


async def test_explain_query_raises_when_no_plan_is_returned() -> None:
    with pytest.raises(QueryError, match="no plan"):
        await explain_query(FakeDriver([]), "SELECT 1")


async def test_explain_query_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([{"QUERY PLAN": [{"Plan": {"Node Type": "Result"}}]}]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("explain_query", {"sql": "SELECT 1"})

    assert result.isError is False


_PLAN_TREE = [
    {
        "Plan": {
            "Node Type": "Hash Join",
            "Total Cost": 250.0,
            "Plan Rows": 1000,
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "orders", "Total Cost": 100.0, "Plan Rows": 5000},
                {"Node Type": "Index Scan", "Relation Name": "users", "Total Cost": 50.0, "Plan Rows": 1000},
            ],
        }
    }
]


async def test_analyze_query_plan_summarises_the_plan_tree() -> None:
    result = await analyze_query_plan(FakeDriver([{"QUERY PLAN": _PLAN_TREE}]), "SELECT 1")

    assert result == QueryPlanAnalysis(
        total_cost=250.0,
        estimated_rows=1000,
        node_types=["Hash Join", "Index Scan", "Seq Scan"],
        sequential_scans=["orders"],
    )


async def test_analyze_query_plan_reports_no_sequential_scans_for_an_index_plan() -> None:
    plan = [{"Plan": {"Node Type": "Index Scan", "Relation Name": "users", "Total Cost": 8.0, "Plan Rows": 1}}]

    result = await analyze_query_plan(FakeDriver([{"QUERY PLAN": plan}]), "SELECT 1")

    assert result.sequential_scans == []


async def test_analyze_query_plan_rejects_unexpected_explain_output() -> None:
    with pytest.raises(QueryError, match="unexpected EXPLAIN output"):
        await analyze_query_plan(FakeDriver([{"QUERY PLAN": {"not": "a list"}}]), "SELECT 1")


async def test_analyze_query_plan_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([{"QUERY PLAN": _PLAN_TREE}]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("analyze_query_plan", {"sql": "SELECT 1"})

    assert result.isError is False


# --- io=True / EXPLAIN ANALYZE BUFFERS path (roadmap 2.6) ---------------


async def test_explain_query_io_true_runs_analyze_buffers_timing() -> None:
    """With ``io=True`` the driver must see the full ANALYZE+BUFFERS+TIMING
    options on the EXPLAIN — otherwise PG won't emit the I/O fields."""
    driver = FakeDriver([{"QUERY PLAN": [{"Plan": {"Node Type": "Result"}}]}])
    await explain_query(driver, "SELECT 1", io=True)
    sent_query, _, force_readonly = driver.calls[-1]
    assert "ANALYZE" in sent_query
    assert "BUFFERS" in sent_query
    assert "TIMING" in sent_query
    assert "FORMAT JSON" in sent_query
    # EXPLAIN ANALYZE executes the query — it must run inside a read-only
    # transaction so a validated SELECT can't have a write side effect.
    assert force_readonly is True


async def test_explain_query_default_keeps_the_plan_only_path() -> None:
    """Regression — without ``io=True`` we must NOT run ANALYZE (that
    would execute every query an agent inspects)."""
    driver = FakeDriver([{"QUERY PLAN": [{"Plan": {"Node Type": "Result"}}]}])
    await explain_query(driver, "SELECT 1")
    sent_query, _, _ = driver.calls[-1]
    assert "ANALYZE" not in sent_query
    assert "BUFFERS" not in sent_query


_PLAN_TREE_WITH_BUFFERS = [
    {
        "Plan": {
            "Node Type": "Hash Join",
            "Total Cost": 250.0,
            "Plan Rows": 1000,
            "Actual Total Time": 42.5,
            "Actual Rows": 950,
            "Shared Hit Blocks": 100,
            "Shared Read Blocks": 20,
            "I/O Read Time": 12.3,
            "I/O Write Time": 0.0,
            # PG 19 BUFFERS extension — asynchronous-I/O block counts.
            "Async I/O Read Blocks": 15,
            "Async I/O Write Blocks": 0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "orders",
                    "Total Cost": 100.0,
                    "Plan Rows": 5000,
                    "Shared Hit Blocks": 80,
                    "Shared Read Blocks": 15,
                    "I/O Read Time": 8.0,
                    "I/O Write Time": 0.0,
                    "Async I/O Read Blocks": 10,
                    "Async I/O Write Blocks": 0,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "users",
                    "Total Cost": 50.0,
                    "Plan Rows": 1000,
                    "Shared Hit Blocks": 5,
                    "Shared Read Blocks": 2,
                    "I/O Read Time": 1.5,
                    "I/O Write Time": 0.0,
                    "Async I/O Read Blocks": 2,
                    "Async I/O Write Blocks": 0,
                },
            ],
        }
    }
]


async def test_analyze_query_plan_io_rolls_up_buffer_and_timing_counts() -> None:
    """Per-node buffer + IO counts get summed across the plan tree."""
    driver = FakeDriver([{"QUERY PLAN": _PLAN_TREE_WITH_BUFFERS}])
    result = await analyze_query_plan(driver, "SELECT 1", io=True)
    assert result.actual_total_time_ms == 42.5
    assert result.actual_rows == 950
    # Sums across hash-join + seq-scan + index-scan.
    assert result.shared_blocks_hit == 100 + 80 + 5
    assert result.shared_blocks_read == 20 + 15 + 2
    assert result.io_read_time_ms == pytest.approx(12.3 + 8.0 + 1.5)
    assert result.io_write_time_ms == 0.0
    # PG 19 AIO summary — non-None means the EXPLAIN carried the keys.
    assert result.aio_read_blocks == 15 + 10 + 2
    assert result.aio_write_blocks == 0


async def test_analyze_query_plan_io_aio_fields_are_none_when_explain_omits_them() -> None:
    """PG ≤ 18 doesn't emit ``Async I/O *`` keys in BUFFERS output;
    the rollup must report ``None`` (not 0) so the caller can tell
    "no AIO observations" apart from "zero AIO observations"."""
    plan = [
        {
            "Plan": {
                "Node Type": "Result",
                "Total Cost": 0.01,
                "Plan Rows": 1,
                "Actual Total Time": 0.05,
                "Actual Rows": 1,
                "Shared Hit Blocks": 1,
                "Shared Read Blocks": 0,
                "I/O Read Time": 0.0,
                "I/O Write Time": 0.0,
                # No "Async I/O Read Blocks" / "Async I/O Write Blocks" keys.
            }
        }
    ]
    result = await analyze_query_plan(FakeDriver([{"QUERY PLAN": plan}]), "SELECT 1", io=True)
    assert result.aio_read_blocks is None
    assert result.aio_write_blocks is None
    # Shared counts are still populated — pre-PG-19 BUFFERS reports them.
    assert result.shared_blocks_hit == 1


async def test_analyze_query_plan_default_leaves_io_fields_unset() -> None:
    """Without io=True the io-related fields stay None — agents who
    don't ask for execution shouldn't see synthesised zeros."""
    result = await analyze_query_plan(FakeDriver([{"QUERY PLAN": _PLAN_TREE}]), "SELECT 1")
    assert result.actual_total_time_ms is None
    assert result.shared_blocks_read is None
    assert result.aio_read_blocks is None


@pytest.mark.parametrize(
    "unsafe_sql",
    [
        "DROP TABLE widget",
        "DELETE FROM widget",
        "INSERT INTO widget (id) VALUES (1)",
        "UPDATE widget SET id = 1",
    ],
)
async def test_explain_query_io_true_still_rejects_writes(unsafe_sql: str) -> None:
    """``io=True`` switches to ``EXPLAIN ANALYZE`` (which executes the
    statement). The pre-flight validation must still reject every
    write/DDL form — otherwise ``explain_query(..., io=True)`` would
    become a backdoor to run any SQL the agent supplies."""
    with pytest.raises(QueryError):
        await explain_query(FakeDriver(), unsafe_sql, io=True)


class _StallingDriver:
    """SqlDriver double that hangs forever on execute_query.

    The ``io=True`` path bypasses ``SafeSqlDriver`` (which has built-in
    asyncio.timeout enforcement) — so the explain_query code must
    re-impose the timeout via ``asyncio.wait_for``. Without that, an
    EXPLAIN ANALYZE on a runaway query would block the server worker
    indefinitely. This double lets us prove the timeout fires.
    """

    async def execute_query(
        self, query: str, params: list[Any] | None = None, force_readonly: bool = False
    ) -> list[Any]:
        del query, params, force_readonly
        import asyncio

        await asyncio.Event().wait()  # never fires; hangs until cancelled
        return []  # pragma: no cover


async def test_explain_query_io_true_enforces_timeout_against_runaway_query() -> None:
    """The io=True path uses the raw driver and so loses SafeSqlDriver's
    built-in timeout. The code must re-impose it via asyncio.wait_for —
    otherwise a long-running EXPLAIN ANALYZE blocks the server worker."""
    with pytest.raises(QueryError):
        await explain_query(_StallingDriver(), "SELECT 1", io=True, timeout=0.05)  # type: ignore[arg-type]


# --- run_select_parallel (Phase 3.4) -------------------------------------


async def test_run_select_parallel_runs_all_statements_concurrently() -> None:
    driver = FakeDriver([{"x": 1}])

    result = await run_select_parallel(
        driver,
        [
            "SELECT 1 AS x",
            "SELECT 2 AS x",
            "SELECT 3 AS x",
        ],
    )

    assert isinstance(result, ParallelQueryResult)
    assert len(result.outcomes) == 3
    # Indices preserved + every slot succeeded.
    assert [o.index for o in result.outcomes] == [0, 1, 2]
    assert all(o.success for o in result.outcomes)
    assert all(o.error is None for o in result.outcomes)
    assert result.outcomes[0].result is not None
    assert result.outcomes[0].result.row_count == 1


async def test_run_select_parallel_rejects_empty_list() -> None:
    with pytest.raises(QueryError, match="empty"):
        await run_select_parallel(FakeDriver([]), [])


async def test_run_select_parallel_rejects_blank_entry() -> None:
    with pytest.raises(QueryError, match="blank"):
        await run_select_parallel(FakeDriver([]), ["SELECT 1", "   "])


async def test_run_select_parallel_rejects_too_many_statements() -> None:
    with pytest.raises(QueryError, match="too many"):
        await run_select_parallel(
            FakeDriver([]),
            [f"SELECT {i}" for i in range(20)],
            parallel_limit=5,
        )


async def test_run_select_parallel_captures_per_statement_failure() -> None:
    """A query that the safety allowlist rejects yields a captured outcome,
    not an exception that aborts the others."""
    driver = FakeDriver([{"x": 1}])

    result = await run_select_parallel(
        driver,
        [
            "SELECT 1 AS x",
            "DROP TABLE users",  # unsafe — should be rejected
            "SELECT 2 AS x",
        ],
    )

    # Three slots regardless of one bad entry.
    assert len(result.outcomes) == 3
    assert result.outcomes[0].success is True
    assert result.outcomes[1].success is False
    assert result.outcomes[1].error is not None
    assert result.outcomes[2].success is True


async def test_run_select_parallel_tool_is_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "run_select_parallel" in listed
