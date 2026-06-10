"""Tests for the pg_search BM-1 observability surface."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.extensions import ENABLEABLE_EXTENSIONS
from mcpg.pg_search import (
    PgSearchError,
    PgSearchIndexInfo,
    get_pg_search_index_metadata,
    list_pg_search_indexes,
)
from mcpg.server import create_server

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# Fixture mirroring the 13 documented bm25 reloptions from
# pg_search/src/api/index.rs IndexOptions (BM-0 §2.2 checkpoint).
# Reloptions reach Python as text[] with JSONB values pre-stringified.
_RELOPTIONS = [
    "key_field=id",
    'text_fields={"body": {"tokenizer": "default"}}',
    'numeric_fields={"price": {"fast": true}}',
    'boolean_fields={"active": {}}',
    'json_fields={"meta": {}}',
    'range_fields={"interval": {}}',
    'datetime_fields={"created_at": {}}',
    "layer_sizes=100,1000,10000",
    "background_layer_sizes=10000,100000",
    "target_segment_count=8",
    "mutable_segment_rows=10000",
    "sort_by=id",
    'search_tokenizer={"type": "default"}',
]


# --- list_pg_search_indexes -------------------------------------------------


async def test_list_pg_search_indexes_returns_empty_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    assert await list_pg_search_indexes(driver) == []  # type: ignore[arg-type]


async def test_list_pg_search_indexes_maps_rows_when_extension_present() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                {
                    "schema": "public",
                    "index": "docs_bm25_idx",
                    "table": "docs",
                    "columns": ["id", "body"],
                    "reloptions": _RELOPTIONS,
                }
            ],
        }
    )

    infos = await list_pg_search_indexes(driver)  # type: ignore[arg-type]

    assert len(infos) == 1
    info = infos[0]
    assert info.schema == "public"
    assert info.index == "docs_bm25_idx"
    assert info.table == "docs"
    assert info.columns == ["id", "body"]
    assert info.key_field == "id"
    assert info.text_fields == {"body": {"tokenizer": "default"}}
    assert info.numeric_fields == {"price": {"fast": True}}
    assert info.boolean_fields == {"active": {}}
    assert info.json_fields == {"meta": {}}
    assert info.range_fields == {"interval": {}}
    assert info.datetime_fields == {"created_at": {}}
    assert info.layer_sizes == "100,1000,10000"
    assert info.background_layer_sizes == "10000,100000"
    assert info.target_segment_count == 8
    assert info.mutable_segment_rows == 10000
    assert info.sort_by == "id"
    assert info.search_tokenizer == {"type": "default"}
    # Full parsed dict carries every documented key.
    assert set(info.index_options.keys()) == {
        "key_field",
        "text_fields",
        "numeric_fields",
        "boolean_fields",
        "json_fields",
        "range_fields",
        "datetime_fields",
        "layer_sizes",
        "background_layer_sizes",
        "target_segment_count",
        "mutable_segment_rows",
        "sort_by",
        "search_tokenizer",
    }


async def test_list_pg_search_indexes_tolerates_partial_reloptions() -> None:
    # Only the required `key_field` set; everything else should fall back
    # to its dataclass default rather than raise.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                {
                    "schema": "public",
                    "index": "docs_bm25_idx",
                    "table": "docs",
                    "columns": ["id"],
                    "reloptions": ["key_field=id"],
                }
            ],
        }
    )

    infos = await list_pg_search_indexes(driver)  # type: ignore[arg-type]

    assert infos == [
        PgSearchIndexInfo(
            schema="public",
            index="docs_bm25_idx",
            table="docs",
            columns=["id"],
            key_field="id",
            index_options={"key_field": "id"},
        )
    ]


async def test_list_pg_search_indexes_tolerates_null_reloptions() -> None:
    # PG returns NULL for reloptions when no WITH clause is provided.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                {
                    "schema": "public",
                    "index": "docs_bm25_idx",
                    "table": "docs",
                    "columns": ["id"],
                    "reloptions": None,
                }
            ],
        }
    )

    infos = await list_pg_search_indexes(driver)  # type: ignore[arg-type]

    assert infos[0].index_options == {}
    assert infos[0].key_field is None
    assert infos[0].text_fields == {}


async def test_list_pg_search_indexes_skips_malformed_reloption_entries() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                {
                    "schema": "public",
                    "index": "docs_bm25_idx",
                    "table": "docs",
                    "columns": ["id"],
                    # No '=', empty key, junk JSON — every entry should be
                    # silently skipped or recoverable.
                    "reloptions": [
                        "key_field=id",
                        "no_equals_sign",
                        "=no_key",
                        "text_fields=not-valid-json",
                    ],
                }
            ],
        }
    )

    infos = await list_pg_search_indexes(driver)  # type: ignore[arg-type]
    # Malformed JSONB option falls back to {} rather than blowing up.
    assert infos[0].text_fields == {}
    assert infos[0].key_field == "id"
    assert "text_fields" in infos[0].index_options


async def test_list_pg_search_indexes_tolerates_null_columns() -> None:
    # When the indkey expansion subquery returns NULL (no attributes
    # matched, e.g. expression index), columns should be empty list.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                {
                    "schema": "public",
                    "index": "docs_bm25_idx",
                    "table": "docs",
                    "columns": None,
                    "reloptions": ["key_field=id"],
                }
            ],
        }
    )

    infos = await list_pg_search_indexes(driver)  # type: ignore[arg-type]
    assert infos[0].columns == []


# --- get_pg_search_index_metadata ------------------------------------------


async def test_get_pg_search_index_metadata_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(PgSearchError, match="not installed"):
        await get_pg_search_index_metadata(driver, "public", "docs_bm25_idx")  # type: ignore[arg-type]


async def test_get_pg_search_index_metadata_raises_when_index_missing() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [],  # no matching index
        }
    )

    with pytest.raises(PgSearchError, match="no BM25 index named"):
        await get_pg_search_index_metadata(driver, "public", "docs_bm25_idx")  # type: ignore[arg-type]


async def test_get_pg_search_index_metadata_returns_parsed_info() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                {
                    "schema": "public",
                    "index": "docs_bm25_idx",
                    "table": "docs",
                    "columns": ["id", "body"],
                    "reloptions": _RELOPTIONS,
                }
            ],
        }
    )

    info = await get_pg_search_index_metadata(driver, "public", "docs_bm25_idx")  # type: ignore[arg-type]
    assert info.key_field == "id"
    assert info.target_segment_count == 8


@pytest.mark.parametrize(
    ("schema", "index"),
    [
        ("public; DROP TABLE x", "docs_bm25_idx"),
        ("public", "docs_bm25_idx; --"),
        ("", "docs"),
        ("public", ""),
        ("123bad", "docs"),
    ],
)
async def test_get_pg_search_index_metadata_validates_identifiers(schema: str, index: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="invalid"):
        await get_pg_search_index_metadata(driver, schema, index)  # type: ignore[arg-type]


# --- extension allowlist ---------------------------------------------------


def test_pg_search_on_enableable_extensions_allowlist() -> None:
    """enable_extension must accept 'pg_search'; the allowlist is the
    only injection guard since CREATE EXTENSION takes an identifier."""
    assert "pg_search" in ENABLEABLE_EXTENSIONS


# --- tool registration ------------------------------------------------------


async def test_pg_search_tools_registered_for_read_access() -> None:
    """Both BM-1 tools should be visible via list_tools when running with
    default (READ-capable) access."""
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {"list_pg_search_indexes", "get_pg_search_index_metadata"} <= listed
