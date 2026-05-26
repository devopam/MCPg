"""Integration tests for fuzzy text search against a live PostgreSQL."""

from collections.abc import AsyncIterator

import pytest

from mcpg.database import Database
from mcpg.extensions import enable_extension
from mcpg.introspection import list_available_extensions
from mcpg.textsearch import full_text_search, fuzzy_search, geo_search, vector_search

_TABLE = "mcpg_trgm_it"


@pytest.fixture
async def trigram_table(connected_database: Database) -> AsyncIterator[str]:
    """Enable pg_trgm and create a small text table; drop it afterwards."""
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "pg_trgm" not in available:
        pytest.skip("pg_trgm is not available on this PostgreSQL server")
    await enable_extension(driver, "pg_trgm")
    await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}", force_readonly=False)
    await driver.execute_query(f"CREATE TABLE {_TABLE} (name text)", force_readonly=False)
    await driver.execute_query(
        f"INSERT INTO {_TABLE} (name) VALUES ('alice'), ('alicia'), ('bob'), ('Espresso Machine')",
        force_readonly=False,
    )
    try:
        yield _TABLE
    finally:
        await driver.execute_query(f"DROP TABLE IF EXISTS {_TABLE}", force_readonly=False)


async def test_fuzzy_search_ranks_real_rows_by_similarity(connected_database: Database, trigram_table: str) -> None:
    result = await fuzzy_search(connected_database.driver(), "public", trigram_table, "name", "alice")

    assert result.available is True
    values = [match.value for match in result.matches]
    assert "alice" in values
    assert "bob" not in values
    # Matches are ordered by descending similarity.
    assert result.matches == sorted(result.matches, key=lambda m: m.score, reverse=True)


async def test_fuzzy_search_word_mode_matches_a_fragment_full_mode_misses(
    connected_database: Database, trigram_table: str
) -> None:
    driver = connected_database.driver()
    # "expreso" is a misspelled fragment of "Espresso Machine".
    word = await fuzzy_search(driver, "public", trigram_table, "name", "expreso", mode="word")
    full = await fuzzy_search(driver, "public", trigram_table, "name", "expreso", mode="full")

    assert "Espresso Machine" in [match.value for match in word.matches]
    assert "Espresso Machine" not in [match.value for match in full.matches]


async def test_full_text_search_against_real_postgres(connected_database: Database) -> None:
    driver = connected_database.driver()
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_fts_it", force_readonly=False)
    await driver.execute_query("CREATE TABLE mcpg_fts_it (body text)", force_readonly=False)
    await driver.execute_query(
        "INSERT INTO mcpg_fts_it (body) VALUES ('the quick brown fox'), ('a lazy dog sleeps'), ('foxes are clever')",
        force_readonly=False,
    )
    try:
        matches = await full_text_search(driver, "public", "mcpg_fts_it", "body", "fox")

        values = [match.value for match in matches]
        assert "the quick brown fox" in values
        assert "a lazy dog sleeps" not in values
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_fts_it", force_readonly=False)


async def test_vector_search_against_real_pgvector(connected_database: Database) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "vector" not in available:
        pytest.skip("pgvector is not available on this PostgreSQL server")
    await enable_extension(driver, "vector")
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_vsearch_it", force_readonly=False)
    await driver.execute_query("CREATE TABLE mcpg_vsearch_it (id integer, embedding vector(3))", force_readonly=False)
    await driver.execute_query(
        "INSERT INTO mcpg_vsearch_it (id, embedding) VALUES (1, '[1,0,0]'), (2, '[0,1,0]'), (3, '[0.9,0.1,0]')",
        force_readonly=False,
    )
    try:
        result = await vector_search(driver, "public", "mcpg_vsearch_it", "embedding", [1.0, 0.0, 0.0])

        assert result.available is True
        # The nearest row to [1,0,0] is row 1; the embedding column is dropped.
        assert result.matches[0].row["id"] == 1
        assert "embedding" not in result.matches[0].row
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_vsearch_it", force_readonly=False)


async def test_vector_range_search_returns_only_rows_within_threshold(connected_database: Database) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "vector" not in available:
        pytest.skip("pgvector is not available on this PostgreSQL server")
    await enable_extension(driver, "vector")
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_vrange_it", force_readonly=False)
    await driver.execute_query("CREATE TABLE mcpg_vrange_it (id integer, embedding vector(3))", force_readonly=False)
    await driver.execute_query(
        "INSERT INTO mcpg_vrange_it (id, embedding) VALUES "
        "(1, '[1,0,0]'), (2, '[0.9,0.1,0]'), (3, '[0,1,0]'), (4, '[0,0,1]')",
        force_readonly=False,
    )
    try:
        from mcpg.textsearch import vector_range_search

        # L2 distance from [1,0,0]: row 1 = 0.0, row 2 ≈ 0.14, row 3 ≈ 1.41, row 4 ≈ 1.41.
        # max_distance=0.5 should keep only the first two.
        result = await vector_range_search(
            driver, "public", "mcpg_vrange_it", "embedding", [1.0, 0.0, 0.0], max_distance=0.5
        )

        assert result.available is True
        ids = {match.row["id"] for match in result.matches}
        assert ids == {1, 2}
        # Distances are ordered ascending.
        distances = [m.distance for m in result.matches]
        assert distances == sorted(distances)
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_vrange_it", force_readonly=False)


