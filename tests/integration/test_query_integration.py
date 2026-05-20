"""Integration tests for safe query execution against a live PostgreSQL."""

import pytest

from mcpg.database import Database
from mcpg.query import QueryError, run_select


async def test_run_select_executes_against_real_postgres(connected_database: Database) -> None:
    result = await run_select(connected_database.driver(), "SELECT 1 AS one, 'x' AS label")

    assert result.row_count == 1
    assert result.columns == ["one", "label"]
    assert result.rows[0] == {"one": 1, "label": "x"}


async def test_run_select_rejects_a_real_write(connected_database: Database) -> None:
    with pytest.raises(QueryError):
        await run_select(connected_database.driver(), "CREATE TABLE mcpg_should_not_exist (id int)")
