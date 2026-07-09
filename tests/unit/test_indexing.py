"""Tests for index recommendations and the recommend_indexes tool."""

import pytest
from _fakes import FakeDatabase, FakeDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.indexing import IndexRecommendation, IndexSuggestion, recommend_indexes
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


def _row(column: str, data_type: str) -> dict[str, object]:
    """One (candidate table x column) row for the orders table."""
    return {
        "schemaname": "app",
        "relname": "orders",
        "seq_scan": 5000,
        "n_live_tup": 250000,
        "column_name": column,
        "data_type": data_type,
        "parent_schema": None,
        "parent_table": None,
    }


def _partition_row(
    partition: str, parent: str, column: str, data_type: str, *, seq_scan: int, n_live_tup: int
) -> dict[str, object]:
    """One (partition x column) row whose parent partitioned table is ``parent``."""
    return {
        "schemaname": "app",
        "relname": partition,
        "seq_scan": seq_scan,
        "n_live_tup": n_live_tup,
        "column_name": column,
        "data_type": data_type,
        "parent_schema": "app",
        "parent_table": parent,
    }


async def test_recommend_indexes_groups_columns_into_one_table_recommendation() -> None:
    driver = FakeDriver([_row("id", "integer"), _row("payload", "jsonb")])

    result = await recommend_indexes(driver)

    assert result == [
        IndexRecommendation(
            schema="app",
            table="orders",
            seq_scans=5000,
            live_tuples=250000,
            reason="large table read mostly by sequential scan",
            suggestions=[IndexSuggestion("payload", "gin", "GIN supports jsonb containment and key lookups")],
            partitioned=False,
        )
    ]


async def test_recommend_indexes_deduplicates_suggestions_across_partitions() -> None:
    # Both partitions expose the same GIN-friendly column. After roll-up, the
    # parent should be flagged once with a single payload suggestion.
    driver = FakeDriver(
        [
            _partition_row("event_2026", "event", "payload", "jsonb", seq_scan=4000, n_live_tup=120000),
            _partition_row("event_2027", "event", "payload", "jsonb", seq_scan=3000, n_live_tup=90000),
        ]
    )

    result = await recommend_indexes(driver)

    assert len(result) == 1
    assert result[0].table == "event"
    assert result[0].suggestions == [
        IndexSuggestion("payload", "gin", "GIN supports jsonb containment and key lookups")
    ]


async def test_recommend_indexes_rolls_partition_stats_up_to_the_parent() -> None:
    driver = FakeDriver(
        [
            _partition_row("event_2026", "event", "id", "integer", seq_scan=4000, n_live_tup=120000),
            _partition_row("event_2027", "event", "payload", "jsonb", seq_scan=3000, n_live_tup=90000),
        ]
    )

    result = await recommend_indexes(driver)

    assert result == [
        IndexRecommendation(
            schema="app",
            table="event",
            seq_scans=7000,
            live_tuples=210000,
            reason="partitioned table whose partitions are read mostly by sequential scan",
            suggestions=[IndexSuggestion("payload", "gin", "GIN supports jsonb containment and key lookups")],
            partitioned=True,
        )
    ]


@pytest.mark.parametrize("data_type", ["text", "character varying", "character"])
async def test_recommend_indexes_suggests_trigram_gin_for_text_columns(data_type: str) -> None:
    result = await recommend_indexes(FakeDriver([_row("name", data_type)]))

    assert result[0].suggestions == [
        IndexSuggestion("name", "gin_trgm", "trigram GIN (pg_trgm) accelerates LIKE/ILIKE pattern search")
    ]


async def test_recommend_indexes_suggests_gin_for_array_columns() -> None:
    result = await recommend_indexes(FakeDriver([_row("tags", "ARRAY")]))

    assert result[0].suggestions == [IndexSuggestion("tags", "gin", "GIN supports array membership queries")]


