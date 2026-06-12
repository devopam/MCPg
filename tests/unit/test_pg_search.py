"""Tests for the pg_search BM-1 observability + BM-2 search surfaces."""

from typing import Any

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.extensions import ENABLEABLE_EXTENSIONS
from mcpg.pg_search import (
    CreatePgSearchIndexResult,
    HybridHit,
    PgSearchAdvisorFinding,
    PgSearchError,
    PgSearchHit,
    PgSearchIndexInfo,
    PgSearchParsedQuery,
    ReindexPgSearchResult,
    audit_pg_search_indexes,
    create_pg_search_index,
    get_pg_search_index_metadata,
    hybrid_bm25_vector_search,
    list_pg_search_indexes,
    pg_search_more_like_this,
    pg_search_parse_query,
    pg_search_run,
    recommend_pg_search_maintenance,
    reindex_pg_search_index,
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
    """BM-1 and BM-2 tools should be visible via list_tools when running
    with default (READ-capable) access."""
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {
        "list_pg_search_indexes",
        "get_pg_search_index_metadata",
        "pg_search_run",
        "pg_search_more_like_this",
        "pg_search_parse_query",
    } <= listed


# --- BM-2: pg_search_run ----------------------------------------------------


async def test_pg_search_run_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(PgSearchError, match="not installed"):
        await pg_search_run(driver, "public", "docs", "rust", "id", limit=10)  # type: ignore[arg-type]


async def test_pg_search_run_whole_index_search_returns_hits() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "@@@": [
                {"id": 1, "score": 9.5, "snippets": None},
                {"id": 2, "score": 7.1, "snippets": None},
            ],
        }
    )

    hits = await pg_search_run(driver, "public", "docs", "rust", "id", limit=10)  # type: ignore[arg-type]

    assert hits == [
        PgSearchHit(id=1, score=9.5, snippets=[]),
        PgSearchHit(id=2, score=7.1, snippets=[]),
    ]
    # SQL inspection: whole-index form omits the column qualifier on
    # the @@@ predicate.
    sql, params, force_readonly = driver.calls[-1]
    assert force_readonly is True
    assert " t @@@ %s " in sql
    assert "ORDER BY pdb.score(t) DESC" in sql
    assert params == ["rust", 10]


async def test_pg_search_run_single_column_search_emits_column_qualifier() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "@@@": [{"id": 1, "score": 5.0, "snippets": None}],
        }
    )

    await pg_search_run(
        driver,  # type: ignore[arg-type]
        "public",
        "docs",
        "rust",
        "id",
        columns=["body"],
        limit=5,
    )

    sql, params, _ = driver.calls[-1]
    assert ' t."body" @@@ %s ' in sql
    assert params == ["rust", 5]


async def test_pg_search_run_with_snippets_projects_pdb_snippets() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "@@@": [
                {"id": 1, "score": 9.5, "snippets": ["<b>rust</b> programming", "<b>rust</b>aceans"]},
            ],
        }
    )

    hits = await pg_search_run(
        driver,  # type: ignore[arg-type]
        "public",
        "docs",
        "rust",
        "id",
        limit=5,
        return_snippets=True,
        snippet_field="body",
    )

    assert hits[0].snippets == ["<b>rust</b> programming", "<b>rust</b>aceans"]
    sql, params, _ = driver.calls[-1]
    assert 'pdb.snippets(t."body", %s, %s, %s, NULL, NULL, ' in sql
    # Param order must match placeholder positions: snippet args appear
    # in SELECT (first), then query (WHERE), then limit (LIMIT).
    assert params == ["<b>", "</b>", 150, "rust", 5]


async def test_pg_search_run_param_count_matches_placeholder_count() -> None:
    """Regression: the snippet projection adds three %s placeholders in
    SELECT that must appear in params before the WHERE/LIMIT binds.
    Mismatched ordering would silently rotate the query arg into the
    snippet slot, returning whatever rows happen to match the start_tag
    literal."""
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "@@@": [{"id": 1, "score": 1.0, "snippets": []}],
        }
    )

    # No-snippet case: 2 placeholders (query, limit).
    await pg_search_run(driver, "public", "docs", "rust", "id", limit=5)  # type: ignore[arg-type]
    sql, params, _ = driver.calls[-1]
    assert sql.count("%s") == len(params)
    assert params[-2:] == ["rust", 5]

    # Snippet case: 5 placeholders (3 snippet, query, limit).
    await pg_search_run(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        "rust",
        "id",
        limit=5,
        return_snippets=True,
        snippet_field="body",
    )
    sql, params, _ = driver.calls[-1]
    assert sql.count("%s") == len(params) == 5
    # Snippet args first (they appear in SELECT), then query (WHERE),
    # then limit (LIMIT).
    assert params == ["<b>", "</b>", 150, "rust", 5]


async def test_pg_search_run_return_snippets_without_field_raises() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="snippet_field"):
        await pg_search_run(
            driver,  # type: ignore[arg-type]
            "public",
            "docs",
            "rust",
            "id",
            limit=5,
            return_snippets=True,
        )


async def test_pg_search_run_snippet_field_without_return_snippets_raises() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="snippet_field is only valid"):
        await pg_search_run(
            driver,  # type: ignore[arg-type]
            "public",
            "docs",
            "rust",
            "id",
            limit=5,
            return_snippets=False,
            snippet_field="body",
        )


