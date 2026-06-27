"""Unit tests for advanced pgvector index-tuning diagnostics."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeParamRoutingDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.vector_tuner_advanced import (
    HnswRecallRecommendation,
    analyze_hnsw_recall,
    recommend_hnsw_ef_search,
)
from mcpg.vector_tuning import VectorTuningError

_SETTINGS = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


async def test_analyze_hnsw_recall_errors_when_vector_extension_absent() -> None:
    # extension_installed returns empty list -> not installed
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(VectorTuningError) as exc_info:
        await analyze_hnsw_recall(driver, "public", "items", "embedding", [0.1, 0.2])  # type: ignore[arg-type]

    assert "vector extension is not installed" in str(exc_info.value)


async def test_analyze_hnsw_recall_errors_on_invalid_metric() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(VectorTuningError) as exc_info:
        await analyze_hnsw_recall(driver, "public", "items", "embedding", [0.1, 0.2], metric="invalid")  # type: ignore[arg-type]

    assert "unknown metric" in str(exc_info.value)


async def test_analyze_hnsw_recall_errors_on_invalid_k() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(VectorTuningError) as exc_info:
        await analyze_hnsw_recall(driver, "public", "items", "embedding", [0.1, 0.2], k=0)  # type: ignore[arg-type]

    assert "k must be positive" in str(exc_info.value)


async def test_analyze_hnsw_recall_success_sweep_recall_curve() -> None:
    # 1. ext installed: present
    # 2. primary key detect: return attname 'uuid'
    # 3. ground truth query (enable_indexscan = off): returns ids [1, 2, 3, 4]
    # 4. approx HNSW sweep queries:
    #    - ef_search = 16: returns [1, 2] -> 50% recall
    #    - ef_search = 32: returns [1, 2, 3] -> 75% recall
    #    - ef_search = 64+: returns [1, 2, 3, 4] -> 100% recall
    routes = {
        ("pg_extension", None): [{"present": 1}],
        ("indisprimary = true", None): [{"pk_column": "uuid"}],
        ("enable_indexscan = off", None): [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
        ("ef_search = 16", None): [{"id": 1}, {"id": 2}],
        ("ef_search = 32", None): [{"id": 1}, {"id": 2}, {"id": 3}],
        ("ef_search = 64", None): [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
        ("ef_search = 128", None): [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
        ("ef_search = 256", None): [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
    }
    driver = FakeParamRoutingDriver(routes)

    curve = await analyze_hnsw_recall(
        driver,  # type: ignore[arg-type]
        "public",
        "items",
        "embedding",
        [0.1, 0.2],
        k=4,
    )

    assert len(curve) == 5
    assert curve[0] == {"ef_search": 16, "recall": 0.5, "latency_ms": pytest.approx(curve[0]["latency_ms"])}
    assert curve[1] == {"ef_search": 32, "recall": 0.75, "latency_ms": pytest.approx(curve[1]["latency_ms"])}
    assert curve[2] == {"ef_search": 64, "recall": 1.0, "latency_ms": pytest.approx(curve[2]["latency_ms"])}
    assert curve[3] == {"ef_search": 128, "recall": 1.0, "latency_ms": pytest.approx(curve[3]["latency_ms"])}
    assert curve[4] == {"ef_search": 256, "recall": 1.0, "latency_ms": pytest.approx(curve[4]["latency_ms"])}


async def test_analyze_hnsw_recall_tool_is_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "analyze_hnsw_recall" in listed


async def test_analyze_hnsw_recall_string_vector_and_pk_fallback() -> None:
    # 1. ext installed: present
    # 2. primary key detect: return empty rows (no pk) -> fall back to 'id'
    # 3. ground truth query (enable_indexscan = off): returns ids [1, 2]
    # 4. approx HNSW sweep queries:
    #    - returns [1, 2] -> 100% recall
    routes = {
        ("pg_extension", None): [{"present": 1}],
        ("indisprimary = true", None): [],  # Empty -> PK column fallback to 'id'
        ("enable_indexscan = off", None): [{"id": 1}, {"id": 2}],
        ("ef_search = 16", None): [{"id": 1}, {"id": 2}],
        ("ef_search = 32", None): [{"id": 1}, {"id": 2}],
        ("ef_search = 64", None): [{"id": 1}, {"id": 2}],
        ("ef_search = 128", None): [{"id": 1}, {"id": 2}],
        ("ef_search = 256", None): [{"id": 1}, {"id": 2}],
    }
    driver = FakeParamRoutingDriver(routes)

    # Pass query_vector as a string instead of a list
    curve = await analyze_hnsw_recall(
        driver,  # type: ignore[arg-type]
        "public",
        "items",
        "embedding",
        "[0.1, 0.2]",
        k=2,
    )

    assert len(curve) == 5
    assert curve[0]["recall"] == 1.0


async def test_analyze_hnsw_recall_empty_table_returns_empty_curve() -> None:
    # 1. ext installed: present
    # 2. primary key detect: return attname 'id'
    # 3. ground truth query (enable_indexscan = off): returns no rows (empty table)
    routes = {
        ("pg_extension", None): [{"present": 1}],
        ("indisprimary = true", None): [{"pk_column": "id"}],
        ("enable_indexscan = off", None): [],
    }
    driver = FakeParamRoutingDriver(routes)

    curve = await analyze_hnsw_recall(
        driver,  # type: ignore[arg-type]
        "public",
        "items",
        "embedding",
        [0.1, 0.2],
        k=5,
    )

    assert curve == []


# ---------------------------------------------------------------------------
# recommend_hnsw_ef_search (roadmap 9.1 advisor)
# ---------------------------------------------------------------------------


def _recommend_routes(
    *,
    has_index: bool = True,
    samples: list[dict[str, object]] | None = None,
    truth: list[dict[str, object]] | None = None,
    ef_results: dict[int, list[dict[str, object]]] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Build FakeRoutingDriver routes for recommend_hnsw_ef_search.

    Substrings are mutually exclusive across the query types the tool
    issues (extension probe, PK detect, index detect, sample fetch,
    brute-force truth, per-ef approx)."""
    routes: dict[str, list[dict[str, object]]] = {
        "pg_extension": [{"present": 1}],
        "indisprimary = true": [{"pk_column": "id"}],
        "am.amname AS index_method": (
            [{"index_name": "docs_embedding_hnsw", "index_method": "hnsw", "index_def": "CREATE INDEX ..."}]
            if has_index
            else []
        ),
        "IS NOT NULL ORDER BY": samples if samples is not None else [{"id": 1, "vec": "[0.1,0.2]"}],
        "l2_distance(": truth if truth is not None else [{"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}],
    }
    for ef, rows in (ef_results or {}).items():
        routes[f"hnsw.ef_search = {ef}"] = rows
    return routes


async def test_recommend_unavailable_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})
    result = await recommend_hnsw_ef_search(driver, "public", "docs", "embedding")  # type: ignore[arg-type]
    assert isinstance(result, HnswRecallRecommendation)
    assert result.available is False
    assert result.recommended_ef_search is None


