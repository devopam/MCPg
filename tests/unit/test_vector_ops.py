"""Tests for mcpg.vector_ops (pgvector analytics + tool wiring)."""

import math

import pytest
from _fakes import FakeDatabase, FakeParamRoutingDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.vector_ops import (
    DEFAULT_CLUSTER_SAMPLE_SIZE,
    DEFAULT_DRIFT_SAMPLE_SIZE,
    DEFAULT_DRIFT_THRESHOLD,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_OUTLIER_K,
    DEFAULT_OUTLIER_MAX_RESULTS,
    DEFAULT_OUTLIER_ZSCORE,
    DEFAULT_SAMPLE_SIZE,
    ClusterVectorsResult,
    ContextHit,
    CrossTableMatch,
    CrossTableSimilarityResult,
    DistanceMetricRecommendation,
    RelatedRecords,
    RetrieveWithContextResult,
    VectorOpsError,
    VectorOutlierResult,
    _cosine_distance,
    _l2_norm,
    _normalize_in_place,
    _parse_embedding,
    _pick_metric,
    _relative_change,
    _squared_distance,
    _vector_literal,
    analyze_distance_metric,
    cluster_vectors,
    cross_table_similarity,
    detect_vector_outliers,
    monitor_embedding_drift,
    retrieve_with_context,
)

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


# --- shared sample_size cap (scalability P0) -------------------------------


async def test_every_vector_ops_entry_point_rejects_oversized_sample_size() -> None:
    """Regression for deep-review scalability P0 #3: cluster_vectors,
    detect_vector_outliers, monitor_embedding_drift, and
    analyze_distance_metric all pull ``sample_size`` rows into the
    process for in-Python work (k-means, z-scores, centroid drift,
    norm stats). Without a cap, ``sample_size=10_000_000`` is a
    process killer. The shared ``_validate_sample_size`` helper now
    enforces the same ceiling at every entry point; this test pins
    that every tool actually wires it in."""
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    oversize = 10_000_000
    with pytest.raises(VectorOpsError, match="must be ≤"):
        await analyze_distance_metric(driver, "app", "docs", "embedding", sample_size=oversize)  # type: ignore[arg-type]
    with pytest.raises(VectorOpsError, match="must be ≤"):
        await cluster_vectors(driver, "app", "docs", "embedding", k=2, sample_size=oversize)  # type: ignore[arg-type]
    with pytest.raises(VectorOpsError, match="must be ≤"):
        await detect_vector_outliers(driver, "app", "docs", "embedding", sample_size=oversize)  # type: ignore[arg-type]
    with pytest.raises(VectorOpsError, match="must be ≤"):
        await monitor_embedding_drift(  # type: ignore[arg-type]
            driver,
            "app",
            "docs",
            "embedding",
            timestamp_column="created_at",
            baseline_start="2026-01-01",
            baseline_end="2026-01-02",
            current_start="2026-02-01",
            current_end="2026-02-02",
            sample_size=oversize,
        )


# --- _parse_embedding ------------------------------------------------------


def test_parse_embedding_accepts_lists_tuples_and_strings() -> None:
    assert _parse_embedding([1, 2, 3]) == [1.0, 2.0, 3.0]
    assert _parse_embedding((0.5, 1.5)) == [0.5, 1.5]
    assert _parse_embedding("[0.1, 0.2]") == [0.1, 0.2]


def test_parse_embedding_returns_none_for_unparseable_cells() -> None:
    # Tolerant by design — bad rows are skipped, not fatal.
    assert _parse_embedding(None) is None
    assert _parse_embedding("[not a number]") is None
    assert _parse_embedding("") is None
    assert _parse_embedding("[]") is None
    assert _parse_embedding(object()) is None
    assert _parse_embedding([1, "x"]) is None  # mixed-type list


# --- _l2_norm + _pick_metric ----------------------------------------------