async def test_hybrid_search_fuses_vector_and_full_text_results_against_real_pg(
    connected_database: Database,
) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "vector" not in available:
        pytest.skip("pgvector is not available on this PostgreSQL server")
    await enable_extension(driver, "vector")
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_hybrid_it", force_readonly=False)
    await driver.execute_query(
        "CREATE TABLE mcpg_hybrid_it (id serial PRIMARY KEY, body text NOT NULL, embedding vector(3))",
        force_readonly=False,
    )
    await driver.execute_query(
        "INSERT INTO mcpg_hybrid_it (body, embedding) VALUES "
        "('apple pie recipe', '[1,0,0]'),"
        "('banana bread', '[0,1,0]'),"
        "('apple banana smoothie', '[0.5,0.5,0]'),"
        "('vintage car', '[0,0,1]')",
        force_readonly=False,
    )
    try:
        from mcpg.textsearch import hybrid_search

        # Query vector points at "apple pie" row 1; FTS query "apple"
        # matches rows 1 and 3 (anything mentioning apple). The fused
        # ranking should rank row 1 first (appears in both sources at
        # rank 1), then row 3.
        result = await hybrid_search(
            driver,
            "public",
            "mcpg_hybrid_it",
            "embedding",
            "body",
            [1.0, 0.0, 0.0],
            "apple",
        )

        assert result.available is True
        # All four candidates surface in the merged list (each appears
        # in at least the vector pool).
        ids = [match.row["id"] for match in result.matches]
        assert 1 in ids and 3 in ids
        # The top-ranked result is the one that appears in BOTH sources.
        top = result.matches[0]
        assert top.row["id"] == 1
        assert top.vector_rank == 1 and top.fts_rank == 1
        # rrf_score is descending across the list.
        scores = [m.rrf_score for m in result.matches]
        assert scores == sorted(scores, reverse=True)
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_hybrid_it", force_readonly=False)


async def test_recommend_vector_quantization_picks_up_real_pgvector_columns(
    connected_database: Database,
) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "vector" not in available:
        pytest.skip("pgvector is not available on this PostgreSQL server")
    await enable_extension(driver, "vector")
    await driver.execute_query("DROP SCHEMA IF EXISTS mcpg_quant_it CASCADE", force_readonly=False)
    await driver.execute_query("CREATE SCHEMA mcpg_quant_it", force_readonly=False)
    # 768-dim vectors, just below the row threshold — should NOT recommend.
    await driver.execute_query(
        "CREATE TABLE mcpg_quant_it.small (id serial PRIMARY KEY, emb vector(768))",
        force_readonly=False,
    )
    # 768-dim with 10001 rows — clears the dimension>=768 + row_count>=10000 path.
    await driver.execute_query(
        "CREATE TABLE mcpg_quant_it.big (id serial PRIMARY KEY, emb vector(768))",
        force_readonly=False,
    )
    await driver.execute_query(
        "INSERT INTO mcpg_quant_it.big (emb) SELECT "
        "('[' || array_to_string(array(SELECT random() FROM generate_series(1, 768)), ',') || ']')::vector "
        "FROM generate_series(1, 10001)",
        force_readonly=False,
    )
    try:
        from mcpg.textsearch import recommend_vector_quantization

        recs = await recommend_vector_quantization(driver, "mcpg_quant_it")
        # Only the big table qualifies.
        tables = {r.table for r in recs}
        assert "big" in tables
        assert "small" not in tables
        big = next(r for r in recs if r.table == "big")
        assert big.dimension == 768
        assert big.current_type == "vector"
        assert big.suggested_type == "halfvec"
        # halfvec halves the per-element bytes — savings ratio ~0.5.
        assert 0.49 < big.savings_ratio < 0.51
    finally:
        await driver.execute_query("DROP SCHEMA IF EXISTS mcpg_quant_it CASCADE", force_readonly=False)


async def test_geo_search_against_real_postgis(connected_database: Database) -> None:
    driver = connected_database.driver()
    available = {extension.name for extension in await list_available_extensions(driver)}
    if "postgis" not in available:
        pytest.skip("postgis is not available on this PostgreSQL server")
    await enable_extension(driver, "postgis")
    await driver.execute_query("DROP TABLE IF EXISTS mcpg_geo_it", force_readonly=False)
    await driver.execute_query(
        "CREATE TABLE mcpg_geo_it (id integer, location geometry(Point, 4326))",
        force_readonly=False,
    )
    await driver.execute_query(
        "INSERT INTO mcpg_geo_it (id, location) VALUES "
        "(1, ST_SetSRID(ST_MakePoint(0, 0), 4326)), "
        "(2, ST_SetSRID(ST_MakePoint(10, 10), 4326)), "
        "(3, ST_SetSRID(ST_MakePoint(1, 1), 4326))",
        force_readonly=False,
    )
    try:
        result = await geo_search(driver, "public", "mcpg_geo_it", "location", 0.0, 0.0)

        assert result.available is True
        # The nearest row to (0,0) is row 1; the geometry column is dropped.
        assert result.matches[0].row["id"] == 1
        assert "location" not in result.matches[0].row
    finally:
        await driver.execute_query("DROP TABLE IF EXISTS mcpg_geo_it", force_readonly=False)