async def test_pg_search_run_multi_column_search_raises() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="multi-column"):
        await pg_search_run(
            driver,  # type: ignore[arg-type]
            "public",
            "docs",
            "rust",
            "id",
            columns=["body", "title"],
            limit=5,
        )


async def test_pg_search_run_empty_columns_raises() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="non-empty list"):
        await pg_search_run(
            driver,  # type: ignore[arg-type]
            "public",
            "docs",
            "rust",
            "id",
            columns=[],
            limit=5,
        )


@pytest.mark.parametrize("bad_limit", [0, -1, 10_001, True, "10", 1.5])
async def test_pg_search_run_limit_validation(bad_limit: object) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="limit"):
        await pg_search_run(driver, "public", "docs", "rust", "id", limit=bad_limit)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("schema", "table", "key_field"),
    [
        ("public; DROP TABLE x", "docs", "id"),
        ("public", "docs; --", "id"),
        ("public", "docs", "id'; DROP"),
        ("", "docs", "id"),
        ("public", "", "id"),
        ("public", "docs", ""),
    ],
)
async def test_pg_search_run_identifier_validation(schema: str, table: str, key_field: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="invalid"):
        await pg_search_run(driver, schema, table, "rust", key_field, limit=5)  # type: ignore[arg-type]


async def test_pg_search_run_query_must_be_str() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="query must be str"):
        await pg_search_run(driver, "public", "docs", 42, "id", limit=5)  # type: ignore[arg-type]


# --- BM-2: pg_search_more_like_this ----------------------------------------


async def test_pg_search_more_like_this_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(PgSearchError, match="not installed"):
        await pg_search_more_like_this(  # type: ignore[arg-type]
            driver, "public", "docs", 42, "id", limit=10
        )


async def test_pg_search_more_like_this_returns_hits_with_correlated_seed() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pdb.more_like_this(seed)": [
                {"id": 2, "score": 8.0},
                {"id": 3, "score": 6.5},
            ],
        }
    )

    hits = await pg_search_more_like_this(  # type: ignore[arg-type]
        driver, "public", "docs", 42, "id", limit=10
    )

    assert hits == [PgSearchHit(id=2, score=8.0), PgSearchHit(id=3, score=6.5)]
    sql, params, force_readonly = driver.calls[-1]
    assert force_readonly is True
    # Seed sub-SELECT references the same table as the outer scan,
    # joined by key_field.
    assert 'WHERE seed."id" = %s' in sql
    assert "pdb.more_like_this(seed)" in sql
    assert params == [42, 10]


async def test_pg_search_more_like_this_rejects_bad_limit() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="limit"):
        await pg_search_more_like_this(  # type: ignore[arg-type]
            driver, "public", "docs", 42, "id", limit=0
        )


async def test_pg_search_more_like_this_validates_identifiers() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="invalid"):
        await pg_search_more_like_this(  # type: ignore[arg-type]
            driver, "bad; schema", "docs", 42, "id", limit=10
        )


# --- pdb.more_like_this tuning args (backlog follow-up) --------------------


async def test_pg_search_more_like_this_no_tuning_args_omits_them_from_sql() -> None:
    """Sanity check: the wrapper's default behavior renders exactly the
    minimal pdb.more_like_this(seed) call, no named args, so upstream's
    defaults apply unchanged."""
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pdb.more_like_this(seed)": [{"id": 2, "score": 8.0}],
        }
    )

    await pg_search_more_like_this(driver, "public", "docs", 42, "id", limit=10)  # type: ignore[arg-type]

    sql, params, _ = driver.calls[-1]
    assert "pdb.more_like_this(seed)" in sql
    # No named-arg syntax should appear when no tuning kwargs are set.
    assert " => " not in sql
    assert params == [42, 10]


async def test_pg_search_more_like_this_int_tuning_args_render_as_named() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pdb.more_like_this(seed,": [],
        }
    )

    await pg_search_more_like_this(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        42,
        "id",
        limit=10,
        min_doc_frequency=2,
        max_doc_frequency=1000,
        min_term_frequency=1,
        max_query_terms=25,
        min_word_length=2,
        max_word_length=20,
    )

    sql, params, _ = driver.calls[-1]
    # Each int tuning arg uses named-arg syntax against a bind param.
    assert "min_doc_frequency => %s" in sql
    assert "max_doc_frequency => %s" in sql
    assert "min_term_frequency => %s" in sql
    assert "max_query_terms => %s" in sql
    assert "min_word_length => %s" in sql
    assert "max_word_length => %s" in sql
    # Bind order: tuning args (in caller order), then document_id, then limit.
    assert params == [2, 1000, 1, 25, 2, 20, 42, 10]


async def test_pg_search_more_like_this_fields_jsonb_serialized_and_cast() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pdb.more_like_this(seed,": [],
        }
    )

    await pg_search_more_like_this(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        42,
        "id",
        limit=10,
        fields={"body": {"boost": 2.0}},
    )

    sql, params, _ = driver.calls[-1]
    assert "fields => %s::jsonb" in sql
    # JSON-serialized with sort_keys for deterministic SQL.
    assert params[0] == '{"body": {"boost": 2.0}}'