def test_l2_norm_handles_zero_and_unit_vectors() -> None:
    assert _l2_norm([0.0, 0.0, 0.0]) == 0.0
    assert _l2_norm([1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert _l2_norm([3.0, 4.0]) == pytest.approx(5.0)


def test_pick_metric_recommends_inner_product_for_pre_normalised_vectors() -> None:
    metric, pre, rationale = _pick_metric(mean_mag=1.0, cv=0.001)
    assert metric == "inner_product"
    assert pre is True
    assert "pre-normalised" in rationale


def test_pick_metric_recommends_cosine_for_flat_but_off_unit_magnitudes() -> None:
    metric, pre, rationale = _pick_metric(mean_mag=10.0, cv=0.001)
    assert metric == "cosine"
    assert pre is False
    assert "nearly constant" in rationale


def test_pick_metric_recommends_cosine_for_variable_magnitudes() -> None:
    metric, pre, rationale = _pick_metric(mean_mag=5.0, cv=0.5)
    assert metric == "cosine"
    assert pre is False
    assert "vary substantially" in rationale


def test_pick_metric_handles_zero_mean_magnitude_safely() -> None:
    metric, pre, rationale = _pick_metric(mean_mag=0.0, cv=0.0)
    assert metric == "cosine"
    assert pre is False
    assert "zero magnitude" in rationale


# --- analyze_distance_metric ----------------------------------------------


def _routes(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    """Routes for FakeRoutingDriver: extension-present + the sample query."""
    return {
        "pg_extension": [{"present": 1}],
        # The actual SELECT uses LIMIT %s; we route on a substring that's
        # unique to this tool's query.
        'WHERE "embedding" IS NOT NULL': rows,
    }


async def test_analyze_distance_metric_reports_unavailable_without_pgvector() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    result = await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert result == DistanceMetricRecommendation(
        available=False,
        sampled_rows=0,
        mean_magnitude=0.0,
        magnitude_std=0.0,
        magnitude_cv=0.0,
        pre_normalised=False,
        recommended_metric="cosine",
        rationale="pgvector extension is not installed",
    )


async def test_analyze_distance_metric_detects_pre_normalised_embeddings() -> None:
    # Three unit vectors — magnitudes all exactly 1.0 → CV=0, pre-normalised.
    rows = [
        {"embedding": [1.0, 0.0, 0.0]},
        {"embedding": [0.0, 1.0, 0.0]},
        {"embedding": [0.0, 0.0, 1.0]},
    ]
    driver = FakeRoutingDriver(_routes(rows))

    result = await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert result.available is True
    assert result.sampled_rows == 3
    assert result.mean_magnitude == pytest.approx(1.0)
    assert result.magnitude_cv == pytest.approx(0.0)
    assert result.pre_normalised is True
    assert result.recommended_metric == "inner_product"


async def test_analyze_distance_metric_recommends_cosine_for_constant_off_unit_magnitudes() -> None:
    # All vectors have magnitude exactly 10 -> CV=0 but not unit-norm.
    rows = [
        {"embedding": [10.0, 0.0]},
        {"embedding": [0.0, 10.0]},
        {"embedding": [6.0, 8.0]},
    ]
    driver = FakeRoutingDriver(_routes(rows))

    result = await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert result.mean_magnitude == pytest.approx(10.0)
    assert result.magnitude_cv == pytest.approx(0.0)
    assert result.pre_normalised is False
    assert result.recommended_metric == "cosine"


async def test_analyze_distance_metric_recommends_cosine_for_variable_magnitudes() -> None:
    rows = [
        {"embedding": [1.0, 0.0]},
        {"embedding": [10.0, 0.0]},
        {"embedding": [100.0, 0.0]},
    ]
    driver = FakeRoutingDriver(_routes(rows))

    result = await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert result.magnitude_cv > 0.5
    assert result.recommended_metric == "cosine"
    assert "vary substantially" in result.rationale


async def test_analyze_distance_metric_returns_zero_sample_when_table_is_empty() -> None:
    driver = FakeRoutingDriver(_routes([]))

    result = await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert result.available is True
    assert result.sampled_rows == 0
    assert result.recommended_metric == "cosine"
    assert "No non-NULL embeddings" in result.rationale


async def test_analyze_distance_metric_skips_unparseable_rows_silently() -> None:
    rows = [
        {"embedding": [1.0, 0.0]},
        {"embedding": None},  # NULL — skipped
        {"embedding": "[bad text]"},  # unparseable — skipped
        {"embedding": [0.0, 1.0]},
    ]
    driver = FakeRoutingDriver(_routes(rows))

    result = await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert result.sampled_rows == 2
    assert result.mean_magnitude == pytest.approx(1.0)


async def test_analyze_distance_metric_parses_bracketed_text_embeddings() -> None:
    # pgvector hands the column back as a text literal when the
    # psycopg adapter isn't registered.
    rows = [
        {"embedding": "[3.0, 4.0]"},
        {"embedding": "[6.0, 8.0]"},
    ]
    driver = FakeRoutingDriver(_routes(rows))

    result = await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    assert result.sampled_rows == 2
    # Norms are 5 and 10 -> mean 7.5
    assert result.mean_magnitude == pytest.approx(7.5)


async def test_analyze_distance_metric_binds_sample_size_as_limit() -> None:
    driver = FakeRoutingDriver(_routes([{"embedding": [1.0, 0.0]}]))

    await analyze_distance_metric(driver, "app", "docs", "embedding", sample_size=42)  # type: ignore[arg-type]

    sample_call = next(call for call in driver.calls if "IS NOT NULL" in call[0])
    assert sample_call[1] == [42]


async def test_analyze_distance_metric_uses_default_sample_size() -> None:
    driver = FakeRoutingDriver(_routes([{"embedding": [1.0, 0.0]}]))

    await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    sample_call = next(call for call in driver.calls if "IS NOT NULL" in call[0])
    assert sample_call[1] == [DEFAULT_SAMPLE_SIZE]


async def test_analyze_distance_metric_rejects_non_positive_sample_size() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(VectorOpsError, match="sample_size"):
        await analyze_distance_metric(driver, "app", "docs", "embedding", sample_size=0)  # type: ignore[arg-type]


async def test_analyze_distance_metric_rejects_oversized_sample_size() -> None:
    """Regression for deep-review scalability P0 #3: an unbounded
    sample_size pulls every requested row into the process for in-
    process work. The shared cap caps the cliff at 50k regardless of
    which entry point is called."""
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(VectorOpsError, match="must be ≤"):
        await analyze_distance_metric(driver, "app", "docs", "embedding", sample_size=10_000_000)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ['docs"; DROP TABLE x', "1bad", "a-b"])
async def test_analyze_distance_metric_rejects_unsafe_identifiers(bad: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(VectorOpsError, match="invalid"):
        await analyze_distance_metric(driver, "app", bad, "embedding")  # type: ignore[arg-type]


async def test_analyze_distance_metric_reports_realistic_distribution_stats() -> None:
    # A small sample where the mean and std are easy to verify by hand.
    rows = [
        {"embedding": [1.0, 0.0]},  # norm 1
        {"embedding": [2.0, 0.0]},  # norm 2
        {"embedding": [3.0, 0.0]},  # norm 3
    ]
    driver = FakeRoutingDriver(_routes(rows))

    result = await analyze_distance_metric(driver, "app", "docs", "embedding")  # type: ignore[arg-type]

    # mean = 2, var = ((1-2)^2 + 0 + (3-2)^2)/3 = 2/3, std = sqrt(2/3)
    assert result.mean_magnitude == pytest.approx(2.0)
    assert result.magnitude_std == pytest.approx(math.sqrt(2 / 3))
    assert result.magnitude_cv == pytest.approx(math.sqrt(2 / 3) / 2.0)


# --- tool wiring -----------------------------------------------------------


async def test_analyze_distance_metric_tool_is_callable_from_a_client() -> None:
    database = FakeDatabase(
        FakeRoutingDriver({"pg_extension": [{"present": 1}], 'WHERE "embedding" IS NOT NULL': []}),
    )
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "analyze_distance_metric" in listed
        result = await client.call_tool(
            "analyze_distance_metric",
            {"schema": "app", "table": "docs", "column": "embedding"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is True
    assert result.structuredContent["recommended_metric"] in {"cosine", "l2", "inner_product"}


async def test_analyze_distance_metric_tool_reports_unavailable_via_client() -> None:
    database = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "analyze_distance_metric",
            {"schema": "app", "table": "docs", "column": "embedding"},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is False
    assert "pgvector extension" in result.structuredContent["rationale"]


# --- helper: _vector_literal ----------------------------------------------


def test_vector_literal_formats_floats_in_bracketed_text() -> None:
    assert _vector_literal([1, 2, 3]) == "[1.0,2.0,3.0]"
    assert _vector_literal([0.1, 0.2]) == "[0.1,0.2]"


# --- cross_table_similarity -----------------------------------------------


_SRC_DIM_LOOKUP = "FROM pg_attribute a"
_KNN_QUERY = "ORDER BY"
_SRC_ROW_FETCH = '"src_id" = '


def _xt_routes(
    *,
    src_dim: int | None = 4,
    tgt_dim: int | None = 4,
    src_embedding: object | None = [1.0, 0.0, 0.0, 0.0],
    knn_rows: list[dict[str, object]] | None = None,
) -> dict[tuple[str, tuple[object, ...] | None], list[dict[str, object]]]:
    """Standard routes for a cross_table_similarity call.

    src/tgt columns: src_schema.src_table.src_emb / tgt_schema.tgt_table.tgt_emb.
    Each catalog lookup is routed by its (schema, table, column) params so
    src and tgt can return different dimensions.
    """
    src_dim_row: list[dict[str, object]] = [{"type_name": "vector", "type_mod": src_dim}] if src_dim is not None else []
    tgt_dim_row: list[dict[str, object]] = [{"type_name": "vector", "type_mod": tgt_dim}] if tgt_dim is not None else []
    src_row: list[dict[str, object]] = [{"embedding": src_embedding}] if src_embedding is not None else []
    knn = knn_rows or []
    return {
        ("pg_extension", None): [{"present": 1}],
        # Catalog dim lookups distinguished by the (schema, table, column) bind tuple.
        (_SRC_DIM_LOOKUP, ("src_schema", "src_table", "src_emb")): src_dim_row,
        (_SRC_DIM_LOOKUP, ("tgt_schema", "tgt_table", "tgt_emb")): tgt_dim_row,
        # Source row fetch routes on the WHERE-clause identifier substring.
        (_SRC_ROW_FETCH, None): src_row,
        # k-NN against the target table.
        (_KNN_QUERY, None): knn,
    }


async def test_cross_table_similarity_reports_unavailable_without_pgvector() -> None:
    driver = FakeParamRoutingDriver({("pg_extension", None): []})

    result = await cross_table_similarity(
        driver,  # type: ignore[arg-type]
        source_schema="src_schema",
        source_table="src_table",
        source_embedding_column="src_emb",
        source_id_column="src_id",
        source_id_value=1,
        target_schema="tgt_schema",
        target_table="tgt_table",
        target_embedding_column="tgt_emb",
    )

    assert result == CrossTableSimilarityResult(
        available=False, source_embedding_found=False, source_dimension=0, matches=[]
    )


async def test_cross_table_similarity_returns_knn_rows_minus_embedding_column() -> None:
    knn = [
        {"id": 10, "title": "alpha", "tgt_emb": "[1.0,0.0,0.0,0.0]", "mcpg_distance": 0.0},
        {"id": 11, "title": "beta", "tgt_emb": "[0.0,1.0,0.0,0.0]", "mcpg_distance": 1.41},
    ]
    driver = FakeParamRoutingDriver(_xt_routes(knn_rows=knn))

    result = await cross_table_similarity(
        driver,  # type: ignore[arg-type]
        source_schema="src_schema",
        source_table="src_table",
        source_embedding_column="src_emb",
        source_id_column="src_id",
        source_id_value=1,
        target_schema="tgt_schema",
        target_table="tgt_table",
        target_embedding_column="tgt_emb",
        k=2,
    )

    assert result.available is True
    assert result.source_embedding_found is True
    assert result.source_dimension == 4
    assert result.matches == [
        CrossTableMatch(distance=0.0, row={"id": 10, "title": "alpha"}),
        CrossTableMatch(distance=1.41, row={"id": 11, "title": "beta"}),
    ]


async def test_cross_table_similarity_binds_the_source_embedding_as_a_pgvector_literal() -> None:
    driver = FakeParamRoutingDriver(_xt_routes(knn_rows=[]))

    await cross_table_similarity(
        driver,  # type: ignore[arg-type]
        source_schema="src_schema",
        source_table="src_table",
        source_embedding_column="src_emb",
        source_id_column="src_id",
        source_id_value=42,
        target_schema="tgt_schema",
        target_table="tgt_table",
        target_embedding_column="tgt_emb",
        k=5,
    )

    knn_call = next(call for call in driver.calls if "ORDER BY" in call[0])
    # Two bind copies (SELECT distance + ORDER BY distance) plus the LIMIT.
    assert knn_call[1] == ["[1.0,0.0,0.0,0.0]", "[1.0,0.0,0.0,0.0]", 5]


async def test_cross_table_similarity_returns_not_found_when_source_id_misses() -> None:
    driver = FakeParamRoutingDriver(_xt_routes(src_embedding=None))

    result = await cross_table_similarity(
        driver,  # type: ignore[arg-type]
        source_schema="src_schema",
        source_table="src_table",
        source_embedding_column="src_emb",
        source_id_column="src_id",
        source_id_value=999,
        target_schema="tgt_schema",
        target_table="tgt_table",
        target_embedding_column="tgt_emb",
    )

    assert result.available is True
    assert result.source_embedding_found is False
    assert result.matches == []
    # Crucially: no k-NN query was issued — the missing source row short-circuits.
    assert not any("ORDER BY" in call[0] for call in driver.calls)


async def test_cross_table_similarity_raises_on_unknown_metric() -> None:
    driver = FakeParamRoutingDriver(_xt_routes())
    with pytest.raises(VectorOpsError, match="unknown vector metric"):
        await cross_table_similarity(
            driver,  # type: ignore[arg-type]
            source_schema="src_schema",
            source_table="src_table",
            source_embedding_column="src_emb",
            source_id_column="src_id",
            source_id_value=1,
            target_schema="tgt_schema",
            target_table="tgt_table",
            target_embedding_column="tgt_emb",
            metric="manhattan",
        )


async def test_cross_table_similarity_rejects_non_positive_k() -> None:
    driver = FakeParamRoutingDriver(_xt_routes())
    with pytest.raises(VectorOpsError, match="k must be"):
        await cross_table_similarity(
            driver,  # type: ignore[arg-type]
            source_schema="src_schema",
            source_table="src_table",
            source_embedding_column="src_emb",
            source_id_column="src_id",
            source_id_value=1,
            target_schema="tgt_schema",
            target_table="tgt_table",
            target_embedding_column="tgt_emb",
            k=0,
        )


async def test_cross_table_similarity_raises_when_source_column_is_not_vector() -> None:
    driver = FakeParamRoutingDriver(_xt_routes(src_dim=None))
    with pytest.raises(VectorOpsError, match=r"src_schema\.src_table\.src_emb is not a pgvector"):
        await cross_table_similarity(
            driver,  # type: ignore[arg-type]
            source_schema="src_schema",
            source_table="src_table",
            source_embedding_column="src_emb",
            source_id_column="src_id",
            source_id_value=1,
            target_schema="tgt_schema",
            target_table="tgt_table",
            target_embedding_column="tgt_emb",
        )


async def test_cross_table_similarity_raises_when_target_column_is_not_vector() -> None:
    driver = FakeParamRoutingDriver(_xt_routes(tgt_dim=None))
    with pytest.raises(VectorOpsError, match=r"tgt_schema\.tgt_table\.tgt_emb is not a pgvector"):
        await cross_table_similarity(
            driver,  # type: ignore[arg-type]
            source_schema="src_schema",
            source_table="src_table",
            source_embedding_column="src_emb",
            source_id_column="src_id",
            source_id_value=1,
            target_schema="tgt_schema",
            target_table="tgt_table",
            target_embedding_column="tgt_emb",
        )


async def test_cross_table_similarity_raises_on_dimension_mismatch_up_front() -> None:
    driver = FakeParamRoutingDriver(_xt_routes(src_dim=4, tgt_dim=8))
    with pytest.raises(VectorOpsError, match=r"dimension mismatch.*vector\(4\).*vector\(8\)"):
        await cross_table_similarity(
            driver,  # type: ignore[arg-type]
            source_schema="src_schema",
            source_table="src_table",
            source_embedding_column="src_emb",
            source_id_column="src_id",
            source_id_value=1,
            target_schema="tgt_schema",
            target_table="tgt_table",
            target_embedding_column="tgt_emb",
        )
    # The mismatch is caught before the source row is even fetched.
    assert not any('"src_id" = ' in call[0] for call in driver.calls)


async def test_cross_table_similarity_raises_when_source_row_has_unparseable_embedding() -> None:
    driver = FakeParamRoutingDriver(_xt_routes(src_embedding="not-a-vector-literal"))
    with pytest.raises(VectorOpsError, match="NULL or unparseable embedding"):
        await cross_table_similarity(
            driver,  # type: ignore[arg-type]
            source_schema="src_schema",
            source_table="src_table",
            source_embedding_column="src_emb",
            source_id_column="src_id",
            source_id_value=1,
            target_schema="tgt_schema",
            target_table="tgt_table",
            target_embedding_column="tgt_emb",
        )


@pytest.mark.parametrize("bad", ['x"; DROP TABLE y', "1bad", "a-b"])
async def test_cross_table_similarity_rejects_unsafe_identifiers(bad: str) -> None:
    driver = FakeParamRoutingDriver(_xt_routes())
    with pytest.raises(VectorOpsError, match="invalid"):
        await cross_table_similarity(
            driver,  # type: ignore[arg-type]
            source_schema=bad,
            source_table="src_table",
            source_embedding_column="src_emb",
            source_id_column="src_id",
            source_id_value=1,
            target_schema="tgt_schema",
            target_table="tgt_table",
            target_embedding_column="tgt_emb",
        )


async def test_cross_table_similarity_tool_is_listed_and_callable() -> None:
    database = FakeDatabase(FakeParamRoutingDriver(_xt_routes(knn_rows=[])))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "cross_table_similarity" in listed
        result = await client.call_tool(
            "cross_table_similarity",
            {
                "source_schema": "src_schema",
                "source_table": "src_table",
                "source_embedding_column": "src_emb",
                "source_id_column": "src_id",
                "source_id_value": 1,
                "target_schema": "tgt_schema",
                "target_table": "tgt_table",
                "target_embedding_column": "tgt_emb",
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is True
    assert result.structuredContent["source_embedding_found"] is True


# --- cluster_vectors helpers ----------------------------------------------


def test_squared_distance_is_zero_for_identical_vectors() -> None:
    assert _squared_distance([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 0.0
    # (3-0)^2 + (4-0)^2 = 9 + 16 = 25
    assert _squared_distance([3.0, 4.0], [0.0, 0.0]) == 25.0


def test_squared_distance_raises_on_dim_mismatch() -> None:
    with pytest.raises(ValueError):
        _squared_distance([1.0, 0.0], [1.0, 0.0, 0.0])


def test_cosine_distance_is_zero_for_aligned_vectors() -> None:
    assert _cosine_distance([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
    # Orthogonal -> distance 1.
    assert _cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)


def test_cosine_distance_is_one_for_zero_vector() -> None:
    # By convention, zero magnitude → max distance.
    assert _cosine_distance([0.0, 0.0], [1.0, 1.0]) == 1.0


def test_normalize_in_place_yields_unit_norm_vectors() -> None:
    vec = [3.0, 4.0]
    _normalize_in_place(vec)
    assert _l2_norm(vec) == pytest.approx(1.0)
    # Zero vectors pass through unchanged.
    zero = [0.0, 0.0, 0.0]
    _normalize_in_place(zero)
    assert zero == [0.0, 0.0, 0.0]


# --- cluster_vectors end-to-end -------------------------------------------


_VECTOR_DIM_LOOKUP = "FROM pg_attribute a"
_SAMPLE_QUERY = "IS NOT NULL LIMIT"


def _cluster_routes(
    *,
    dim: int | None = 2,
    sample_rows: list[dict[str, object]] | None = None,
) -> dict[tuple[str, tuple[object, ...] | None], list[dict[str, object]]]:
    """Standard FakeParamRoutingDriver routes for cluster_vectors."""
    dim_rows: list[dict[str, object]] = [{"type_name": "vector", "type_mod": dim}] if dim is not None else []
    return {
        ("pg_extension", None): [{"present": 1}],
        (_VECTOR_DIM_LOOKUP, None): dim_rows,
        (_SAMPLE_QUERY, None): sample_rows or [],
    }


async def test_cluster_vectors_reports_unavailable_without_pgvector() -> None:
    driver = FakeParamRoutingDriver({("pg_extension", None): []})

    result = await cluster_vectors(
        driver,
        "app",
        "docs",
        "embedding",
        k=2,  # type: ignore[arg-type]
    )

    assert result == ClusterVectorsResult(
        available=False,
        sampled_rows=0,
        dimension=0,
        metric="l2",
        iterations=0,
        converged=False,
        inertia=0.0,
        centroids=[],
        assignments=[],
    )


async def test_cluster_vectors_finds_two_well_separated_clusters() -> None:
    # 6 points: 3 around (0,0), 3 around (10,10). k-means with k=2 must
    # assign each cluster cohesively regardless of starting seed.
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.1, 0.0]},
        {"embedding": [0.0, 0.1]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.1, 10.0]},
        {"embedding": [10.0, 10.1]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await cluster_vectors(
        driver,
        "app",
        "docs",
        "embedding",
        k=2,  # type: ignore[arg-type]
    )

    assert result.available is True
    assert result.sampled_rows == 6
    assert result.dimension == 2
    assert result.metric == "l2"
    # Two clusters of 3 each.
    sizes = sorted(c.size for c in result.centroids)
    assert sizes == [3, 3]
    # First three rows share one cluster, last three share the other.
    a_labels = {result.assignments[i].cluster for i in range(3)}
    b_labels = {result.assignments[i].cluster for i in range(3, 6)}
    assert len(a_labels) == 1 and len(b_labels) == 1
    assert a_labels != b_labels
    # Inertia is small for tight clusters.
    assert result.inertia < 1.0
    # Algorithm converges on this toy set.
    assert result.converged is True


async def test_cluster_vectors_uses_id_column_when_provided() -> None:
    sample_rows: list[dict[str, object]] = [
        {"row_id": "alpha", "embedding": [0.0, 0.0]},
        {"row_id": "beta", "embedding": [0.1, 0.1]},
        {"row_id": "gamma", "embedding": [10.0, 10.0]},
        {"row_id": "delta", "embedding": [10.1, 10.1]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await cluster_vectors(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
        id_column="row_id",
    )

    ids = sorted(a.id for a in result.assignments)
    assert ids == ["alpha", "beta", "delta", "gamma"]


async def test_cluster_vectors_supports_cosine_metric() -> None:
    # Two unit vectors per direction.
    sample_rows: list[dict[str, object]] = [
        {"embedding": [1.0, 0.0]},
        {"embedding": [0.99, 0.14]},  # ~7° from [1, 0]
        {"embedding": [-1.0, 0.0]},
        {"embedding": [-0.99, -0.14]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await cluster_vectors(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
        metric="cosine",
    )

    assert result.metric == "cosine"
    # Each cluster has 2 members. Centroids should be ~ unit norm.
    sizes = sorted(c.size for c in result.centroids)
    assert sizes == [2, 2]
    for c in result.centroids:
        assert _l2_norm(c.centroid) == pytest.approx(1.0, abs=1e-6)


async def test_cluster_vectors_is_deterministic_given_a_seed() -> None:
    sample_rows: list[dict[str, object]] = [
        {"embedding": [v, v]} for v in (0.0, 0.1, 0.2, 5.0, 5.1, 5.2, 10.0, 10.1, 10.2)
    ]
    # Same seed -> same centroids on two runs.
    driver1 = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))
    driver2 = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    r1 = await cluster_vectors(driver1, "app", "docs", "embedding", k=3, seed=7)  # type: ignore[arg-type]
    r2 = await cluster_vectors(driver2, "app", "docs", "embedding", k=3, seed=7)  # type: ignore[arg-type]

    assert [c.centroid for c in r1.centroids] == [c.centroid for c in r2.centroids]
    assert [a.cluster for a in r1.assignments] == [a.cluster for a in r2.assignments]


async def test_cluster_vectors_binds_sample_size_as_limit() -> None:
    sample_rows: list[dict[str, object]] = [{"embedding": [float(i), 0.0]} for i in range(10)]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    await cluster_vectors(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
        sample_size=500,
    )

    sample_call = next(call for call in driver.calls if "IS NOT NULL" in call[0])
    assert sample_call[1] == [500]


async def test_cluster_vectors_skips_rows_with_wrong_dimension() -> None:
    # Declared dim is 2; one row is 3-D and must be silently skipped.
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.1, 0.0]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.1, 10.0]},
        {"embedding": [1.0, 2.0, 3.0]},  # bad — wrong dim
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await cluster_vectors(driver, "app", "docs", "embedding", k=2)  # type: ignore[arg-type]

    assert result.sampled_rows == 4


async def test_cluster_vectors_rejects_not_enough_rows_for_k() -> None:
    # 3 rows, k=2 -> needs at least 4 (2*k).
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [1.0, 0.0]},
        {"embedding": [0.0, 1.0]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    with pytest.raises(VectorOpsError, match="not enough rows to cluster"):
        await cluster_vectors(driver, "app", "docs", "embedding", k=2)  # type: ignore[arg-type]


async def test_cluster_vectors_raises_when_column_is_not_pgvector() -> None:
    driver = FakeParamRoutingDriver(_cluster_routes(dim=None))
    with pytest.raises(VectorOpsError, match="is not a pgvector"):
        await cluster_vectors(driver, "app", "docs", "embedding", k=2)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"metric": "manhattan", "k": 2}, "metric"),
        ({"k": 0}, "k must be"),
        ({"k": 1}, "k must be"),
        ({"k": 2, "sample_size": 0}, "sample_size"),
        ({"k": 2, "max_iterations": 0}, "max_iterations"),
    ],
)
async def test_cluster_vectors_validates_arguments(kwargs: dict[str, object], match: str) -> None:
    driver = FakeParamRoutingDriver(_cluster_routes())
    with pytest.raises(VectorOpsError, match=match):
        await cluster_vectors(driver, "app", "docs", "embedding", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ['x"; DROP TABLE y', "1bad", "a-b"])
async def test_cluster_vectors_rejects_unsafe_identifiers(bad: str) -> None:
    driver = FakeParamRoutingDriver(_cluster_routes())
    with pytest.raises(VectorOpsError, match="invalid"):
        await cluster_vectors(driver, bad, "docs", "embedding", k=2)  # type: ignore[arg-type]


async def test_cluster_vectors_reports_default_constants_via_module() -> None:
    # Lightweight sanity that the surfaced defaults match the docstring.
    assert DEFAULT_CLUSTER_SAMPLE_SIZE == 5000
    assert DEFAULT_MAX_ITERATIONS == 20


async def test_cluster_vectors_avoids_duplicate_centroids_when_many_clusters_collapse() -> None:
    # Regression for the gemini PR #52 bug: when several clusters end
    # up empty in the same iteration, each was re-seeded onto the
    # *same* worst-fit point (distances[] wasn't updated inside the
    # loop), yielding duplicate centroids. We force the situation by
    # asking for k=4 over 8 well-separated points so it's still
    # sensible, but seed with a value where the initial seeding could
    # plausibly leave some clusters underfilled; either way the final
    # centroids must all be distinct.
    sample_rows: list[dict[str, object]] = [
        {"embedding": [x, y]}
        for x, y in [
            (0.0, 0.0),
            (0.1, 0.0),
            (10.0, 0.0),
            (10.1, 0.0),
            (0.0, 10.0),
            (0.1, 10.0),
            (10.0, 10.0),
            (10.1, 10.0),
        ]
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await cluster_vectors(
        driver,
        "app",
        "docs",
        "embedding",
        k=4,
        seed=1,  # type: ignore[arg-type]
    )

    # Render each centroid as a tuple so we can use set membership.
    distinct = {tuple(c.centroid) for c in result.centroids}
    assert len(distinct) == len(result.centroids), (
        f"duplicate centroids slipped through: {[c.centroid for c in result.centroids]}"
    )


async def test_cluster_vectors_tool_is_callable_from_a_client() -> None:
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.1, 0.1]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.1, 10.1]},
    ]
    database = FakeDatabase(FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows)))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "cluster_vectors" in listed
        result = await client.call_tool(
            "cluster_vectors",
            {"schema": "app", "table": "docs", "embedding_column": "embedding", "k": 2},
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is True
    assert len(result.structuredContent["centroids"]) == 2


# --- detect_vector_outliers -----------------------------------------------


async def test_detect_vector_outliers_reports_unavailable_without_pgvector() -> None:
    driver = FakeParamRoutingDriver({("pg_extension", None): []})

    result = await detect_vector_outliers(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
    )

    assert result == VectorOutlierResult(
        available=False,
        sampled_rows=0,
        dimension=0,
        metric="l2",
        k=2,
        zscore_threshold=DEFAULT_OUTLIER_ZSCORE,
        total_outliers=0,
        outliers=[],
        cluster_stats=[],
    )


async def test_detect_vector_outliers_flags_a_row_far_from_its_cluster() -> None:
    # Two tight clusters of 5 + 5 points, plus one obvious outlier far
    # from either centroid.
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.1, 0.0]},
        {"embedding": [0.0, 0.1]},
        {"embedding": [0.05, 0.05]},
        {"embedding": [0.1, 0.1]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.1, 10.0]},
        {"embedding": [10.0, 10.1]},
        {"embedding": [10.05, 10.05]},
        {"embedding": [10.1, 10.1]},
        {"embedding": [100.0, 100.0]},  # outlier — distant from either centroid
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await detect_vector_outliers(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
        zscore_threshold=2.0,
    )

    assert result.available is True
    assert result.sampled_rows == 11
    assert result.dimension == 2
    assert result.total_outliers >= 1
    # The row at index 10 is the only one ~127 units from the nearest cluster.
    flagged_ids = {o.id for o in result.outliers}
    assert 10 in flagged_ids
    # And its z-score should be the highest by far.
    top = max(result.outliers, key=lambda o: o.zscore)
    assert top.id == 10


