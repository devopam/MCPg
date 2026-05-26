"""Integration tests for the Ent (Go) schema exporter against real PG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.ent import generate_ent_schemas

_SCHEMA = "mcpg_ent_it"


@pytest.fixture
async def ent_schema(connected_database: Database) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(f"CREATE TYPE {_SCHEMA}.status AS ENUM ('active','inactive')")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.owner ("
        "id serial PRIMARY KEY, "
        "name text NOT NULL, "
        "created_at timestamptz DEFAULT now())"
    )
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget ("
        "id serial PRIMARY KEY, "
        f"owner_id integer NOT NULL REFERENCES {_SCHEMA}.owner(id), "
        f"state {_SCHEMA}.status NOT NULL DEFAULT 'active', "
        "extras jsonb)"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_ent_schemas_emits_one_go_file_per_table(connected_database: Database, ent_schema: str) -> None:
    files = await generate_ent_schemas(connected_database.driver(), ent_schema)

    # One file per base table.
    assert set(files.keys()) == {"owner.go", "widget.go"}

    owner = files["owner.go"]
    widget = files["widget.go"]

    # Owner: ent.Schema struct, Fields() returning field.X(...) calls,
    # time.Now default for created_at (timestamp), nullable wrapped.
    assert "package schema" in owner
    assert "type Owner struct {" in owner
    assert "ent.Schema" in owner
    assert 'field.Int("id")' in owner
    assert 'field.Text("name")' in owner
    assert 'field.Time("created_at")' in owner
    assert ".Default(time.Now)" in owner
    assert '"time"' in owner

    # Widget: same shape PLUS Edges() with the FK edge AND field.Enum
    # for the state column.
    assert "type Widget struct {" in widget
    assert "func (Widget) Fields()" in widget
    assert "func (Widget) Edges()" in widget
    assert 'edge.To("owner", Owner.Type).Unique().Field("owner_id")' in widget
    assert 'field.Enum("state").Values("active", "inactive")' in widget
    assert 'field.JSON("extras"' in widget
    # Nullable JSONB column gets .Optional().
    assert ".Optional()" in widget