async def test_pg_search_more_like_this_boost_factor_and_stop_words_render() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pdb.more_like_this(seed,": [],
        }
    )

    await pg_search_more_like_this(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        42,
        "id",
        limit=10,
        boost_factor=1.5,
        stop_words=["the", "a", "an"],
    )

    sql, params, _ = driver.calls[-1]
    assert "boost_factor => %s::real" in sql
    assert "stop_words => %s::text[]" in sql
    # boost_factor coerced to float; stop_words passed through as list.
    assert 1.5 in params
    assert ["the", "a", "an"] in params


async def test_pg_search_more_like_this_param_count_matches_placeholders() -> None:
    """Regression: bind list must align with placeholder positions across
    every combination of tuning args. Same trap that bit pg_search_run
    earlier in BM-2."""
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pdb.more_like_this(seed,": [],
        }
    )

    await pg_search_more_like_this(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        42,
        "id",
        limit=10,
        fields={"body": {}},
        min_doc_frequency=2,
        boost_factor=1.0,
        stop_words=["the"],
    )

    sql, params, _ = driver.calls[-1]
    assert sql.count("%s") == len(params)
    # Order: tuning args in their caller order, then document_id, then limit.
    assert params == ['{"body": {}}', 2, 1.0, ["the"], 42, 10]


@pytest.mark.parametrize(
    ("kwarg", "value", "match"),
    [
        ("min_doc_frequency", -1, "min_doc_frequency"),
        ("min_doc_frequency", True, "min_doc_frequency"),  # bool-as-int
        ("max_doc_frequency", 1_000_000_001, "max_doc_frequency"),
        ("max_query_terms", "25", "max_query_terms"),  # string, not int
        ("boost_factor", float("nan"), "boost_factor"),
        ("boost_factor", float("inf"), "boost_factor"),
        ("boost_factor", True, "boost_factor"),  # bool-as-number
        ("boost_factor", "1.5", "boost_factor"),
        ("stop_words", "the", "stop_words"),  # str, not list
        ("stop_words", [1, 2, 3], "stop_words"),  # list of non-str
        ("fields", [1, 2], "fields"),  # not a dict
        ("fields", "{}", "fields"),  # str, not dict
    ],
)
async def test_pg_search_more_like_this_tuning_arg_validation(kwarg: str, value: object, match: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    kwargs = {kwarg: value}
    with pytest.raises(PgSearchError, match=match):
        await pg_search_more_like_this(  # type: ignore[arg-type]
            driver, "public", "docs", 42, "id", limit=10, **kwargs
        )


async def test_pg_search_more_like_this_fields_non_json_serializable_value_raises() -> None:
    """Regression: a dict whose *shape* passes ``isinstance(..., dict)``
    but contains a value json.dumps can't encode (sets, datetimes,
    custom objects) used to escape validation and surface as a bare
    TypeError from the JSON-encode step. The wrapper's contract is
    "all validation failures surface as PgSearchError", so the
    validator probes encodability up front."""
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    # `set` is the simplest non-JSON-serializable built-in.
    with pytest.raises(PgSearchError, match="JSON-serializable"):
        await pg_search_more_like_this(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            42,
            "id",
            limit=10,
            fields={"body": {"boost", "factor"}},
        )


# --- BM-2: pg_search_parse_query -------------------------------------------


async def test_pg_search_parse_query_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(PgSearchError, match="not installed"):
        await pg_search_parse_query(driver, "rust AND programming")  # type: ignore[arg-type]


async def test_pg_search_parse_query_returns_parsed_text() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pdb.parse(%s, %s, %s)": [{"parsed": "(+rust +programming)"}],
        }
    )

    parsed = await pg_search_parse_query(  # type: ignore[arg-type]
        driver, "rust AND programming", lenient=True, conjunction_mode=True
    )

    assert parsed == PgSearchParsedQuery(parsed="(+rust +programming)")
    sql, params, _ = driver.calls[-1]
    assert params == ["rust AND programming", True, True]
    assert "pdb.parse(%s, %s, %s)::text" in sql


async def test_pg_search_parse_query_empty_result_yields_empty_string() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "pdb.parse": [],  # defensive — upstream always returns a row
        }
    )

    parsed = await pg_search_parse_query(driver, "rust")  # type: ignore[arg-type]
    assert parsed.parsed == ""


@pytest.mark.parametrize("bad_query", [None, 42, ["rust"], b"rust"])
async def test_pg_search_parse_query_rejects_non_string(bad_query: object) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="query_string must be str"):
        await pg_search_parse_query(driver, bad_query)  # type: ignore[arg-type]


async def test_pg_search_parse_query_rejects_non_bool_flags() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="lenient must be a bool"):
        await pg_search_parse_query(driver, "rust", lenient="yes")  # type: ignore[arg-type]


# --- BM-3: hybrid_bm25_vector_search ---------------------------------------


_HYBRID_FUSED_RESULT = [
    {"id": 1, "score": 0.0328, "bm25_rank": 1, "vector_rank": 2},
    {"id": 7, "score": 0.0247, "bm25_rank": None, "vector_rank": 1},
    {"id": 3, "score": 0.0163, "bm25_rank": 2, "vector_rank": None},
]


async def test_hybrid_bm25_vector_search_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(PgSearchError, match="not installed"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            query_text="rust",
            query_vector=[1.0, 2.0, 3.0],
            key_field="id",
            vector_column="embedding",
            final_limit=5,
        )