async def test_detect_vector_outliers_respects_id_column() -> None:
    sample_rows: list[dict[str, object]] = [
        {"row_id": "a1", "embedding": [0.0, 0.0]},
        {"row_id": "a2", "embedding": [0.1, 0.0]},
        {"row_id": "a3", "embedding": [0.0, 0.1]},
        {"row_id": "a4", "embedding": [0.05, 0.05]},
        {"row_id": "b1", "embedding": [10.0, 10.0]},
        {"row_id": "b2", "embedding": [10.1, 10.0]},
        {"row_id": "b3", "embedding": [10.0, 10.1]},
        {"row_id": "b4", "embedding": [10.05, 10.05]},
        {"row_id": "weird", "embedding": [100.0, 100.0]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await detect_vector_outliers(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        id_column="row_id",
        k=2,
        zscore_threshold=2.0,
    )

    flagged_ids = {o.id for o in result.outliers}
    assert "weird" in flagged_ids


async def test_detect_vector_outliers_caps_results_at_max_results() -> None:
    # Many points scattered widely so several get flagged.
    sample_rows: list[dict[str, object]] = [{"embedding": [float(i % 3), float(i // 3)]} for i in range(40)]
    # Add a few extreme outliers
    sample_rows.extend([{"embedding": [1000.0, 1000.0 + i]} for i in range(5)])
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await detect_vector_outliers(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=3,
        zscore_threshold=1.0,
        max_results=2,
    )

    assert len(result.outliers) == 2
    assert result.total_outliers >= 2
    # Sorted by z-score descending.
    assert result.outliers[0].zscore >= result.outliers[1].zscore


async def test_detect_vector_outliers_returns_empty_when_no_outliers() -> None:
    # Tight, well-balanced clusters with no extreme points.
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.1, 0.0]},
        {"embedding": [0.0, 0.1]},
        {"embedding": [0.05, 0.05]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.1, 10.0]},
        {"embedding": [10.0, 10.1]},
        {"embedding": [10.05, 10.05]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await detect_vector_outliers(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
        zscore_threshold=10.0,  # very strict
    )

    assert result.outliers == []
    assert result.total_outliers == 0


async def test_detect_vector_outliers_reports_per_cluster_stats() -> None:
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.1, 0.0]},
        {"embedding": [0.0, 0.1]},
        {"embedding": [0.05, 0.05]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.1, 10.0]},
        {"embedding": [10.0, 10.1]},
        {"embedding": [10.05, 10.05]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await detect_vector_outliers(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
    )

    assert len(result.cluster_stats) == 2
    # Both clusters should be ~4 in size.
    sizes = sorted(s.size for s in result.cluster_stats)
    assert sizes == [4, 4]
    # Tight clusters → small means + small stds.
    for stats in result.cluster_stats:
        assert stats.mean_distance < 1.0
        assert stats.std_distance < 1.0


async def test_detect_vector_outliers_records_zero_std_for_uniform_clusters() -> None:
    # Two perfectly tight clusters: 4 identical points each. Every
    # cluster's mean + std of within-cluster distances should be 0.0
    # and no row should be flagged.
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.0, 0.0]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.0, 10.0]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    result = await detect_vector_outliers(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
        zscore_threshold=2.0,
    )

    assert result.outliers == []
    for stats in result.cluster_stats:
        assert stats.mean_distance == 0.0
        assert stats.std_distance == 0.0