async def test_recommend_indexes_makes_no_suggestions_for_plain_scalar_columns() -> None:
    result = await recommend_indexes(FakeDriver([_row("id", "integer")]))

    assert result[0].suggestions == []


async def test_recommend_indexes_returns_empty_when_nothing_qualifies() -> None:
    assert await recommend_indexes(FakeDriver([])) == []


async def test_recommend_indexes_binds_the_threshold_as_a_parameter() -> None:
    driver = FakeDriver([])

    await recommend_indexes(driver, min_live_tuples=500)

    assert driver.calls[0][1] == [500]


async def test_recommend_indexes_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("recommend_indexes", {})

    assert result.isError is False


# --- recommend_index_drops -------------------------------------------------


from mcpg.indexing import (  # noqa: E402
    DROP_REASON_NEVER_USED,
    DROP_REASON_RARELY_USED,
    DROP_REASON_SCAN_NO_FETCH,
    IndexDropCandidate,
    recommend_index_drops,
)


def _drop_row(
    *,
    schema: str = "app",
    table: str = "orders",
    index: str = "idx_orders_customer",
    idx_scan: int = 0,
    idx_tup_read: int = 0,
    idx_tup_fetch: int = 0,
    size_bytes: int = 5_000_000,
    table_seq_scan: int = 0,
    table_idx_scan: int = 0,
    definition: str = "CREATE INDEX idx_orders_customer ON app.orders (customer_id)",
) -> dict[str, object]:
    return {
        "schemaname": schema,
        "table_name": table,
        "index_name": index,
        "idx_scan": idx_scan,
        "idx_tup_read": idx_tup_read,
        "idx_tup_fetch": idx_tup_fetch,
        "size_bytes": size_bytes,
        "definition": definition,
        "table_seq_scan": table_seq_scan,
        "table_idx_scan": table_idx_scan,
    }


async def test_recommend_index_drops_flags_never_used_indexes() -> None:
    driver = FakeDriver([_drop_row(idx_scan=0, idx_tup_fetch=0, size_bytes=10_000_000)])

    candidates = await recommend_index_drops(driver)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, IndexDropCandidate)
    assert candidate.reason_code == DROP_REASON_NEVER_USED
    assert candidate.index == "idx_orders_customer"
    assert candidate.size_bytes == 10_000_000
    assert candidate.drop_sql == 'DROP INDEX CONCURRENTLY "app"."idx_orders_customer";'
    assert "never been scanned" in candidate.rationale


async def test_recommend_index_drops_flags_scan_no_fetch_indexes() -> None:
    # Index is hit by the planner but reads and returns no rows — existence-
    # check pattern. The reason code escalates the operator's review.
    driver = FakeDriver([_drop_row(idx_scan=500, idx_tup_read=0, idx_tup_fetch=0, size_bytes=4_000_000)])

    candidates = await recommend_index_drops(driver)

    assert len(candidates) == 1
    assert candidates[0].reason_code == DROP_REASON_SCAN_NO_FETCH
    assert "zero" in candidates[0].rationale


async def test_recommend_index_drops_does_not_flag_covering_index_only_scans() -> None:
    # A covering index served entirely by INDEX-ONLY scans has idx_tup_fetch==0
    # (the heap is never touched) but idx_tup_read>0. It must NOT be flagged as
    # scan_no_fetch — dropping it would kill a working covering index. With no
    # rarely-used signal (it's the table's only scan activity) it drops out
    # entirely rather than getting a DROP recommendation.
    driver = FakeDriver([_drop_row(idx_scan=5_000, idx_tup_read=250_000, idx_tup_fetch=0, table_idx_scan=5_000)])

    candidates = await recommend_index_drops(driver)

    assert candidates == []


