"""Tests for `mcpg.autovacuum.read_autovacuum_priority`."""

from __future__ import annotations

import pytest
from _fakes import FakeDriver

from mcpg.autovacuum import (
    AutovacuumPriorityReport,
    AutovacuumPriorityRow,
    read_autovacuum_priority,
)


def _row(
    *,
    schema: str = "public",
    table: str = "widgets",
    reltuples: int = 1000,
    n_dead_tup: int = 0,
    n_live_tup: int = 1000,
    n_mod_since_analyze: int = 0,
    n_ins_since_vacuum: int | None = 0,
    vacuum_threshold: float = 250.0,
    analyze_threshold: float = 150.0,
    last_autovacuum: str | None = None,
    last_autoanalyze: str | None = None,
    autovacuum_enabled: bool = True,
) -> dict[str, object]:
    return {
        "schema": schema,
        "table_name": table,
        "reltuples": reltuples,
        "n_dead_tup": n_dead_tup,
        "n_live_tup": n_live_tup,
        "n_mod_since_analyze": n_mod_since_analyze,
        "n_ins_since_vacuum": n_ins_since_vacuum,
        "vacuum_threshold": vacuum_threshold,
        "analyze_threshold": analyze_threshold,
        "last_autovacuum": last_autovacuum,
        "last_autoanalyze": last_autoanalyze,
        "autovacuum_enabled": autovacuum_enabled,
    }


async def test_returns_empty_report_when_no_user_tables_exist() -> None:
    report = await read_autovacuum_priority(FakeDriver([]))
    assert isinstance(report, AutovacuumPriorityReport)
    assert report.available is True
    assert report.rows == []
    assert report.overdue_count == 0
    assert report.watchlist_count == 0
    assert "No tables" in report.detail


async def test_classifies_a_table_past_the_threshold_as_overdue() -> None:
    driver = FakeDriver([_row(n_dead_tup=500, vacuum_threshold=250.0)])
    report = await read_autovacuum_priority(driver)
    assert report.rows[0].priority == "overdue"
    assert report.rows[0].dead_tuple_ratio == 2.0  # 500 / 250
    assert report.overdue_count == 1


async def test_classifies_a_table_within_50pct_of_threshold_as_watchlist() -> None:
    driver = FakeDriver([_row(n_dead_tup=150, vacuum_threshold=250.0)])
    report = await read_autovacuum_priority(driver)
    assert report.rows[0].priority == "watchlist"
    assert report.rows[0].dead_tuple_ratio == 0.6  # 150 / 250
    assert report.watchlist_count == 1
    assert report.overdue_count == 0


async def test_classifies_a_quiet_table_as_borderline() -> None:
    driver = FakeDriver([_row(n_dead_tup=10, vacuum_threshold=250.0)])
    report = await read_autovacuum_priority(driver)
    assert report.rows[0].priority == "borderline"
    assert report.overdue_count == 0
    assert report.watchlist_count == 0


async def test_rows_are_sorted_by_dead_tuple_ratio_descending() -> None:
    driver = FakeDriver(
        [
            _row(table="quiet", n_dead_tup=10, vacuum_threshold=250.0),  # 0.04
            _row(table="hot", n_dead_tup=500, vacuum_threshold=250.0),  # 2.0
            _row(table="warm", n_dead_tup=150, vacuum_threshold=250.0),  # 0.6
        ]
    )
    report = await read_autovacuum_priority(driver)
    assert [r.table for r in report.rows] == ["hot", "warm", "quiet"]


async def test_limit_clips_returned_rows_but_keeps_overdue_count_accurate() -> None:
    """`overdue_count` should reflect the *cluster*, not what fit under
    `limit` — agents branching on it can't be misled by pagination."""
    driver = FakeDriver([_row(table=f"t{i}", n_dead_tup=500, vacuum_threshold=250.0) for i in range(10)])
    report = await read_autovacuum_priority(driver, limit=3)
    assert len(report.rows) == 3
    assert report.overdue_count == 10


async def test_invalid_limit_falls_back_to_default() -> None:
    driver = FakeDriver([_row(n_dead_tup=500, vacuum_threshold=250.0)])
    report = await read_autovacuum_priority(driver, limit=0)
    assert report.available is True
    assert len(report.rows) == 1


async def test_driver_failure_surfaces_as_available_false() -> None:
    """Convention with `mcpg.health`: probe failures don't raise — they
    flip `available` to False and carry the reason in `detail`."""
    driver = FakeDriver([], fail=True)
    report = await read_autovacuum_priority(driver)
    assert report.available is False
    assert report.overdue_count == 0
    assert report.rows == []
    assert "probe failed" in report.detail


async def test_pg18_style_row_with_null_n_ins_since_vacuum_does_not_crash() -> None:
    """`n_ins_since_vacuum` is the PG 13+ column; on a probe miss the
    cell can be None. The report shape must preserve that."""
    driver = FakeDriver([_row(n_dead_tup=500, vacuum_threshold=250.0, n_ins_since_vacuum=None)])
    report = await read_autovacuum_priority(driver)
    assert report.rows[0].n_ins_since_vacuum is None


async def test_autovacuum_disabled_surfaces_in_the_row() -> None:
    driver = FakeDriver([_row(n_dead_tup=500, vacuum_threshold=250.0, autovacuum_enabled=False)])
    report = await read_autovacuum_priority(driver)
    row = report.rows[0]
    assert row.autovacuum_enabled is False
    # The classification is independent of opt-out — caller decides what
    # to do with an overdue table whose autovacuum is disabled.
    assert row.priority == "overdue"


async def test_zero_vacuum_threshold_does_not_divide_by_zero() -> None:
    """An empty table (`reltuples = 0`) with a zero scale factor yields
    `vacuum_threshold = 0`. The ratio computation must short-circuit to
    0.0 rather than raise `ZeroDivisionError`."""
    driver = FakeDriver([_row(n_dead_tup=0, vacuum_threshold=0.0, reltuples=0)])
    report = await read_autovacuum_priority(driver)
    assert report.rows[0].dead_tuple_ratio == 0.0
    assert report.rows[0].priority == "borderline"


@pytest.mark.parametrize(
    ("dead_tup", "threshold", "expected_ratio"),
    [
        (500, 250, 2.0),
        (250, 250, 1.0),
        (125, 250, 0.5),
        (10, 250, 0.04),
    ],
)
async def test_dead_tuple_ratio_arithmetic(dead_tup: int, threshold: float, expected_ratio: float) -> None:
    driver = FakeDriver([_row(n_dead_tup=dead_tup, vacuum_threshold=threshold)])
    report = await read_autovacuum_priority(driver)
    assert report.rows[0].dead_tuple_ratio == pytest.approx(expected_ratio)


def test_row_is_immutable() -> None:
    """`AutovacuumPriorityRow` is frozen so the report shape can't be
    mutated by an agent inadvertently after the tool returns it."""
    row = AutovacuumPriorityRow(
        schema="public",
        table="widgets",
        reltuples=1000,
        n_dead_tup=0,
        n_live_tup=1000,
        n_mod_since_analyze=0,
        n_ins_since_vacuum=0,
        vacuum_threshold=250.0,
        analyze_threshold=150.0,
        dead_tuple_ratio=0.0,
        analyze_ratio=0.0,
        last_autovacuum=None,
        last_autoanalyze=None,
        autovacuum_enabled=True,
        priority="borderline",
    )
    # frozen=True dataclasses raise FrozenInstanceError on attribute assignment.
    with pytest.raises(AttributeError):
        row.priority = "overdue"  # type: ignore[misc]