async def test_detect_vector_outliers_binds_sample_size_as_limit() -> None:
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.1, 0.1]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.1, 10.1]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    await detect_vector_outliers(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        k=2,
        sample_size=42,
    )

    bound = [params for sql, params, _ in driver.calls if "IS NOT NULL LIMIT" in sql]  # type: ignore[attr-defined]
    assert bound and bound[-1] == [42]


async def test_detect_vector_outliers_raises_when_column_is_not_pgvector() -> None:
    driver = FakeParamRoutingDriver(_cluster_routes(dim=None))

    with pytest.raises(VectorOpsError, match="not a pgvector"):
        await detect_vector_outliers(
            driver,  # type: ignore[arg-type]
            "app",
            "docs",
            "embedding",
            k=2,
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"k": 1}, "k must be at least 2"),
        ({"sample_size": 0}, "must be at least 1"),
        ({"max_iterations": 0}, "max_iterations must be at least 1"),
        ({"metric": "manhattan"}, "unknown metric"),
        ({"zscore_threshold": 0.0}, "zscore_threshold must be > 0"),
        ({"zscore_threshold": -1.0}, "zscore_threshold must be > 0"),
        ({"max_results": 0}, "max_results must be at least 1"),
    ],
)
async def test_detect_vector_outliers_validates_arguments(kwargs: dict[str, object], match: str) -> None:
    driver = FakeParamRoutingDriver(_cluster_routes())
    with pytest.raises(VectorOpsError, match=match):
        await detect_vector_outliers(driver, "app", "docs", "embedding", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["bad name", "ta;ble", "1abc", '"x"'])