async def test_hybrid_bm25_vector_search_returns_fused_hits() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WITH bm25_leg": _HYBRID_FUSED_RESULT,
        }
    )

    hits = await hybrid_bm25_vector_search(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        query_text="rust",
        query_vector=[1.0, 2.0, 3.0],
        key_field="id",
        vector_column="embedding",
        final_limit=5,
    )

    assert hits == [
        HybridHit(id=1, score=0.0328, bm25_rank=1, vector_rank=2),
        HybridHit(id=7, score=0.0247, bm25_rank=None, vector_rank=1),
        HybridHit(id=3, score=0.0163, bm25_rank=2, vector_rank=None),
    ]


async def test_hybrid_bm25_vector_search_renders_canonical_rrf_sql() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WITH bm25_leg": [],
        }
    )

    await hybrid_bm25_vector_search(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        query_text="rust",
        query_vector=[1.0, 2.0, 3.0],
        key_field="id",
        vector_column="embedding",
        final_limit=5,
    )

    sql, params, force_readonly = driver.calls[-1]
    assert force_readonly is True
    # CTE shape per the 2025-10-22 blog + tests/tests/documentation.rs.
    assert "WITH bm25_leg AS" in sql
    assert "ROW_NUMBER() OVER (ORDER BY pdb.score(t) DESC)" in sql
    assert " vector_leg AS" in sql
    assert ' t."embedding" <=> %s::vector' in sql
    assert " fused AS" in sql
    assert " UNION ALL" in sql
    # Defaults render as literals: k=60, equal 1.0 weights, per-leg
    # LIMIT 20.
    assert "1.0 / (60 + rank)" in sql
    assert "1.0 * 1.0 / (60 + rank)" in sql
    assert "LIMIT 20" in sql
    # Bound params: query_text, vector (x2), final_limit.
    assert params == ["rust", "[1.0,2.0,3.0]", "[1.0,2.0,3.0]", 5]
    # Top-level GROUP BY + SUM is what makes this RRF (not a join).
    assert "GROUP BY id" in sql
    assert "SUM(score)" in sql


async def test_hybrid_bm25_vector_search_weights_and_k_render_as_literals() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WITH bm25_leg": [],
        }
    )

    await hybrid_bm25_vector_search(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        query_text="rust",
        query_vector=[1.0],
        key_field="id",
        vector_column="embedding",
        k=42,
        bm25_weight=0.7,
        vector_weight=0.3,
        per_leg_limit=15,
        final_limit=5,
    )

    sql, _, _ = driver.calls[-1]
    assert "0.7 * 1.0 / (42 + rank)" in sql
    assert "0.3 * 1.0 / (42 + rank)" in sql
    assert "LIMIT 15" in sql


async def test_hybrid_bm25_vector_search_single_column_bm25_target() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WITH bm25_leg": [],
        }
    )

    await hybrid_bm25_vector_search(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        query_text="rust",
        query_vector=[1.0],
        key_field="id",
        vector_column="embedding",
        bm25_columns=["body"],
        final_limit=5,
    )

    sql, _, _ = driver.calls[-1]
    assert ' t."body" @@@ %s' in sql


async def test_hybrid_bm25_vector_search_multi_column_bm25_raises() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="multi-column"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            query_text="rust",
            query_vector=[1.0],
            key_field="id",
            vector_column="embedding",
            bm25_columns=["body", "title"],
            final_limit=5,
        )


@pytest.mark.parametrize("op", ["<=>", "<->", "<#>"])
async def test_hybrid_bm25_vector_search_accepts_each_distance_op(op: str) -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WITH bm25_leg": [],
        }
    )

    await hybrid_bm25_vector_search(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        query_text="rust",
        query_vector=[1.0],
        key_field="id",
        vector_column="embedding",
        distance_op=op,
        final_limit=5,
    )

    sql, _, _ = driver.calls[-1]
    assert f' t."embedding" {op} %s::vector' in sql


@pytest.mark.parametrize("op", ["<>", "@@", "drop", ""])
async def test_hybrid_bm25_vector_search_rejects_unknown_distance_op(op: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="distance_op"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            query_text="rust",
            query_vector=[1.0],
            key_field="id",
            vector_column="embedding",
            distance_op=op,
            final_limit=5,
        )


@pytest.mark.parametrize(
    ("bm25_weight", "vector_weight"),
    [
        (-1.0, 1.0),
        (1.0, -0.1),
        (float("nan"), 1.0),
        (1.0, float("inf")),
        (True, 1.0),  # bool-as-int trap
        ("0.5", 1.0),
    ],
)
async def test_hybrid_bm25_vector_search_rejects_bad_weights(bm25_weight: object, vector_weight: object) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="weight"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            query_text="rust",
            query_vector=[1.0],
            key_field="id",
            vector_column="embedding",
            bm25_weight=bm25_weight,
            vector_weight=vector_weight,
            final_limit=5,
        )


