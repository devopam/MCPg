"""Integration tests for the Ecto (Elixir) schema exporter against real PG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.ecto import generate_ecto_schemas

_SCHEMA = "mcpg_ecto_it"


@pytest.fixture
async def ecto_schema(connected_database: Database) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.users ("
        "id serial PRIMARY KEY, "
        "email text NOT NULL UNIQUE, "
        "inserted_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now())"
    )
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widgets ("
        "id serial PRIMARY KEY, "
        f"user_id integer NOT NULL REFERENCES {_SCHEMA}.users(id), "
        "name text NOT NULL, "
        "metadata jsonb)"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_ecto_schemas_emits_one_module_per_table_with_singular_filenames(
    connected_database: Database, ecto_schema: str
) -> None:
    files = await generate_ecto_schemas(connected_database.driver(), ecto_schema, app_module="Shop")

    # File names are singularised — users.ex → user.ex, widgets.ex → widget.ex.
    assert set(files.keys()) == {"user.ex", "widget.ex"}

    user = files["user.ex"]
    widget = files["widget.ex"]

    # User module: PascalCase + singular module name, schema points at PLURAL table.
    assert "defmodule Shop.User do" in user
    assert "use Ecto.Schema" in user
    assert "@primary_key {:id, :id, autogenerate: true}" in user
    assert 'schema "users" do' in user
    assert "field :email, :string" in user
    # Both timestamp columns → timestamps() macro; fields NOT redeclared.
    assert "timestamps()" in user
    assert "field :inserted_at" not in user
    assert "field :updated_at" not in user

    # Widget module: belongs_to + skip user_id field.
    assert "defmodule Shop.Widget do" in widget
    assert 'schema "widgets" do' in widget
    assert "belongs_to :user, Shop.User, foreign_key: :user_id" in widget
    # user_id is covered by belongs_to — must NOT be redeclared as a field.
    assert "field :user_id" not in widget
    assert "field :name, :string" in widget
    # jsonb → :map
    assert "field :metadata, :map" in widget