async def test_detect_vector_outliers_rejects_unsafe_identifiers(bad: str) -> None:
    driver = FakeParamRoutingDriver(_cluster_routes())
    with pytest.raises(VectorOpsError, match="invalid"):
        await detect_vector_outliers(driver, bad, "docs", "embedding", k=2)  # type: ignore[arg-type]


async def test_detect_vector_outliers_rejects_not_enough_rows_for_k() -> None:
    # k=2 needs at least 4 parseable rows.
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [1.0, 1.0]},
        {"embedding": [2.0, 2.0]},
    ]
    driver = FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows))

    with pytest.raises(VectorOpsError, match="not enough rows for outlier detection"):
        await detect_vector_outliers(
            driver,  # type: ignore[arg-type]
            "app",
            "docs",
            "embedding",
            k=2,
        )


def test_detect_vector_outliers_reports_default_constants_via_module() -> None:
    assert DEFAULT_OUTLIER_K == 8
    assert DEFAULT_OUTLIER_ZSCORE == 3.0
    assert DEFAULT_OUTLIER_MAX_RESULTS == 100


async def test_detect_vector_outliers_tool_is_callable_from_a_client() -> None:
    sample_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 0.0]},
        {"embedding": [0.1, 0.0]},
        {"embedding": [0.0, 0.1]},
        {"embedding": [0.05, 0.05]},
        {"embedding": [10.0, 10.0]},
        {"embedding": [10.1, 10.0]},
        {"embedding": [10.0, 10.1]},
        {"embedding": [10.05, 10.05]},
        {"embedding": [100.0, 100.0]},
    ]
    database = FakeDatabase(FakeParamRoutingDriver(_cluster_routes(sample_rows=sample_rows)))
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "detect_vector_outliers" in listed
        result = await client.call_tool(
            "detect_vector_outliers",
            {
                "schema": "app",
                "table": "docs",
                "embedding_column": "embedding",
                "k": 2,
                "zscore_threshold": 2.0,
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is True
    assert result.structuredContent["total_outliers"] >= 1


# --- monitor_embedding_drift ----------------------------------------------


_DRIFT_WINDOW_QUERY = "AND embedding IS NOT NULL"


def _drift_routes(
    *,
    dim: int | None = 2,
    baseline_rows: list[dict[str, object]] | None = None,
    current_rows: list[dict[str, object]] | None = None,
) -> dict[
    tuple[str, tuple[object, ...] | None],
    list[dict[str, object]],
]:
    """FakeParamRoutingDriver routes for monitor_embedding_drift.

    Routes by (query-substring, params-tuple) so the baseline and
    current window calls can return different sample sets even
    though their SQL is identical.

    PR (scalability P0 #4 follow-up) added a COUNT round-trip before
    the sample fetch so the function can choose between the
    bare-LIMIT (small-window) and probability-filter (large-window)
    paths. Test fixtures stub the COUNT result to equal the number
    of supplied rows so the small-window path runs (no random()
    filter, params shape stays `[start, end, sample_size]`).
    """
    dim_rows: list[dict[str, object]] = [{"type_name": "vector", "type_mod": dim}] if dim is not None else []
    baseline_count = len(baseline_rows or [])
    current_count = len(current_rows or [])
    baseline_count_key = ("SELECT count(*)", ("2026-01-01", "2026-02-01"))
    current_count_key = ("SELECT count(*)", ("2026-02-01", "2026-03-01"))
    baseline_key = ('AND "embedding" IS NOT NULL', ("2026-01-01", "2026-02-01", 5000))
    current_key = ('AND "embedding" IS NOT NULL', ("2026-02-01", "2026-03-01", 5000))
    return {
        ("pg_extension", None): [{"present": 1}],
        ("FROM pg_attribute a", None): dim_rows,
        baseline_count_key: [{"n": baseline_count}],
        current_count_key: [{"n": current_count}],
        baseline_key: baseline_rows or [],
        current_key: current_rows or [],
    }


def test_relative_change_handles_zero_baseline() -> None:
    assert _relative_change(0.0, 0.0) == 0.0
    assert _relative_change(0.0, 1.0) == math.inf
    assert _relative_change(10.0, 12.0) == pytest.approx(0.2)
    assert _relative_change(10.0, 8.0) == pytest.approx(-0.2)


async def test_monitor_embedding_drift_reports_unavailable_without_pgvector() -> None:
    driver = FakeParamRoutingDriver({("pg_extension", None): []})

    result = await monitor_embedding_drift(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    assert result.available is False
    assert result.insufficient_data is True
    assert result.drift_detected is False
    assert result.dimension == 0
    assert "not installed" in result.notes


async def test_monitor_embedding_drift_flags_centroid_drift_above_threshold() -> None:
    # Baseline cluster around (1, 0); current cluster around (0, 1).
    # Their centroids point in orthogonal directions → cosine distance 1.0.
    baseline_rows: list[dict[str, object]] = [
        {"embedding": [1.0, 0.0]},
        {"embedding": [1.0, 0.0]},
        {"embedding": [1.0, 0.0]},
    ]
    current_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 1.0]},
        {"embedding": [0.0, 1.0]},
        {"embedding": [0.0, 1.0]},
    ]
    driver = FakeParamRoutingDriver(_drift_routes(baseline_rows=baseline_rows, current_rows=current_rows))

    result = await monitor_embedding_drift(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    assert result.available is True
    assert result.insufficient_data is False
    assert result.drift_detected is True
    assert result.dimension == 2
    assert result.centroid_cosine_distance == pytest.approx(1.0)
    assert result.baseline.sampled_rows == 3
    assert result.current.sampled_rows == 3
    assert result.baseline.centroid == [pytest.approx(1.0), pytest.approx(0.0)]
    assert result.current.centroid == [pytest.approx(0.0), pytest.approx(1.0)]


async def test_monitor_embedding_drift_returns_no_drift_for_identical_distributions() -> None:
    same_rows: list[dict[str, object]] = [
        {"embedding": [1.0, 1.0]},
        {"embedding": [1.0, 1.0]},
        {"embedding": [1.0, 1.0]},
    ]
    driver = FakeParamRoutingDriver(_drift_routes(baseline_rows=same_rows, current_rows=list(same_rows)))

    result = await monitor_embedding_drift(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    assert result.drift_detected is False
    assert result.centroid_cosine_distance == pytest.approx(0.0)
    assert result.norm_mean_relative_change == pytest.approx(0.0)
    assert result.norm_std_relative_change == pytest.approx(0.0)


async def test_monitor_embedding_drift_detects_norm_mean_change_independently() -> None:
    # Both windows have the same direction but different magnitudes —
    # cosine distance ≈ 0, but norm_mean_relative_change should be
    # large.
    baseline_rows: list[dict[str, object]] = [
        {"embedding": [1.0, 1.0]},
        {"embedding": [1.0, 1.0]},
    ]
    current_rows: list[dict[str, object]] = [
        {"embedding": [3.0, 3.0]},
        {"embedding": [3.0, 3.0]},
    ]
    driver = FakeParamRoutingDriver(_drift_routes(baseline_rows=baseline_rows, current_rows=current_rows))

    result = await monitor_embedding_drift(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    assert result.centroid_cosine_distance == pytest.approx(0.0, abs=1e-9)
    # Mean norm grew 3x — relative change is +2.0.
    assert result.norm_mean_relative_change == pytest.approx(2.0)


async def test_monitor_embedding_drift_reports_insufficient_data_for_empty_window() -> None:
    # Current window empty.
    baseline_rows: list[dict[str, object]] = [
        {"embedding": [1.0, 0.0]},
        {"embedding": [1.0, 0.0]},
    ]
    driver = FakeParamRoutingDriver(_drift_routes(baseline_rows=baseline_rows, current_rows=[]))

    result = await monitor_embedding_drift(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    assert result.insufficient_data is True
    assert result.drift_detected is False
    assert result.baseline.sampled_rows == 2
    assert result.current.sampled_rows == 0
    assert "insufficient data" in result.notes


async def test_monitor_embedding_drift_skips_dimension_mismatched_rows() -> None:
    # Mix in a wrong-dimension embedding — it should be silently
    # dropped by `_parse_embedding`/dimension check.
    baseline_rows: list[dict[str, object]] = [
        {"embedding": [1.0, 0.0]},
        {"embedding": [1.0, 0.0, 0.0]},  # wrong dim — dropped
        {"embedding": [1.0, 0.0]},
    ]
    current_rows: list[dict[str, object]] = [{"embedding": [1.0, 0.0]}]
    driver = FakeParamRoutingDriver(_drift_routes(baseline_rows=baseline_rows, current_rows=current_rows))

    result = await monitor_embedding_drift(
        driver,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    assert result.baseline.sampled_rows == 2
    assert result.current.sampled_rows == 1


async def test_monitor_embedding_drift_raises_when_column_is_not_pgvector() -> None:
    driver = FakeParamRoutingDriver(_drift_routes(dim=None))

    with pytest.raises(VectorOpsError, match="not a pgvector"):
        await monitor_embedding_drift(
            driver,  # type: ignore[arg-type]
            "app",
            "docs",
            "embedding",
            "created_at",
            baseline_start="2026-01-01",
            baseline_end="2026-02-01",
            current_start="2026-02-01",
            current_end="2026-03-01",
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"sample_size": 0}, "must be at least 1"),
        ({"drift_threshold": -0.1}, "drift_threshold must be >= 0"),
        ({"baseline_end": "2026-01-01"}, "baseline window end must be after start"),
        ({"current_end": "2026-02-01"}, "current window end must be after start"),
    ],
)
async def test_monitor_embedding_drift_validates_arguments(kwargs: dict[str, object], match: str) -> None:
    driver = FakeParamRoutingDriver(_drift_routes())
    base_kwargs: dict[str, object] = {
        "baseline_start": "2026-01-01",
        "baseline_end": "2026-02-01",
        "current_start": "2026-02-01",
        "current_end": "2026-03-01",
    }
    base_kwargs.update(kwargs)
    with pytest.raises(VectorOpsError, match=match):
        await monitor_embedding_drift(driver, "app", "docs", "embedding", "created_at", **base_kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["bad name", "ta;ble", "1abc", '"x"'])
async def test_monitor_embedding_drift_rejects_unsafe_identifiers(bad: str) -> None:
    driver = FakeParamRoutingDriver(_drift_routes())
    with pytest.raises(VectorOpsError, match="invalid"):
        await monitor_embedding_drift(
            driver,  # type: ignore[arg-type]
            bad,
            "docs",
            "embedding",
            "created_at",
            baseline_start="2026-01-01",
            baseline_end="2026-02-01",
            current_start="2026-02-01",
            current_end="2026-03-01",
        )


def test_monitor_embedding_drift_reports_default_constants_via_module() -> None:
    assert DEFAULT_DRIFT_SAMPLE_SIZE == 5000
    assert DEFAULT_DRIFT_THRESHOLD == 0.05


async def test_monitor_embedding_drift_tool_is_callable_from_a_client() -> None:
    baseline_rows: list[dict[str, object]] = [
        {"embedding": [1.0, 0.0]},
        {"embedding": [1.0, 0.0]},
    ]
    current_rows: list[dict[str, object]] = [
        {"embedding": [0.0, 1.0]},
        {"embedding": [0.0, 1.0]},
    ]
    database = FakeDatabase(
        FakeParamRoutingDriver(_drift_routes(baseline_rows=baseline_rows, current_rows=current_rows))
    )
    server = create_server(_SETTINGS, database=database)  # type: ignore[arg-type]

    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert "monitor_embedding_drift" in listed
        result = await client.call_tool(
            "monitor_embedding_drift",
            {
                "schema": "app",
                "table": "docs",
                "embedding_column": "embedding",
                "timestamp_column": "created_at",
                "baseline_start": "2026-01-01",
                "baseline_end": "2026-02-01",
                "current_start": "2026-02-01",
                "current_end": "2026-03-01",
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["available"] is True
    assert result.structuredContent["drift_detected"] is True


# Regression coverage for the probability-based sampling path (deep-
# review scalability P0 #4, follow-up to PR #99). The tests assert SQL
# shape directly because verifying statistical behaviour against a
# fake driver would just re-state the calling convention.


async def test_monitor_embedding_drift_small_window_skips_random_filter() -> None:
    """When the COUNT round-trip reports the window is at or below
    ``sample_size``, _fetch_window_vectors must skip both the random
    filter and the sort — a bare LIMIT is strictly cheaper. Tested
    via SQL shape because counting calls is the only way to confirm
    no ``random()`` slipped in."""

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[object]]] = []

        async def execute_query(
            self,
            sql: str,
            params: list[object] | None = None,
            force_readonly: bool = False,
        ) -> list[object]:
            from mcpg._vendor.sql import SqlDriver

            self.calls.append((sql, list(params or [])))
            if "pg_extension" in sql:
                return [SqlDriver.RowResult(cells={"present": 1})]
            if "FROM pg_attribute a" in sql:
                return [SqlDriver.RowResult(cells={"type_name": "vector", "type_mod": 2})]
            if "SELECT count(*)" in sql:
                # Both windows look "small" so the small-window path runs.
                return [SqlDriver.RowResult(cells={"n": 3})]
            # Sample fetch — return three usable rows.
            return [SqlDriver.RowResult(cells={"embedding": [1.0, 0.0]}) for _ in range(3)]

    recorder = _Recorder()
    await monitor_embedding_drift(
        recorder,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    sample_calls = [
        sql for sql, _params in recorder.calls if 'AND "embedding" IS NOT NULL' in sql and "SELECT count(*)" not in sql
    ]
    assert len(sample_calls) == 2, sample_calls
    for sql in sample_calls:
        # Small-window path: no random filter, no ORDER BY RANDOM, just LIMIT.
        assert "random()" not in sql
        assert "ORDER BY" not in sql or "ORDER BY id" not in sql  # incidental id order is fine, RANDOM is not
        assert "RANDOM()" not in sql
        assert "LIMIT %s" in sql


async def test_monitor_embedding_drift_large_window_applies_probability_filter() -> None:
    """When the COUNT reports the window is much larger than
    ``sample_size``, the sample query must add ``WHERE random() < %s``
    with a computed probability and the params list grows to 4
    entries: [start, end, p, sample_size]."""

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[object]]] = []

        async def execute_query(
            self,
            sql: str,
            params: list[object] | None = None,
            force_readonly: bool = False,
        ) -> list[object]:
            from mcpg._vendor.sql import SqlDriver

            self.calls.append((sql, list(params or [])))
            if "pg_extension" in sql:
                return [SqlDriver.RowResult(cells={"present": 1})]
            if "FROM pg_attribute a" in sql:
                return [SqlDriver.RowResult(cells={"type_name": "vector", "type_mod": 2})]
            if "SELECT count(*)" in sql:
                # 1 million rows in each window — well above the default 5000 sample_size.
                return [SqlDriver.RowResult(cells={"n": 1_000_000})]
            # Sample query result — three rows is fine for the test;
            # we're asserting on SQL shape, not row count.
            return [SqlDriver.RowResult(cells={"embedding": [1.0, 0.0]}) for _ in range(3)]

    recorder = _Recorder()
    await monitor_embedding_drift(
        recorder,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    sample_calls = [
        (sql, params)
        for sql, params in recorder.calls
        if 'AND "embedding" IS NOT NULL' in sql and "SELECT count(*)" not in sql
    ]
    assert len(sample_calls) == 2, sample_calls
    for sql, params in sample_calls:
        assert "random()" in sql
        assert "LIMIT %s" in sql
        # No global sort — the random() filter is per-row independent.
        assert "RANDOM()" not in sql
        # Param order: window_start, window_end, p (float), sample_size (int).
        assert len(params) == 4
        assert isinstance(params[2], float) and 0.0 < params[2] <= 1.0
        assert isinstance(params[3], int) and params[3] == 5000
        # Probability targets ``sample_size * over_fetch / window_size`` =
        # ``5000 * 1.5 / 1_000_000`` = 0.0075.
        assert params[2] == pytest.approx(0.0075, rel=1e-9)


async def test_monitor_embedding_drift_caps_probability_at_one() -> None:
    """When ``sample_size · over_fetch`` would exceed the window size
    (e.g. window has only slightly more rows than the sample target),
    the probability MUST clamp to 1.0 so PG doesn't try to evaluate
    ``random() < 1.5``. The bare LIMIT then handles the over-select."""

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[object]]] = []

        async def execute_query(
            self,
            sql: str,
            params: list[object] | None = None,
            force_readonly: bool = False,
        ) -> list[object]:
            from mcpg._vendor.sql import SqlDriver

            self.calls.append((sql, list(params or [])))
            if "pg_extension" in sql:
                return [SqlDriver.RowResult(cells={"present": 1})]
            if "FROM pg_attribute a" in sql:
                return [SqlDriver.RowResult(cells={"type_name": "vector", "type_mod": 2})]
            if "SELECT count(*)" in sql:
                # 5001 = just-above-sample_size triggers the large-window
                # path (window_size > sample_size), but p would be
                # 5000*1.5/5001 ≈ 1.4996 → must clamp to 1.0.
                return [SqlDriver.RowResult(cells={"n": 5001})]
            return [SqlDriver.RowResult(cells={"embedding": [1.0, 0.0]}) for _ in range(3)]

    recorder = _Recorder()
    await monitor_embedding_drift(
        recorder,  # type: ignore[arg-type]
        "app",
        "docs",
        "embedding",
        "created_at",
        baseline_start="2026-01-01",
        baseline_end="2026-02-01",
        current_start="2026-02-01",
        current_end="2026-03-01",
    )

    sample_calls = [
        params
        for sql, params in recorder.calls
        if 'AND "embedding" IS NOT NULL' in sql and "SELECT count(*)" not in sql
    ]
    for params in sample_calls:
        assert params[2] == 1.0  # p clamped