@pytest.mark.parametrize("bad_limit", [0, -1, 10_001, True, "10"])
@pytest.mark.parametrize("which", ["final_limit", "per_leg_limit"])
async def test_hybrid_bm25_vector_search_rejects_bad_limits(bad_limit: object, which: str) -> None:
    """Both limit kwargs go through _validate_limit; the parametrize
    sweep covers each so neither path can regress unnoticed."""
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    kwargs: dict[str, object] = {
        "schema": "public",
        "table": "docs",
        "query_text": "rust",
        "query_vector": [1.0],
        "key_field": "id",
        "vector_column": "embedding",
        # Defaults; the parametrize swaps one of these out below.
        "final_limit": 5,
        "per_leg_limit": 20,
    }
    kwargs[which] = bad_limit
    with pytest.raises(PgSearchError, match="limit"):
        await hybrid_bm25_vector_search(driver, **kwargs)  # type: ignore[arg-type]


async def test_hybrid_bm25_vector_search_raises_when_pgvector_absent() -> None:
    """pg_search is installed but pgvector is not — the vector leg
    needs %s::vector to resolve, so the wrapper must fail fast with
    a clear message rather than letting PostgreSQL raise 'type
    vector does not exist'."""
    # FakeRoutingDriver matches the first substring whose key appears
    # in the query. Both extension presence checks use the same
    # `pg_extension` query, so we need to discriminate by params.
    from _fakes import FakeParamRoutingDriver

    driver = FakeParamRoutingDriver(
        {
            ("pg_extension", ("pg_search",)): [{"present": 1}],
            ("pg_extension", ("vector",)): [],
        }
    )

    with pytest.raises(PgSearchError, match="pgvector"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            query_text="rust",
            query_vector=[1.0],
            key_field="id",
            vector_column="embedding",
            final_limit=5,
        )


@pytest.mark.parametrize("bad_k", [0, -1, True])
async def test_hybrid_bm25_vector_search_rejects_bad_k(bad_k: object) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="k"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            query_text="rust",
            query_vector=[1.0],
            key_field="id",
            vector_column="embedding",
            k=bad_k,
            final_limit=5,
        )


@pytest.mark.parametrize(
    ("schema", "table", "key_field", "vector_column"),
    [
        ("public; --", "docs", "id", "embedding"),
        ("public", "docs; DROP", "id", "embedding"),
        ("public", "docs", "id'or'1", "embedding"),
        ("public", "docs", "id", "embedding); --"),
    ],
)
async def test_hybrid_bm25_vector_search_identifier_validation(
    schema: str, table: str, key_field: str, vector_column: str
) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="invalid"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            schema,
            table,
            query_text="rust",
            query_vector=[1.0],
            key_field=key_field,
            vector_column=vector_column,
            final_limit=5,
        )


async def test_hybrid_bm25_vector_search_query_text_must_be_str() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="query_text must be str"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            query_text=42,
            query_vector=[1.0],
            key_field="id",
            vector_column="embedding",
            final_limit=5,
        )


async def test_hybrid_bm25_vector_search_accepts_preformatted_vector_string() -> None:
    """Pre-formatted ``[1,2,3]`` strings pass through unchanged — mirrors
    the turboquant query wrappers' behavior."""
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WITH bm25_leg": [],
        }
    )

    await hybrid_bm25_vector_search(  # type: ignore[arg-type]
        driver,
        "public",
        "docs",
        query_text="rust",
        query_vector="[0.1,0.2,0.3]",
        key_field="id",
        vector_column="embedding",
        final_limit=5,
    )

    _, params, _ = driver.calls[-1]
    assert params == ["rust", "[0.1,0.2,0.3]", "[0.1,0.2,0.3]", 5]


async def test_hybrid_bm25_vector_search_rejects_bad_vector_type() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(PgSearchError, match="query_vector"):
        await hybrid_bm25_vector_search(  # type: ignore[arg-type]
            driver,
            "public",
            "docs",
            query_text="rust",
            query_vector=42,
            key_field="id",
            vector_column="embedding",
            final_limit=5,
        )


# --- Tool registration for BM-3 --------------------------------------------


async def test_hybrid_bm25_vector_search_tool_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "hybrid_bm25_vector_search" in listed


# --- BM-4: create_pg_search_index ------------------------------------------


def _ddl_db_with_extension_installed() -> FakeDatabase:
    """A FakeDatabase whose driver reports pg_search as installed."""
    return FakeDatabase(FakeRoutingDriver({"pg_extension": [{"present": 1}]}))  # type: ignore[arg-type]


async def test_create_pg_search_index_raises_when_extension_absent() -> None:
    db = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))  # type: ignore[arg-type]
    with pytest.raises(PgSearchError, match="not installed"):
        await create_pg_search_index(  # type: ignore[arg-type]
            db, "public", "docs", ["id", "body"], "docs_bm25_idx", "id"
        )


async def test_create_pg_search_index_renders_minimal_sql_when_no_options() -> None:
    db = _ddl_db_with_extension_installed()

    result = await create_pg_search_index(  # type: ignore[arg-type]
        db, "public", "docs", ["id", "body"], "docs_bm25_idx", "id"
    )

    assert isinstance(result, CreatePgSearchIndexResult)
    assert result.schema == "public"
    assert result.columns == ["id", "body"]
    assert result.options == {"key_field": "id"}
    # Verify the rendered DDL.
    assert result.create_sql == (
        'CREATE INDEX CONCURRENTLY "docs_bm25_idx" '
        'ON "public"."docs" '
        'USING bm25 ("id", "body") '
        "WITH (key_field = 'id')"
    )
    # The DDL ran on autocommit via Database.run_unmanaged.
    assert db.unmanaged == [result.create_sql]


