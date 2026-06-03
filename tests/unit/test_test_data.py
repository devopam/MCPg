"""Tests for the synthetic test-data factory (Phase 10.3)."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.test_data import (
    HARD_ROW_CAP,
    GeneratedDataset,
    TestDataError,
    _quote_literal,
    generate_test_data,
)


def test_quote_literal_handles_strings_and_special_values() -> None:
    assert _quote_literal(None) == "NULL"
    assert _quote_literal(True) == "TRUE"
    assert _quote_literal(False) == "FALSE"
    assert _quote_literal(42) == "42"
    assert _quote_literal(3.14) == "3.14"
    assert _quote_literal("hello") == "'hello'"
    # Embedded quote gets doubled.
    assert _quote_literal("it's") == "'it''s'"


async def test_generate_test_data_rejects_unsafe_schema() -> None:
    with pytest.raises(TestDataError, match="invalid schema"):
        await generate_test_data(
            FakeRoutingDriver({}),  # type: ignore[arg-type]
            schema='public"; DROP TABLE x',
            table="widget",
        )


async def test_generate_test_data_rejects_zero_rows() -> None:
    with pytest.raises(TestDataError, match="rows"):
        await generate_test_data(
            FakeRoutingDriver({}),  # type: ignore[arg-type]
            schema="public",
            table="widget",
            rows=0,
        )


async def test_generate_test_data_rejects_rows_above_hard_cap() -> None:
    with pytest.raises(TestDataError, match="hard cap"):
        await generate_test_data(
            FakeRoutingDriver({}),  # type: ignore[arg-type]
            schema="public",
            table="widget",
            rows=HARD_ROW_CAP + 1,
        )


async def test_generate_test_data_raises_when_table_has_no_columns() -> None:
    driver = FakeRoutingDriver({"pg_attribute": []})
    with pytest.raises(TestDataError, match="no columns"):
        await generate_test_data(driver, schema="public", table="widget")  # type: ignore[arg-type]


async def test_generate_test_data_emits_insert_statements_for_supported_types() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_attribute": [
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "nullable": False,
                    "column_default": None,
                    "type_name": "int4",
                    "type_mod": -1,
                },
                {
                    "column_name": "name",
                    "data_type": "text",
                    "nullable": False,
                    "column_default": None,
                    "type_name": "text",
                    "type_mod": -1,
                },
                {
                    "column_name": "active",
                    "data_type": "boolean",
                    "nullable": False,
                    "column_default": None,
                    "type_name": "bool",
                    "type_mod": -1,
                },
            ]
        }
    )

    result = await generate_test_data(
        driver,  # type: ignore[arg-type]
        schema="public",
        table="widget",
        rows=3,
        seed=42,
    )

    assert isinstance(result, GeneratedDataset)
    assert result.rows_generated == 3
    assert len(result.statements) == 3
    # Each statement targets the correct relation and includes all columns.
    for stmt in result.statements:
        assert stmt.startswith('INSERT INTO "public"."widget"')
        assert '"id"' in stmt and '"name"' in stmt and '"active"' in stmt


async def test_generate_test_data_is_deterministic_for_a_given_seed() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_attribute": [
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "nullable": False,
                    "column_default": None,
                    "type_name": "int4",
                    "type_mod": -1,
                }
            ]
        }
    )

    a = await generate_test_data(driver, "public", "widget", rows=5, seed=123)  # type: ignore[arg-type]
    b = await generate_test_data(driver, "public", "widget", rows=5, seed=123)  # type: ignore[arg-type]

    assert a.statements == b.statements


async def test_generate_test_data_skips_unsupported_column_types() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_attribute": [
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "nullable": False,
                    "column_default": None,
                    "type_name": "int4",
                    "type_mod": -1,
                },
                {
                    "column_name": "shape",
                    "data_type": "geometry",
                    "nullable": True,
                    "column_default": None,
                    "type_name": "geometry",
                    "type_mod": -1,
                },
            ]
        }
    )

    result = await generate_test_data(driver, "public", "widget", rows=2)  # type: ignore[arg-type]

    # `shape` (geometry) was skipped; only `id` survives.
    assert "shape" in result.skipped_columns
    assert all('"id"' in stmt and '"shape"' not in stmt for stmt in result.statements)


async def test_seed_table_with_sample_data_generates_and_executes_inserts() -> None:
    from mcpg.test_data import seed_table_with_sample_data

    driver = FakeRoutingDriver(
        {
            "pg_attribute": [
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "nullable": False,
                    "column_default": None,
                    "type_name": "int4",
                    "type_mod": -1,
                }
            ]
        }
    )

    result = await seed_table_with_sample_data(
        driver,  # type: ignore[arg-type]
        schema="public",
        table="widget",
        rows=2,
        seed=42,
    )

    assert result.rows_seeded == 2
    assert len(result.statements_executed) == 2
    assert len(driver.calls) == 2  # 1 for describe_table, 1 for batched insert
    assert driver.calls[1][0].count('INSERT INTO "public"."widget"') == 2