# --- retrieve_with_context ------------------------------------------------


_DIM_LOOKUP = "FROM pg_attribute a"
_FK_LOOKUP = "c.contype = 'f'"
_KNN = "mcpg_distance"


def _fk_row(
    *,
    name: str,
    from_table: str,
    from_columns: list[str],
    to_schema: str,
    to_table: str,
    to_columns: list[str],
) -> dict[str, object]:
    return {
        "name": name,
        "from_table": from_table,
        "from_columns": from_columns,
        "to_schema": to_schema,
        "to_table": to_table,
        "to_columns": to_columns,
    }


def _ctx_routes(
    *,
    dim: int | None = 3,
    fks: list[dict[str, object]] | None = None,
    knn_rows: list[dict[str, object]] | None = None,
    parent_rows: list[dict[str, object]] | None = None,
    child_rows: list[dict[str, object]] | None = None,
) -> dict[tuple[str, tuple[object, ...] | None], list[dict[str, object]]]:
    dim_row: list[dict[str, object]] = [{"type_name": "vector", "type_mod": dim}] if dim is not None else []
    return {
        ("pg_extension", None): [{"present": 1}],
        (_DIM_LOOKUP, None): dim_row,
        (_FK_LOOKUP, None): fks or [],
        (_KNN, None): knn_rows or [],
        ('"authors"', None): parent_rows or [],
        ('"comments"', None): child_rows or [],
    }