async def test_create_pg_search_index_renders_jsonb_int_text_options() -> None:
    db = _ddl_db_with_extension_installed()

    result = await create_pg_search_index(  # type: ignore[arg-type]
        db,
        "public",
        "docs",
        ["id", "body"],
        "docs_bm25_idx",
        "id",
        text_fields={"body": {"tokenizer": "default"}},
        numeric_fields={"price": {"fast": True}},
        layer_sizes="100,1000,10000",
        target_segment_count=8,
        mutable_segment_rows=10_000,
        sort_by="id",
        search_tokenizer={"type": "default"},
        concurrently=False,
    )

    # CONCURRENTLY omitted when explicit False.
    assert " CONCURRENTLY" not in result.create_sql
    # Options block contains all seven, in declaration order.
    assert "key_field = 'id'" in result.create_sql
    assert 'text_fields = \'{"body": {"tokenizer": "default"}}\'' in result.create_sql
    assert 'numeric_fields = \'{"price": {"fast": true}}\'' in result.create_sql
    assert "layer_sizes = '100,1000,10000'" in result.create_sql
    assert "target_segment_count = 8" in result.create_sql
    assert "mutable_segment_rows = 10000" in result.create_sql
    assert "sort_by = 'id'" in result.create_sql
    assert 'search_tokenizer = \'{"type": "default"}\'' in result.create_sql


async def test_create_pg_search_index_escapes_single_quotes_in_text_options() -> None:
    """Per the established pattern in turboquant DDL: PG single-quote
    literals must double internal quotes."""
    db = _ddl_db_with_extension_installed()

    result = await create_pg_search_index(  # type: ignore[arg-type]
        db,
        "public",
        "docs",
        ["id"],
        "docs_bm25_idx",
        "id",
        sort_by="O'Reilly",  # the canonical apostrophe-injection probe
    )

    assert "sort_by = 'O''Reilly'" in result.create_sql


@pytest.mark.parametrize(
    ("kwarg", "value", "match"),
    [
        ("target_segment_count", 0, "target_segment_count"),
        ("target_segment_count", 100_001, "target_segment_count"),
        ("target_segment_count", True, "target_segment_count"),
        ("mutable_segment_rows", -1, "mutable_segment_rows"),
        ("mutable_segment_rows", 100_000_001, "mutable_segment_rows"),
        ("layer_sizes", "", "layer_sizes"),
        ("layer_sizes", 42, "layer_sizes"),
        ("text_fields", "not a dict", "text_fields"),
        ("text_fields", [1, 2, 3], "text_fields"),
    ],
)
async def test_create_pg_search_index_option_validation(kwarg: str, value: object, match: str) -> None:
    db = _ddl_db_with_extension_installed()

    kwargs: dict[str, object] = {kwarg: value}
    with pytest.raises(PgSearchError, match=match):
        await create_pg_search_index(  # type: ignore[arg-type]
            db,
            "public",
            "docs",
            ["id"],
            "docs_bm25_idx",
            "id",
            **kwargs,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"schema": "public; DROP"},
        {"table": "docs'; --"},
        {"index_name": ""},
        {"key_field": "id'or'1"},
        {"columns": []},
        {"columns": ["good", "bad; --"]},
        {"columns": "id"},  # not a list
    ],
)
async def test_create_pg_search_index_rejects_unsafe_identifiers(
    kwargs: dict[str, object],
) -> None:
    db = _ddl_db_with_extension_installed()

    base: dict[str, object] = {
        "schema": "public",
        "table": "docs",
        "columns": ["id"],
        "index_name": "docs_bm25_idx",
        "key_field": "id",
    }
    base.update(kwargs)
    with pytest.raises(PgSearchError):
        await create_pg_search_index(  # type: ignore[arg-type]
            db, **base
        )


async def test_create_pg_search_index_rejects_non_bool_concurrently() -> None:
    db = _ddl_db_with_extension_installed()
    with pytest.raises(PgSearchError, match="concurrently"):
        await create_pg_search_index(  # type: ignore[arg-type]
            db, "public", "docs", ["id"], "docs_bm25_idx", "id", concurrently="yes"
        )


# --- BM-4: reindex_pg_search_index -----------------------------------------


async def test_reindex_pg_search_index_raises_when_extension_absent() -> None:
    db = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))  # type: ignore[arg-type]
    with pytest.raises(PgSearchError, match="not installed"):
        await reindex_pg_search_index(db, "public", "docs_bm25_idx")  # type: ignore[arg-type]


async def test_reindex_pg_search_index_raises_when_index_is_not_bm25() -> None:
    """Pre-flight catalog lookup confirms am.amname='bm25' before REINDEX
    so the call can't probe arbitrary indexes via PG error messages."""
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_extension": [{"present": 1}],
                "WHERE am.amname = 'bm25'": [],  # pre-flight finds nothing
            }
        )
    )
    with pytest.raises(PgSearchError, match="not a BM25 index"):
        await reindex_pg_search_index(db, "public", "other_idx")  # type: ignore[arg-type]


