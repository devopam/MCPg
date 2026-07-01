"""Integration tests for the SQLAlchemy 2.0 exporter against real PG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.sqlalchemy_export import generate_sqlalchemy_models

_SCHEMA = "mcpg_sa_it"


@pytest.fixture
async def sa_schema(connected_database: Database, distributed_replicated_clause: str) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.status AS ENUM ('active','inactive')")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.owner ("
        "id serial PRIMARY KEY, "
        "name varchar(120) NOT NULL, "
        "created_at timestamptz DEFAULT now())"
    )
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget ("
        "id serial PRIMARY KEY, "
        f"owner_id integer NOT NULL REFERENCES {_SCHEMA}.owner(id), "
        "name text NOT NULL UNIQUE, "
        "quantity integer NOT NULL DEFAULT 0, "
        f"state {_SCHEMA}.status NOT NULL DEFAULT 'active', "
        "extras jsonb)"
        f"{distributed_replicated_clause}"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_sqlalchemy_models_emits_valid_python_for_a_real_schema(
    connected_database: Database, sa_schema: str
) -> None:
    py = await generate_sqlalchemy_models(connected_database.driver(), sa_schema)

    # Header imports — the relevant chunks must be present.
    assert "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column" in py
    assert "from sqlalchemy import" in py
    assert "from sqlalchemy.dialects.postgresql import JSONB" in py
    assert "from datetime import datetime" in py
    assert "from typing import Optional" in py
    assert "import enum" in py

    # Base + enum class generated.
    assert "class Base(DeclarativeBase):" in py
    assert "class Status(enum.Enum):" in py
    assert '    active = "active"' in py

    # Both tables present as PascalCase classes.
    assert "class Owner(Base):" in py
    assert "class Widget(Base):" in py
    assert '__tablename__ = "owner"' in py
    assert f'__table_args__ = {{"schema": "{sa_schema}"}}' in py

    # FK references schema.table.column.
    assert f'ForeignKey("{sa_schema}.owner.id")' in py

    # Type mappings reach the columns.
    assert "Mapped[int]" in py
    assert "Mapped[str]" in py
    assert "Mapped[Optional[datetime]]" in py
    assert "Mapped[Status]" in py
    assert "Mapped[Optional[dict]]" in py

    # The output should be valid Python (compile, don't import — the
    # SQLAlchemy class machinery requires a registered Base which we
    # supply, but compilation alone catches syntax errors that would
    # block an agent from using the file).
    compile(py, "<generated>", "exec")


async def test_generate_sqlalchemy_models_for_an_empty_schema_emits_just_the_base_class(
    connected_database: Database,
) -> None:
    driver = connected_database.driver()
    schema = "mcpg_sa_empty_it"
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {schema}")
    try:
        py = await generate_sqlalchemy_models(driver, schema)
        assert "class Base(DeclarativeBase):" in py
        # No model classes emitted.
        assert "class Owner" not in py
        # Still compiles.
        compile(py, "<generated>", "exec")
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
