"""Tests for the realistic single-row factory (roadmap 8.1)."""

from __future__ import annotations

import pytest
from _fakes import FakeRoutingDriver

from mcpg.test_row_factory import (
    ColumnFill,
    GeneratedTestRow,
    TestRowFactoryError,
    generate_test_row_for,
)


def _column(name: str, *, data_type: str, nullable: bool = False, default: str | None = None) -> dict[str, object]:
    """Construct a row dict shaped like ``describe_table``'s catalogue read."""
    return {
        "column_name": name,
        "data_type": data_type,
        "nullable": nullable,
        "column_default": default,
        "type_name": data_type,
        "type_mod": 0,
    }


def _full_routes(
    *,
    columns: list[dict[str, object]],
    identity_names: list[str] | None = None,
    fks: list[dict[str, object]] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Wire all three catalogue queries (describe, identity, FK) in one go."""
    return {
        "FROM pg_attribute a": columns,
        "is_identity = 'YES'": [{"column_name": n} for n in (identity_names or [])],
        "constraint_type = 'FOREIGN KEY'": fks or [],
    }


# ---------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------


async def test_rejects_invalid_schema_name() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(TestRowFactoryError, match="schema"):
        await generate_test_row_for(driver, "bad; DROP", "t")  # type: ignore[arg-type]


async def test_rejects_invalid_table_name() -> None:
    driver = FakeRoutingDriver({})
    with pytest.raises(TestRowFactoryError, match="table"):
        await generate_test_row_for(driver, "public", '"; DROP TABLE x; --')  # type: ignore[arg-type]


async def test_errors_when_table_has_no_columns() -> None:
    driver = FakeRoutingDriver(_full_routes(columns=[]))
    with pytest.raises(TestRowFactoryError, match="no columns"):
        await generate_test_row_for(driver, "public", "missing")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Identity / generated columns are skipped
# ---------------------------------------------------------------------------


async def test_identity_column_lands_as_default() -> None:
    routes = _full_routes(
        columns=[_column("id", data_type="bigint"), _column("name", data_type="text")],
        identity_names=["id"],
    )
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "users", seed=42)  # type: ignore[arg-type]
    id_fill = next(c for c in row.columns if c.name == "id")
    assert id_fill.sql_literal == "DEFAULT"
    assert "identity" in id_fill.heuristic
    assert '"id"' not in row.insert_sql
    assert '"name"' in row.insert_sql


# ---------------------------------------------------------------------------
# Name-based heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("col_name", "expected_substring"),
    [
        ("email", "@example.com"),
        ("user_email", "@example.com"),
        ("phone", "+1-555-"),
        ("avatar_url", "https://example.com/r/"),
        ("country_code", None),
        ("first_name", None),
        ("last_name", None),
        ("full_name", None),
        ("created_at", None),
    ],
)
async def test_name_pattern_drives_value(col_name: str, expected_substring: str | None) -> None:
    routes = _full_routes(columns=[_column(col_name, data_type="text")])
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "t", seed=1)  # type: ignore[arg-type]
    fill = next(c for c in row.columns if c.name == col_name)
    assert fill.heuristic.endswith("pattern")
    if expected_substring is not None:
        assert expected_substring in fill.sql_literal


# ---------------------------------------------------------------------------
# Type-based fallthrough
# ---------------------------------------------------------------------------


async def test_type_based_synthesis_uses_int_for_int_columns() -> None:
    routes = _full_routes(columns=[_column("score", data_type="integer")])
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "t", seed=7)  # type: ignore[arg-type]
    fill = next(c for c in row.columns if c.name == "score")
    assert fill.heuristic == "int type"
    assert fill.sql_literal.isdigit()


@pytest.mark.parametrize(("decl_type", "cap"), [("character varying(2)", 2), ("varchar(5)", 5), ("char(3)", 3)])
async def test_text_type_respects_length_cap(decl_type: str, cap: int) -> None:
    """varchar(N) / char(N) columns get a synth string whose body
    length is ≤ N. The bulk INSERT would otherwise fail with
    "value too long for type" on a real cluster."""
    routes = _full_routes(columns=[_column("code", data_type=decl_type)])
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "t", seed=1)  # type: ignore[arg-type]
    fill = next(c for c in row.columns if c.name == "code")
    assert fill.heuristic == "text type"
    # Strip the wrapping single quotes — the body must fit in `cap`.
    body = fill.sql_literal.strip("'")
    assert 1 <= len(body) <= cap, f"got {body!r} ({len(body)} chars) for cap={cap}"


async def test_unsupported_type_with_nullable_lands_as_null() -> None:
    routes = _full_routes(columns=[_column("geom", data_type="geometry", nullable=True)])
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "t", seed=1)  # type: ignore[arg-type]
    fill = next(c for c in row.columns if c.name == "geom")
    assert fill.sql_literal == "NULL"
    assert "unsupported" in fill.heuristic


async def test_unsupported_type_with_default_lands_as_default() -> None:
    routes = _full_routes(columns=[_column("loc", data_type="geography", default="ST_GeomFromText('POINT(0 0)')")])
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "t", seed=1)  # type: ignore[arg-type]
    fill = next(c for c in row.columns if c.name == "loc")
    assert fill.sql_literal == "DEFAULT"


async def test_unsupported_not_null_no_default_raises() -> None:
    routes = _full_routes(columns=[_column("loc", data_type="geometry")])
    driver = FakeRoutingDriver(routes)
    with pytest.raises(TestRowFactoryError, match="NOT NULL"):
        await generate_test_row_for(driver, "public", "t", seed=1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Foreign-key resolution
# ---------------------------------------------------------------------------


async def test_fk_column_samples_referenced_value() -> None:
    routes = _full_routes(
        columns=[_column("user_id", data_type="bigint")],
        fks=[
            {
                "local_column": "user_id",
                "ref_schema": "public",
                "ref_table": "users",
                "ref_column": "id",
            }
        ],
    )
    routes['SELECT "id" FROM "public"."users"'] = [{"id": 99}]
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "orders", seed=1)  # type: ignore[arg-type]
    fill = next(c for c in row.columns if c.name == "user_id")
    assert fill.sql_literal == "99"
    assert "fk →" in fill.heuristic
    assert "public.users.id sampled" in fill.heuristic


async def test_composite_fk_samples_one_consistent_row() -> None:
    """Composite FK (a, b) REFERENCES t(x, y) must draw both values
    from the SAME row in t — sampling them via separate LIMIT 1
    queries could land different rows under concurrent writes.
    Regression for gemini review on #178."""
    routes = _full_routes(
        columns=[
            _column("user_id", data_type="bigint"),
            _column("tenant_id", data_type="text"),
        ],
        fks=[
            {
                "local_column": "user_id",
                "ref_schema": "public",
                "ref_table": "users",
                "ref_column": "id",
            },
            {
                "local_column": "tenant_id",
                "ref_schema": "public",
                "ref_table": "users",
                "ref_column": "tenant_id",
            },
        ],
    )
    # ONE query against users returns BOTH columns from the same row.
    routes['SELECT "id", "tenant_id" FROM "public"."users"'] = [{"id": 99, "tenant_id": "tenant-A"}]
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "orders", seed=1)  # type: ignore[arg-type]
    user_fill = next(c for c in row.columns if c.name == "user_id")
    tenant_fill = next(c for c in row.columns if c.name == "tenant_id")
    assert user_fill.sql_literal == "99"
    assert tenant_fill.sql_literal == "'tenant-A'"
    # Both columns must have come from the single composite SELECT,
    # not two separate per-column SELECTs.
    select_calls = [call for call in driver.calls if 'FROM "public"."users"' in call[0]]
    assert len(select_calls) == 1, (
        f"expected ONE composite sample query against users, got {len(select_calls)}: {[c[0] for c in select_calls]}"
    )