async def test_reindex_pg_search_index_renders_sql_and_runs_unmanaged() -> None:
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_extension": [{"present": 1}],
                "WHERE am.amname = 'bm25'": [{"present": 1}],  # pre-flight passes
            }
        )
    )

    result = await reindex_pg_search_index(db, "public", "docs_bm25_idx")  # type: ignore[arg-type]

    assert isinstance(result, ReindexPgSearchResult)
    assert result.reindex_sql == 'REINDEX INDEX CONCURRENTLY "public"."docs_bm25_idx"'
    assert db.unmanaged == [result.reindex_sql]


async def test_reindex_pg_search_index_omits_concurrently_when_disabled() -> None:
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_extension": [{"present": 1}],
                "WHERE am.amname = 'bm25'": [{"present": 1}],
            }
        )
    )

    result = await reindex_pg_search_index(  # type: ignore[arg-type]
        db, "public", "docs_bm25_idx", concurrently=False
    )

    assert " CONCURRENTLY" not in result.reindex_sql


async def test_reindex_pg_search_index_rejects_unsafe_identifiers() -> None:
    db = FakeDatabase(FakeRoutingDriver({"pg_extension": [{"present": 1}]}))  # type: ignore[arg-type]
    with pytest.raises(PgSearchError, match="invalid"):
        await reindex_pg_search_index(db, "public; DROP", "docs_bm25_idx")  # type: ignore[arg-type]


async def test_create_pg_search_index_wraps_driver_failure_as_pg_search_error() -> None:
    """Regression: the docstring promises PgSearchError on DDL failure.
    Without the try/except wrap, a psycopg.Error would propagate
    directly and break that contract."""
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver({"pg_extension": [{"present": 1}]}),
        unmanaged_fail=True,
    )
    with pytest.raises(PgSearchError, match="CREATE INDEX failed"):
        await create_pg_search_index(  # type: ignore[arg-type]
            db, "public", "docs", ["id"], "docs_bm25_idx", "id"
        )


async def test_reindex_pg_search_index_wraps_driver_failure_as_pg_search_error() -> None:
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_extension": [{"present": 1}],
                "WHERE am.amname = 'bm25'": [{"present": 1}],
            }
        ),
        unmanaged_fail=True,
    )
    with pytest.raises(PgSearchError, match="REINDEX INDEX failed"):
        await reindex_pg_search_index(db, "public", "docs_bm25_idx")  # type: ignore[arg-type]


# --- BM-4: tool registration -----------------------------------------------


_DDL_SETTINGS = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_DDL": "true",
    }
)

_UNRESTRICTED_NO_DDL_SETTINGS = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_DDL": "false",
    }
)


async def test_pg_search_ddl_tools_register_only_with_ddl_opt_in() -> None:
    # READ-only mode: DDL tools must NOT be visible.
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "create_pg_search_index" not in listed
    assert "reindex_pg_search_index" not in listed

    # Unrestricted access but MCPG_ALLOW_DDL=false: DDL tools must still
    # NOT be visible. Pins the AND condition explicitly so flipping
    # either flag alone doesn't smuggle DDL tools through.
    server = create_server(  # type: ignore[arg-type]
        _UNRESTRICTED_NO_DDL_SETTINGS, database=FakeDatabase(FakeDriver())
    )
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "create_pg_search_index" not in listed
    assert "reindex_pg_search_index" not in listed

    # DDL opt-in: both tools should appear.
    server = create_server(_DDL_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {"create_pg_search_index", "reindex_pg_search_index"} <= listed


# --- BM-5: recommend_pg_search_maintenance ---------------------------------


def _index_row(
    *,
    schema: str = "public",
    index: str = "docs_bm25_idx",
    table: str = "docs",
    columns: list[str] | None = None,
    reloptions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": schema,
        "index": index,
        "table": table,
        "columns": columns if columns is not None else ["id", "body"],
        "reloptions": reloptions if reloptions is not None else ["key_field=id"],
    }


async def test_recommend_pg_search_maintenance_returns_empty_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})
    assert await recommend_pg_search_maintenance(driver) == []  # type: ignore[arg-type]


async def test_recommend_pg_search_maintenance_returns_empty_for_well_configured_index() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                _index_row(
                    reloptions=[
                        "key_field=id",
                        'text_fields={"body": {"tokenizer": "default"}}',
                    ]
                )
            ],
        }
    )
    findings = await recommend_pg_search_maintenance(driver)  # type: ignore[arg-type]
    assert findings == []


async def test_recommend_pg_search_maintenance_flags_missing_key_field() -> None:
    """key_field is required by upstream — a BM25 index without it
    can't satisfy queries. CRITICAL."""
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [_index_row(reloptions=["target_segment_count=8"])],
        }
    )
    findings = await recommend_pg_search_maintenance(driver)  # type: ignore[arg-type]
    assert len(findings) >= 1
    finding = next(f for f in findings if f.code == "missing_key_field")
    assert finding.severity == "CRITICAL"
    assert finding.schema == "public"
    assert finding.index == "docs_bm25_idx"
    assert "key_field" in finding.evidence
    assert "DROP INDEX" in finding.suggested_action


async def test_recommend_pg_search_maintenance_flags_no_field_configs() -> None:
    """Only key_field set, no *_fields configs — index relies on defaults."""
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [_index_row(reloptions=["key_field=id"])],
        }
    )
    findings = await recommend_pg_search_maintenance(driver)  # type: ignore[arg-type]
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "no_field_configs"
    assert finding.severity == "WARNING"
    assert "default tokenization" in finding.evidence