async def test_recommend_rejects_bad_metric() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorTuningError, match="unknown metric"):
        await recommend_hnsw_ef_search(driver, "public", "docs", "embedding", metric="nope")  # type: ignore[arg-type]


async def test_recommend_rejects_target_recall_out_of_range() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorTuningError, match="target_recall"):
        await recommend_hnsw_ef_search(driver, "public", "docs", "embedding", target_recall=1.5)  # type: ignore[arg-type]


async def test_recommend_rejects_too_many_samples() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(VectorTuningError, match="sample_queries"):
        await recommend_hnsw_ef_search(driver, "public", "docs", "embedding", sample_queries=999)  # type: ignore[arg-type]


async def test_recommend_reports_missing_hnsw_index() -> None:
    driver = FakeRoutingDriver(_recommend_routes(has_index=False))
    result = await recommend_hnsw_ef_search(driver, "public", "docs", "embedding")  # type: ignore[arg-type]
    assert result.available is True
    assert result.has_hnsw_index is False
    assert result.recommended_ef_search is None
    assert "No HNSW index" in result.detail


async def test_recommend_picks_smallest_ef_meeting_target() -> None:
    # truth = {2,3,4,5} (k=4). ef=16 → 2/4, ef=32 → 3/4, ef=64+ → 4/4.
    ef_results = {
        16: [{"id": 2}, {"id": 3}],
        32: [{"id": 2}, {"id": 3}, {"id": 4}],
        64: [{"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}],
        128: [{"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}],
        256: [{"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}],
    }
    driver = FakeRoutingDriver(_recommend_routes(ef_results=ef_results))
    result = await recommend_hnsw_ef_search(
        driver,  # type: ignore[arg-type]
        "public",
        "docs",
        "embedding",
        k=4,
        target_recall=0.95,
    )
    assert result.has_hnsw_index is True
    assert result.index_name == "docs_embedding_hnsw"
    assert len(result.sweep) == 5
    assert result.sweep[0].mean_recall_at_k == 0.5
    assert result.sweep[1].mean_recall_at_k == 0.75
    assert result.sweep[2].mean_recall_at_k == 1.0
    # 64 is the smallest clearing 0.95.
    assert result.recommended_ef_search == 64
    assert result.sweep[2].meets_target is True
    assert result.sweep[0].meets_target is False


async def test_recommend_none_when_target_unreachable() -> None:
    # Every ef returns only 1 of 4 → recall 0.25 everywhere.
    ef_results = {ef: [{"id": 2}] for ef in (16, 32, 64, 128, 256)}
    driver = FakeRoutingDriver(_recommend_routes(ef_results=ef_results))
    result = await recommend_hnsw_ef_search(
        driver,  # type: ignore[arg-type]
        "public",
        "docs",
        "embedding",
        k=4,
        target_recall=0.95,
    )
    assert result.recommended_ef_search is None
    assert "No swept ef_search reached" in result.detail
    assert all(p.meets_target is False for p in result.sweep)


async def test_recommend_empty_table_with_index() -> None:
    driver = FakeRoutingDriver(_recommend_routes(samples=[]))
    result = await recommend_hnsw_ef_search(driver, "public", "docs", "embedding")  # type: ignore[arg-type]
    assert result.has_hnsw_index is True
    assert result.sample_queries == 0
    assert result.recommended_ef_search is None
    assert "No non-null vectors" in result.detail


async def test_recommend_custom_ef_values_respected() -> None:
    ef_results = {
        50: [{"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}],
        100: [{"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}],
    }
    driver = FakeRoutingDriver(_recommend_routes(ef_results=ef_results))
    result = await recommend_hnsw_ef_search(
        driver,  # type: ignore[arg-type]
        "public",
        "docs",
        "embedding",
        k=4,
        target_recall=0.9,
        ef_values=(50, 100),
    )
    assert [p.ef_search for p in result.sweep] == [50, 100]
    assert result.recommended_ef_search == 50


async def test_recommend_tool_is_registered() -> None:
    server = create_server(_SETTINGS, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "recommend_hnsw_ef_search" in listed
