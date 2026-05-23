"""Integration tests for the Mermaid ER diagram generator."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.diagrams import generate_schema_diagram

_SCHEMA = "mcpg_diagrams_it"


@pytest.fixture
async def diagram_schema(connected_database: Database) -> AsyncIterator[str]:
    """Create a small two-table schema with a real foreign key."""
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(f"CREATE TABLE {_SCHEMA}.widget (id integer PRIMARY KEY, name text NOT NULL)")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.order_item ("
        f"  id integer PRIMARY KEY, "
        f"  widget_id integer NOT NULL REFERENCES {_SCHEMA}.widget(id)"
        f")"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_schema_diagram_renders_entities_and_fk_edge(
    connected_database: Database, diagram_schema: str
) -> None:
    rendered = await generate_schema_diagram(connected_database.driver(), diagram_schema)

    assert rendered.startswith("erDiagram\n")
    assert "widget {" in rendered
    assert "order_item {" in rendered
    assert "integer id PK" in rendered
    assert "integer widget_id" in rendered and "FK" in rendered
    assert "widget ||--o{ order_item" in rendered