async def test_retrieve_with_context_unavailable_without_pgvector() -> None:
    driver = FakeParamRoutingDriver({("pg_extension", None): []})
    result = await retrieve_with_context(
        driver,  # type: ignore[arg-type]
        schema="public",
        table="posts",
        embedding_column="embedding",
        query_vector=[0.1, 0.2, 0.3],
    )
    assert result == RetrieveWithContextResult(available=False, dimension=0, hits=[], detail=result.detail)
    assert result.available is False


async def test_retrieve_with_context_dimension_mismatch_raises() -> None:
    driver = FakeParamRoutingDriver(_ctx_routes(dim=4))
    with pytest.raises(VectorOpsError, match="query_vector has 3 dimensions but"):
        await retrieve_with_context(
            driver,  # type: ignore[arg-type]
            schema="public",
            table="posts",
            embedding_column="embedding",
            query_vector=[0.1, 0.2, 0.3],
        )


async def test_retrieve_with_context_not_a_vector_column_raises() -> None:
    driver = FakeParamRoutingDriver(_ctx_routes(dim=None))
    with pytest.raises(VectorOpsError, match="not a pgvector vector"):
        await retrieve_with_context(
            driver,  # type: ignore[arg-type]
            schema="public",
            table="posts",
            embedding_column="embedding",
            query_vector=[0.1, 0.2, 0.3],
        )


