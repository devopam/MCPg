"""Tests for the pg_stat_io reader (Phase 4.3)."""

from __future__ import annotations

from _fakes import FakeRoutingDriver

from mcpg.io_stats import IOStatRow, IOStatsReport, read_pg_stat_io


async def test_read_pg_stat_io_reports_unavailable_on_pg14_pg15() -> None:
    # current_setting('server_version_num')::int returns < 160000
    driver = FakeRoutingDriver({"current_setting": [{"v": 150004}]})

    report = await read_pg_stat_io(driver)  # type: ignore[arg-type]

    assert isinstance(report, IOStatsReport)
    assert report.available is False
    assert report.server_version == 150004
    assert report.rows == []


async def test_read_pg_stat_io_returns_typed_rows_on_pg16_plus() -> None:
    driver = FakeRoutingDriver(
        {
            "current_setting": [{"v": 160003}],
            "pg_stat_io": [
                {
                    "backend_type": "client backend",
                    "object": "relation",
                    "context": "normal",
                    "reads": 1234,
                    "read_bytes": 1234 * 8192,
                    "read_time": 12.5,
                    "writes": 56,
                    "write_bytes": 56 * 8192,
                    "write_time": 3.4,
                    "writebacks": 0,
                    "extends": 2,
                    "extend_bytes": 2 * 8192,
                    "hits": 9999,
                    "evictions": 1,
                    "reuses": None,
                    "fsyncs": None,
                }
            ],
        }
    )

    report = await read_pg_stat_io(driver)  # type: ignore[arg-type]

    assert report.available is True
    assert report.server_version == 160003
    assert len(report.rows) == 1
    row = report.rows[0]
    assert isinstance(row, IOStatRow)
    assert row.backend_type == "client backend"
    assert row.reads == 1234
    assert row.read_time_ms == 12.5
    # Nullable counters preserved as None.
    assert row.reuses is None
    assert row.fsyncs is None
