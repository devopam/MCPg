"""Unit tests for advanced pgvector index-tuning diagnostics."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeParamRoutingDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.vector_tuner_advanced import analyze_hnsw_recall
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
