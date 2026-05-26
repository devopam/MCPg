"""Integration tests for the jOOQ configuration exporter against real PG."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.jooq import generate_jooq_config

_SCHEMA = "mcpg_jooq_it"


@pytest.fixture
async def jooq_schema(connected_database: Database) -> AsyncIterator[str]:
    driver = connected_database.driver()
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {_SCHEMA}")
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.owner (id serial PRIMARY KEY, name text NOT NULL, profile jsonb)"
    )
    await driver.execute_query(
        f"CREATE TABLE {_SCHEMA}.widget ("
        "id serial PRIMARY KEY, "
        f"owner_id integer NOT NULL REFERENCES {_SCHEMA}.owner(id), "
        "attrs json)"
    )
    try:
        yield _SCHEMA
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


async def test_generate_jooq_config_emits_parseable_xml_with_expected_includes(
    connected_database: Database, jooq_schema: str
) -> None:
    xml_text = await generate_jooq_config(connected_database.driver(), jooq_schema, target_package="io.example.app")

    # Output must be parseable XML — that's the contract jOOQ depends on.
    root = ET.fromstring(xml_text)
    # ElementTree exposes the namespaced element name; check by suffix.
    assert root.tag.endswith("configuration")

    # Includes regex names BOTH tables, anchored to schema.
    assert f"{jooq_schema}\\.owner" in xml_text
    assert f"{jooq_schema}\\.widget" in xml_text
    # Excludes covers MCPg's audit + migrations schemas.
    assert "mcpg_audit" in xml_text
    assert "mcpg_migrations" in xml_text
    # Target package is the override the caller supplied.
    assert "<packageName>io.example.app</packageName>" in xml_text
    # Both JSON columns generated forced-type entries.
    assert f"{jooq_schema}\\.owner\\.profile" in xml_text
    assert f"{jooq_schema}\\.widget\\.attrs" in xml_text
    assert "org.jooq.JSONB" in xml_text  # for jsonb
    assert "org.jooq.JSON" in xml_text  # for json


async def test_generate_jooq_config_for_empty_schema_emits_empty_includes(
    connected_database: Database,
) -> None:
    driver = connected_database.driver()
    schema = "mcpg_jooq_empty_it"
    await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await driver.execute_query(f"CREATE SCHEMA {schema}")
    try:
        xml_text = await generate_jooq_config(driver, schema)
        # Still parseable, just with an empty includes regex.
        ET.fromstring(xml_text)
        assert "<includes></includes>" in xml_text
        # No forcedTypes block when no JSON columns exist.
        assert "<forcedTypes>" not in xml_text
    finally:
        await driver.execute_query(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