async def test_fk_to_empty_table_with_default_lands_as_default() -> None:
    routes = _full_routes(
        columns=[_column("user_id", data_type="bigint", default="1")],
        fks=[
            {
                "local_column": "user_id",
                "ref_schema": "public",
                "ref_table": "users",
                "ref_column": "id",
            }
        ],
    )
    # No route for the SELECT — driver returns []; FK sample is None.
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "orders", seed=1)  # type: ignore[arg-type]
    fill = next(c for c in row.columns if c.name == "user_id")
    assert fill.sql_literal == "DEFAULT"
    assert "empty" in fill.heuristic


async def test_fk_to_empty_table_not_null_no_default_raises() -> None:
    routes = _full_routes(
        columns=[_column("user_id", data_type="bigint")],
        fks=[
            {
                "local_column": "user_id",
                "ref_schema": "public",
                "ref_table": "users",
                "ref_column": "id",
            }
        ],
    )
    driver = FakeRoutingDriver(routes)
    with pytest.raises(TestRowFactoryError, match="empty"):
        await generate_test_row_for(driver, "public", "orders", seed=1)  # type: ignore[arg-type]


async def test_follow_foreign_keys_false_skips_fk_query() -> None:
    """When follow_foreign_keys=False, FK columns get synth-from-type only."""
    routes = _full_routes(columns=[_column("user_id", data_type="bigint")])
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "orders", seed=1, follow_foreign_keys=False)  # type: ignore[arg-type]
    fill = next(c for c in row.columns if c.name == "user_id")
    assert fill.heuristic == "int type"
    assert not any("constraint_type = 'FOREIGN KEY'" in call[0] for call in driver.calls)


# ---------------------------------------------------------------------------
# INSERT SQL shape
# ---------------------------------------------------------------------------


async def test_insert_sql_quotes_identifiers() -> None:
    routes = _full_routes(columns=[_column("email", data_type="text")])
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "users", seed=1)  # type: ignore[arg-type]
    assert row.insert_sql.startswith('INSERT INTO "public"."users" ("email") VALUES')


async def test_all_columns_skipped_emits_default_values() -> None:
    routes = _full_routes(columns=[_column("id", data_type="bigint")], identity_names=["id"])
    driver = FakeRoutingDriver(routes)
    row = await generate_test_row_for(driver, "public", "t", seed=1)  # type: ignore[arg-type]
    assert row.insert_sql == 'INSERT INTO "public"."t" DEFAULT VALUES'


# ---------------------------------------------------------------------------
# Determinism + dataclass shape
# ---------------------------------------------------------------------------


async def test_seed_makes_output_deterministic() -> None:
    routes = _full_routes(columns=[_column("name", data_type="text")])
    driver = FakeRoutingDriver(routes)
    row_a = await generate_test_row_for(driver, "public", "t", seed=42)  # type: ignore[arg-type]
    row_b = await generate_test_row_for(driver, "public", "t", seed=42)  # type: ignore[arg-type]
    assert row_a.insert_sql == row_b.insert_sql


async def test_returned_dataclasses_are_frozen() -> None:
    fill = ColumnFill(name="x", sql_literal="NULL", heuristic="t")
    with pytest.raises((AttributeError, Exception)):
        fill.name = "y"  # type: ignore[misc]
    row = GeneratedTestRow(schema="s", table="t", columns=[fill], insert_sql="x")
    with pytest.raises((AttributeError, Exception)):
        row.schema = "z"  # type: ignore[misc]
