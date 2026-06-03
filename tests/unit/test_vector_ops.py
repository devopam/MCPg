"""Tests for mcpg.vector_ops (pgvector analytics + the analyze_distance_metric tool)."""

import math

import pytest
from _fakes import FakeDatabase, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.vector_ops import (
    DEFAULT_SAMPLE_SIZE,
    DistanceMetricRecommendation,
    VectorOpsError,
    _l2_norm,
    _parse_embedding,
    _pick_metric,
    analyze_distance_metric,
)

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


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