async def test_recommend_pg_search_maintenance_each_field_config_suppresses_rule() -> None:
    """Any one of the six *_fields configs is enough to suppress the
    no_field_configs WARNING."""
    for field_name in (
        "text_fields",
        "numeric_fields",
        "boolean_fields",
        "json_fields",
        "range_fields",
        "datetime_fields",
    ):
        driver = FakeRoutingDriver(
            {
                "pg_extension": [{"present": 1}],
                "WHERE am.amname = 'bm25'": [_index_row(reloptions=["key_field=id", f'{field_name}={{"col": {{}}}}'])],
            }
        )
        findings = await recommend_pg_search_maintenance(driver)  # type: ignore[arg-type]
        codes = {f.code for f in findings}
        assert "no_field_configs" not in codes, f"{field_name} did not suppress the rule"


async def test_recommend_pg_search_maintenance_multiple_indexes_each_evaluated() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                _index_row(index="good_idx", reloptions=["key_field=id", 'text_fields={"body": {}}']),
                _index_row(index="bad_idx", reloptions=["key_field=id"]),
                _index_row(index="broken_idx", reloptions=["target_segment_count=8"]),
            ],
        }
    )
    findings = await recommend_pg_search_maintenance(driver)  # type: ignore[arg-type]
    by_index = {(f.index, f.code) for f in findings}
    assert ("bad_idx", "no_field_configs") in by_index
    assert ("broken_idx", "missing_key_field") in by_index
    # broken_idx also has no field configs — both rules fire on it.
    assert ("broken_idx", "no_field_configs") in by_index
    # good_idx is clean.
    assert not any(idx == "good_idx" for idx, _ in by_index)


# --- BM-5: audit_pg_search_indexes (CategoryResult adapter) ----------------


async def test_audit_pg_search_indexes_returns_none_when_extension_absent() -> None:
    """The scorecard must omit the category cleanly so a stock cluster
    isn't padded with empty sections and the overall score isn't diluted."""
    driver = FakeRoutingDriver({"pg_extension": []})
    assert await audit_pg_search_indexes(driver) is None  # type: ignore[arg-type]


async def test_audit_pg_search_indexes_returns_good_when_no_findings() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [_index_row(reloptions=["key_field=id", 'text_fields={"body": {}}'])],
        }
    )
    result = await audit_pg_search_indexes(driver)  # type: ignore[arg-type]
    assert result is not None
    assert result.category == "pg_search BM25 Indexes"
    assert result.status == "GOOD"
    assert result.score == 100
    assert len(result.metrics) == 1
    assert result.metrics[0].status == "GOOD"


async def test_audit_pg_search_indexes_deducts_15_per_warning() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [_index_row(reloptions=["key_field=id"])],
        }
    )
    result = await audit_pg_search_indexes(driver)  # type: ignore[arg-type]
    assert result is not None
    assert result.score == 85  # 100 - 15
    assert result.status == "WARNING"  # 85 is < 90 → not GOOD


async def test_audit_pg_search_indexes_deducts_30_per_critical_and_clamps_at_zero() -> None:
    # Four broken indexes → 4 * (CRITICAL=30 + WARNING=15) = 180; clamps at 0.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [
                _index_row(index=f"broken_{i}", reloptions=["target_segment_count=8"]) for i in range(4)
            ],
        }
    )
    result = await audit_pg_search_indexes(driver)  # type: ignore[arg-type]
    assert result is not None
    assert result.score == 0
    assert result.status == "CRITICAL"


# --- BM-5: tool registration -----------------------------------------------


async def test_recommend_pg_search_maintenance_tool_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "recommend_pg_search_maintenance" in listed


# --- BM-5: audit_database integration --------------------------------------


async def test_audit_database_includes_pg_search_category_when_extension_present() -> None:
    """The scorecard glue in audit.audit_database must call our adapter
    and append the CategoryResult — otherwise the rule table is invisible."""
    from mcpg.audit import audit_database

    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'bm25'": [_index_row(reloptions=["key_field=id"])],
        }
    )
    report = await audit_database(driver, "public")  # type: ignore[arg-type]
    cat_names = {c.category for c in report.categories}
    assert "pg_search BM25 Indexes" in cat_names


async def test_audit_database_omits_pg_search_category_when_extension_absent() -> None:
    """Stock cluster (no pg_search installed) must not have the category."""
    from mcpg.audit import audit_database

    driver = FakeRoutingDriver({"pg_extension": []})
    report = await audit_database(driver, "public")  # type: ignore[arg-type]
    cat_names = {c.category for c in report.categories}
    assert "pg_search BM25 Indexes" not in cat_names


# --- BM-5: dataclass surface -----------------------------------------------


def test_pg_search_advisor_finding_is_frozen() -> None:
    """Stable contract — callers script against the dataclass shape, so
    field mutation must raise rather than silently corrupt the rule table."""
    from dataclasses import FrozenInstanceError

    f = PgSearchAdvisorFinding(
        code="missing_key_field",
        severity="CRITICAL",
        schema="public",
        index="docs_bm25_idx",
        evidence="...",
        suggested_action="...",
    )
    with pytest.raises(FrozenInstanceError):
        f.severity = "GOOD"  # type: ignore[misc]
