"""Tests for the logical replication management writes bundle (roadmap 2.1)."""

from __future__ import annotations

import pytest
from _fakes import FakeDatabase, FakeDriver

from mcpg.logical_replication import (
    LogicalReplicationError,
    create_publication,
    create_subscription,
    drop_publication,
    drop_subscription,
)

# ---------------------------------------------------------------------------
# create_publication
# ---------------------------------------------------------------------------


async def test_create_publication_for_all_tables_emits_for_all_tables_clause() -> None:
    db = FakeDatabase(FakeDriver())
    result = await create_publication(db, name="full_pub", all_tables=True)  # type: ignore[arg-type]
    assert result.all_tables is True
    assert result.tables == []
    assert result.executed_sql == 'CREATE PUBLICATION "full_pub" FOR ALL TABLES'
    assert db.unmanaged == [result.executed_sql]


async def test_create_publication_for_explicit_tables_quotes_each_piece() -> None:
    """schema and table names land in the SQL as quoted identifiers,
    even when they're plain lowercase — keeps the rendered SQL legal
    for any caller-supplied name."""
    db = FakeDatabase(FakeDriver())
    result = await create_publication(
        db,  # type: ignore[arg-type]
        name="orders_pub",
        tables=("public.orders", "public.line_items"),
    )
    assert result.executed_sql == 'CREATE PUBLICATION "orders_pub" FOR TABLE "public"."orders", "public"."line_items"'
    assert result.tables == ["public.orders", "public.line_items"]


async def test_create_publication_rejects_both_all_tables_and_tables() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError, match="either"):
        await create_publication(
            db,  # type: ignore[arg-type]
            name="bad",
            all_tables=True,
            tables=("public.t",),
        )


async def test_create_publication_rejects_neither_all_tables_nor_tables() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError, match="must specify"):
        await create_publication(db, name="bad")  # type: ignore[arg-type]


async def test_create_publication_rejects_invalid_publication_name() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError, match="invalid publication"):
        await create_publication(
            db,  # type: ignore[arg-type]
            name='pub"; DROP TABLE x; --',
            all_tables=True,
        )


async def test_create_publication_rejects_unqualified_table_name() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError, match=r"schema\.table"):
        await create_publication(
            db,  # type: ignore[arg-type]
            name="bad",
            tables=("orders",),  # no schema prefix
        )


async def test_create_publication_rejects_injection_in_table_name() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError, match="invalid"):
        await create_publication(
            db,  # type: ignore[arg-type]
            name="bad",
            tables=('public.t"; DROP TABLE x; --',),
        )


async def test_create_publication_surfaces_driver_failure_as_typed_error() -> None:
    db = FakeDatabase(FakeDriver(), unmanaged_fail=True)
    with pytest.raises(LogicalReplicationError, match="create publication failed"):
        await create_publication(db, name="x", all_tables=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# drop_publication
# ---------------------------------------------------------------------------


async def test_drop_publication_basic() -> None:
    db = FakeDatabase(FakeDriver())
    result = await drop_publication(db, name="x")  # type: ignore[arg-type]
    assert result.executed_sql == 'DROP PUBLICATION "x"'


async def test_drop_publication_with_if_exists_and_cascade() -> None:
    db = FakeDatabase(FakeDriver())
    result = await drop_publication(  # type: ignore[arg-type]
        db, name="x", if_exists=True, cascade=True
    )
    assert result.executed_sql == 'DROP PUBLICATION IF EXISTS "x" CASCADE'


async def test_drop_publication_rejects_invalid_name() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError):
        await drop_publication(db, name="0bad")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# create_subscription
# ---------------------------------------------------------------------------