async def test_recommend_index_drops_flags_rarely_used_indexes() -> None:
    # Index is hit but represents <1% of the table's total scan
    # activity — the marginal-read-value case.
    driver = FakeDriver(
        [
            _drop_row(
                idx_scan=5,
                idx_tup_fetch=5,
                size_bytes=8_000_000,
                table_seq_scan=10_000,
                table_idx_scan=0,
            )
        ]
    )

    candidates = await recommend_index_drops(driver)

    assert len(candidates) == 1
    assert candidates[0].reason_code == DROP_REASON_RARELY_USED
    assert "5" in candidates[0].rationale  # the scan count


async def test_recommend_index_drops_skips_well_used_indexes() -> None:
    # The index is hit at >low_scan_ratio of total table activity —
    # no flag.
    driver = FakeDriver(
        [
            _drop_row(
                idx_scan=500,
                idx_tup_fetch=500,
                size_bytes=8_000_000,
                table_seq_scan=100,
                table_idx_scan=500,
            )
        ]
    )

    candidates = await recommend_index_drops(driver)

    assert candidates == []


async def test_recommend_index_drops_sorts_by_reason_strength_then_size() -> None:
    # never_used > scan_no_fetch > rarely_used; within a reason
    # bucket, larger indexes come first.
    driver = FakeDriver(
        [
            _drop_row(
                index="small_never_used",
                idx_scan=0,
                size_bytes=2_000_000,
            ),
            _drop_row(
                index="big_never_used",
                idx_scan=0,
                size_bytes=20_000_000,
            ),
            _drop_row(
                index="rarely",
                idx_scan=1,
                idx_tup_fetch=1,
                size_bytes=10_000_000,
                table_seq_scan=10_000,
                table_idx_scan=0,
            ),
            _drop_row(
                index="scan_no_fetch",
                idx_scan=200,
                idx_tup_fetch=0,
                size_bytes=5_000_000,
            ),
        ]
    )

    candidates = await recommend_index_drops(driver)

    assert [c.index for c in candidates] == [
        "big_never_used",
        "small_never_used",
        "scan_no_fetch",
        "rarely",
    ]


async def test_recommend_index_drops_respects_min_size_filter() -> None:
    # The SQL applies the >= min_size filter, so a small index never
    # reaches the classifier. Smoke the bound parameter.
    driver = FakeDriver([])

    await recommend_index_drops(driver, min_index_size_bytes=2_000_000)

    # The first arg in params is the min-size bound.
    assert driver.calls[0][1][0] == 2_000_000


async def test_recommend_index_drops_threads_schema_filter() -> None:
    driver = FakeDriver([])

    await recommend_index_drops(driver, schema="reporting", min_index_size_bytes=1_000_000)

    # The two-bound shape: [min_size, schema].
    assert driver.calls[0][1] == [1_000_000, "reporting"]
    assert "si.schemaname = %s" in driver.calls[0][0]


async def test_recommend_index_drops_escapes_double_quotes_in_identifiers() -> None:
    # Postgres permits literal ``"`` in quoted identifiers (e.g.
    # ``CREATE INDEX "weird""name" ON ...``). Emitted DROP SQL must
    # double-up the embedded quotes per the standard delimited-
    # identifier escape, or the statement is malformed (and at worst
    # could be coerced into something unintended).
    driver = FakeDriver(
        [
            _drop_row(
                schema='ten"ant',
                table="orders",
                index='idx"weird',
                idx_scan=0,
                size_bytes=5_000_000,
            )
        ]
    )

    candidates = await recommend_index_drops(driver)

    assert len(candidates) == 1
    drop_sql = candidates[0].drop_sql
    # The embedded ``"`` characters are doubled on emit.
    assert drop_sql == 'DROP INDEX CONCURRENTLY "ten""ant"."idx""weird";'
    # And the stored identifiers themselves are NOT pre-escaped — they
    # match what came back from the catalog.
    assert candidates[0].schema == 'ten"ant'
    assert candidates[0].index == 'idx"weird'


async def test_recommend_index_drops_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(FakeDriver([]))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "recommend_index_drops" in listed
        result = await client.call_tool("recommend_index_drops", {"schema": "public"})

    assert result.isError is False