async def test_retrieve_with_context_packs_parents_and_children() -> None:
    fks = [
        _fk_row(
            name="posts_author_fk",
            from_table="posts",
            from_columns=["author_id"],
            to_schema="public",
            to_table="authors",
            to_columns=["id"],
        ),
        _fk_row(
            name="comments_post_fk",
            from_table="comments",
            from_columns=["post_id"],
            to_schema="public",
            to_table="posts",
            to_columns=["id"],
        ),
    ]
    knn = [
        {"id": 1, "author_id": 7, "title": "alpha", "embedding": "[0.1,0.2,0.3]", "mcpg_distance": 0.0},
    ]
    driver = FakeParamRoutingDriver(
        _ctx_routes(
            fks=fks,
            knn_rows=knn,
            parent_rows=[{"id": 7, "name": "Ada", "embedding": "[9,9,9]"}],
            child_rows=[
                {"id": 100, "post_id": 1, "body": "nice", "embedding": "[1,1,1]"},
                {"id": 101, "post_id": 1, "body": "great"},
            ],
        )
    )

    result = await retrieve_with_context(
        driver,  # type: ignore[arg-type]
        schema="public",
        table="posts",
        embedding_column="embedding",
        query_vector=[0.1, 0.2, 0.3],
        k=1,
    )

    assert result.available is True
    assert result.dimension == 3
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert isinstance(hit, ContextHit)
    assert hit.distance == 0.0
    # Embedding column stripped, mcpg_distance stripped.
    assert hit.row == {"id": 1, "author_id": 7, "title": "alpha"}
    assert "embedding" not in hit.row
    # The searched embedding column is stripped from related rows too,
    # not just the hit row (matters for self-referential FK chunk tables).
    assert hit.related == [
        RelatedRecords(
            fk_name="posts_author_fk",
            direction="parent",
            related_schema="public",
            related_table="authors",
            rows=[{"id": 7, "name": "Ada"}],
        ),
        RelatedRecords(
            fk_name="comments_post_fk",
            direction="child",
            related_schema="public",
            related_table="comments",
            rows=[{"id": 100, "post_id": 1, "body": "nice"}, {"id": 101, "post_id": 1, "body": "great"}],
        ),
    ]
    assert all("embedding" not in r for rec in hit.related for r in rec.rows)


async def test_retrieve_with_context_include_flags_suppress_expansion() -> None:
    fks = [
        _fk_row(
            name="posts_author_fk",
            from_table="posts",
            from_columns=["author_id"],
            to_schema="public",
            to_table="authors",
            to_columns=["id"],
        ),
        _fk_row(
            name="comments_post_fk",
            from_table="comments",
            from_columns=["post_id"],
            to_schema="public",
            to_table="posts",
            to_columns=["id"],
        ),
    ]
    knn = [{"id": 1, "author_id": 7, "embedding": "[0.1,0.2,0.3]", "mcpg_distance": 0.0}]
    driver = FakeParamRoutingDriver(
        _ctx_routes(fks=fks, knn_rows=knn, parent_rows=[{"id": 7}], child_rows=[{"id": 100, "post_id": 1}])
    )

    result = await retrieve_with_context(
        driver,  # type: ignore[arg-type]
        schema="public",
        table="posts",
        embedding_column="embedding",
        query_vector=[0.1, 0.2, 0.3],
        k=1,
        include_parents=False,
        include_children=False,
    )
    assert result.hits[0].related == []


async def test_retrieve_with_context_skips_parent_when_fk_value_null() -> None:
    fks = [
        _fk_row(
            name="posts_author_fk",
            from_table="posts",
            from_columns=["author_id"],
            to_schema="public",
            to_table="authors",
            to_columns=["id"],
        ),
    ]
    knn = [{"id": 1, "author_id": None, "embedding": "[0.1,0.2,0.3]", "mcpg_distance": 0.0}]
    driver = FakeParamRoutingDriver(_ctx_routes(fks=fks, knn_rows=knn, parent_rows=[{"id": 7}]))

    result = await retrieve_with_context(
        driver,  # type: ignore[arg-type]
        schema="public",
        table="posts",
        embedding_column="embedding",
        query_vector=[0.1, 0.2, 0.3],
        k=1,
    )
    # NULL FK value -> no parent lookup at all.
    assert result.hits[0].related == []


async def test_retrieve_with_context_child_limit_bound() -> None:
    fks = [
        _fk_row(
            name="comments_post_fk",
            from_table="comments",
            from_columns=["post_id"],
            to_schema="public",
            to_table="posts",
            to_columns=["id"],
        ),
    ]
    knn = [{"id": 1, "embedding": "[0.1,0.2,0.3]", "mcpg_distance": 0.0}]
    driver = FakeParamRoutingDriver(_ctx_routes(fks=fks, knn_rows=knn, child_rows=[{"id": 100, "post_id": 1}]))

    await retrieve_with_context(
        driver,  # type: ignore[arg-type]
        schema="public",
        table="posts",
        embedding_column="embedding",
        query_vector=[0.1, 0.2, 0.3],
        k=1,
        max_related=3,
    )
    child_call = next(call for call in driver.calls if 'FROM "public"."comments"' in call[0])
    # Bind order: to_value(s) then max_related LIMIT.
    assert child_call[1] == [1, 3]


async def test_retrieve_with_context_rejects_unsafe_identifier() -> None:
    driver = FakeParamRoutingDriver(_ctx_routes())
    with pytest.raises(VectorOpsError, match="invalid"):
        await retrieve_with_context(
            driver,  # type: ignore[arg-type]
            schema="public",
            table="posts; DROP",
            embedding_column="embedding",
            query_vector=[0.1, 0.2, 0.3],
        )


async def test_retrieve_with_context_tool_is_registered() -> None:
    from _fakes import FakeDriver

    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "retrieve_with_context" in listed
