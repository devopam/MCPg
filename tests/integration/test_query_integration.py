"""Integration tests for safe query execution against a live PostgreSQL."""

import pytest

from mcpg.database import Database
from mcpg.query import QueryError, explain_query, run_select


async def test_run_select_executes_against_real_postgres(connected_database: Database) -> None:
    result = await run_select(connected_database.driver(), "SELECT 1 AS one, 'x' AS label")

    assert result.row_count == 1
    assert result.columns == ["one", "label"]
    assert result.rows[0] == {"one": 1, "label": "x"}


async def test_run_select_caps_rows_against_real_postgres(connected_database: Database) -> None:
    result = await run_select(
        connected_database.driver(),
        "SELECT g FROM generate_series(1, 50) AS g",
        max_rows=10,
    )

    assert result.row_count == 10
    assert result.truncated is True


async def test_run_select_rejects_a_real_write(connected_database: Database) -> None:
    with pytest.raises(QueryError):
        await run_select(connected_database.driver(), "CREATE TABLE mcpg_should_not_exist (id int)")


async def test_explain_query_returns_a_real_plan(connected_database: Database) -> None:
    result = await explain_query(connected_database.driver(), "SELECT 1")

    # EXPLAIN (FORMAT JSON) yields a single-element list whose item has a Plan.
    assert isinstance(result.plan, list)
    assert "Plan" in result.plan[0]