async def test_create_subscription_basic_renders_canonical_sql() -> None:
    db = FakeDatabase(FakeDriver())
    result = await create_subscription(  # type: ignore[arg-type]
        db,
        name="sub1",
        connection_string="host=publisher dbname=app user=replicator",
        publications=("pub1",),
    )
    assert result.executed_sql == (
        'CREATE SUBSCRIPTION "sub1" CONNECTION \'host=publisher dbname=app user=replicator\' PUBLICATION "pub1"'
    )
    # Defaults: enabled=True, copy_data=True, create_slot=True → no WITH clause
    assert "WITH" not in result.executed_sql
    # Connection string is not echoed to the result.
    assert "publisher" not in str(result)


async def test_create_subscription_with_options_renders_with_clause() -> None:
    db = FakeDatabase(FakeDriver())
    result = await create_subscription(  # type: ignore[arg-type]
        db,
        name="sub2",
        connection_string="host=p dbname=d user=u",
        publications=("p1", "p2"),
        enabled=False,
        copy_data=False,
        create_slot=False,
        slot_name="my_slot",
        synchronous_commit="remote_apply",
    )
    sql = result.executed_sql
    assert 'PUBLICATION "p1", "p2"' in sql
    assert "WITH (" in sql
    assert "enabled = false" in sql
    assert "copy_data = false" in sql
    assert "create_slot = false" in sql
    assert 'slot_name = "my_slot"' in sql
    assert "synchronous_commit = 'remote_apply'" in sql


async def test_create_subscription_escapes_single_quotes_in_connection_string() -> None:
    """libpq DSNs can legitimately contain single quotes inside quoted
    values; doubling them is the SQL-literal escape."""
    db = FakeDatabase(FakeDriver())
    result = await create_subscription(  # type: ignore[arg-type]
        db,
        name="sub3",
        connection_string="host=p dbname=d password='with''quote'",
        publications=("p1",),
    )
    # Single quotes inside the DSN should be doubled in the rendered SQL.
    assert "password=''with''''quote''" in result.executed_sql


async def test_create_subscription_rejects_empty_publications() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError, match="at least one"):
        await create_subscription(  # type: ignore[arg-type]
            db, name="x", connection_string="host=p", publications=()
        )


async def test_create_subscription_rejects_invalid_publication_name() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError, match="invalid publication"):
        await create_subscription(  # type: ignore[arg-type]
            db,
            name="x",
            connection_string="host=p",
            publications=('pub"; DROP TABLE y',),
        )


async def test_create_subscription_rejects_unknown_synchronous_commit() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError, match="synchronous_commit"):
        await create_subscription(  # type: ignore[arg-type]
            db,
            name="x",
            connection_string="host=p",
            publications=("p1",),
            synchronous_commit="bogus",
        )


# ---------------------------------------------------------------------------
# drop_subscription
# ---------------------------------------------------------------------------


async def test_drop_subscription_basic() -> None:
    db = FakeDatabase(FakeDriver())
    result = await drop_subscription(db, name="sub1")  # type: ignore[arg-type]
    assert result.executed_sql == 'DROP SUBSCRIPTION "sub1"'


async def test_drop_subscription_if_exists() -> None:
    db = FakeDatabase(FakeDriver())
    result = await drop_subscription(  # type: ignore[arg-type]
        db, name="sub1", if_exists=True
    )
    assert result.executed_sql == 'DROP SUBSCRIPTION IF EXISTS "sub1"'


async def test_drop_subscription_rejects_invalid_name() -> None:
    db = FakeDatabase(FakeDriver())
    with pytest.raises(LogicalReplicationError):
        await drop_subscription(db, name=" bad name ")  # type: ignore[arg-type]


async def test_drop_subscription_surfaces_driver_failure() -> None:
    db = FakeDatabase(FakeDriver(), unmanaged_fail=True)
    with pytest.raises(LogicalReplicationError, match="drop subscription failed"):
        await drop_subscription(db, name="x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Frozen dataclass immutability
# ---------------------------------------------------------------------------


async def test_all_result_dataclasses_are_immutable() -> None:
    db = FakeDatabase(FakeDriver())
    result = await create_publication(db, name="x", all_tables=True)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        result.name = "modified"  # type: ignore[misc]
