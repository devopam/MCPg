"""TimescaleDB read-only functions in the SQL-safety allowlist (issue #353)."""

from unittest.mock import AsyncMock, Mock

import pytest

from mcpg.sql import SafeSqlDriver, SqlDriver
from mcpg.sql.allowlist import ALLOWED_FUNCTIONS

_READ_ONLY_CALLS = [
    "SELECT time_bucket('1 hour', ts), avg(v) FROM m GROUP BY 1",
    "SELECT time_bucket_gapfill('1 hour', ts), locf(avg(v)) FROM m GROUP BY 1",
    "SELECT interpolate(avg(v)) FROM m GROUP BY time_bucket_gapfill('1 hour', ts)",
    "SELECT first(v, ts), last(v, ts) FROM m",
    "SELECT histogram(v, 0, 100, 5) FROM m",
    "SELECT hypertable_size('m')",
    "SELECT hypertable_detailed_size('m')",
    "SELECT hypertable_approximate_size('m')",
    "SELECT hypertable_approximate_detailed_size('m')",
    "SELECT * FROM chunks_detailed_size('m')",
    "SELECT hypertable_index_size('m_idx')",
    "SELECT approximate_row_count('m')",
    "SELECT * FROM hypertable_compression_stats('m')",
    "SELECT * FROM hypertable_columnstore_stats('m')",
    "SELECT * FROM chunk_compression_stats('m')",
    "SELECT * FROM chunk_columnstore_stats('m')",
    "SELECT show_chunks('m', older_than => INTERVAL '1 day')",
    "SELECT show_tablespaces('m')",
    "SELECT by_range('ts'), by_hash('id', 4)",
    "SELECT generate_uuidv7(), to_uuidv7(now()), to_uuidv7_boundary(now())",
    "SELECT uuid_timestamp(id), uuid_timestamp_micros(id), uuid_version(id) FROM m",
    "SELECT * FROM timescaledb_experimental.show_policies('cagg')",
]

_BLOCKED_CALLS = [
    "SELECT drop_chunks('m', INTERVAL '1 day')",
    "SELECT create_hypertable('m', 'ts')",
    "SELECT compress_chunk('c')",
    "SELECT add_retention_policy('m', INTERVAL '1 day')",
    "SELECT alter_job(1, scheduled => false)",
    "SELECT delete_job(1)",
    "SELECT timescaledb_pre_restore()",
    "SELECT timescaledb_post_restore()",
    "SELECT get_telemetry_report()",
    "SELECT timescaledb_experimental.remove_all_policies('cagg')",
    "CALL run_job(1)",
    "CALL refresh_continuous_aggregate('cagg', NULL, NULL)",
]


@pytest.fixture
def driver() -> SafeSqlDriver:
    inner = Mock(spec=SqlDriver)
    inner.execute_query = AsyncMock(return_value=[])
    return SafeSqlDriver(inner)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", _READ_ONLY_CALLS)
async def test_read_only_timescaledb_functions_allowed(driver: SafeSqlDriver, query: str) -> None:
    await driver.execute_query(query)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", _BLOCKED_CALLS)
async def test_mutating_and_excluded_timescaledb_calls_blocked(driver: SafeSqlDriver, query: str) -> None:
    with pytest.raises(ValueError, match="Error validating query"):
        await driver.execute_query(query)


def test_excluded_names_not_in_allowlist() -> None:
    for name in ("drop_chunks", "timescaledb_pre_restore", "timescaledb_post_restore", "get_telemetry_report"):
        assert name not in ALLOWED_FUNCTIONS
