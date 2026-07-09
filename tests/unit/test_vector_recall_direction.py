"""Regression guard: brute-force ground-truth ORDER BY direction per metric.

pgvector's ``<#>`` operator is the *negated* inner product (ASC = nearest),
but the ``inner_product()`` *function* returns the raw dot product (DESC =
nearest). The recall tools rank the ANN side with the operator and the
ground-truth side with the function, so for ``metric="inner_product"`` the
ground-truth ORDER BY must be DESC — otherwise truth ranks the k *farthest*
rows and recall collapses to ~0 while the index is fine. ``l2`` / ``cosine``
are distances (ASC = nearest) and must stay ASC.

These tests capture the SQL each tool emits and assert the ground-truth query's
direction, without a live database.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcpg.sql import SqlDriver
from mcpg.vector_tuning import _DISTANCE_FUNCTIONS, _TRUTH_ORDER, vector_recall_at_k


def test_truth_order_map() -> None:
    assert _TRUTH_ORDER == {"l2": "ASC", "cosine": "ASC", "inner_product": "DESC"}


class _CaptureDriver:
    """Records every SQL string; returns two id/vec rows for any query."""

    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def execute_query(self, sql: Any, params: Any = None, force_readonly: bool = False) -> list[Any]:
        self.sqls.append(str(sql))
        return [
            SqlDriver.RowResult(cells={"id": 1, "vec": "[1,0]"}),
            SqlDriver.RowResult(cells={"id": 2, "vec": "[0,1]"}),
        ]


def _truth_sql(sqls: list[str], metric: str) -> str:
    """The ground-truth query is the one using the FUNCTION form, not SET LOCAL."""
    fn = _DISTANCE_FUNCTIONS[metric] + "("
    hits = [q for q in sqls if fn in q and "ORDER BY" in q and "SET LOCAL" not in q]
    assert hits, f"no ground-truth query captured for {metric}"
    return hits[0]


@pytest.mark.parametrize("metric,want", [("inner_product", "DESC"), ("l2", "ASC"), ("cosine", "ASC")])
async def test_vector_recall_truth_direction(monkeypatch: pytest.MonkeyPatch, metric: str, want: str) -> None:
    async def _noop(_driver: Any) -> None:
        return None

    monkeypatch.setattr("mcpg.vector_tuning._ensure_installed", _noop)
    drv = _CaptureDriver()
    await vector_recall_at_k(drv, "public", "t", "emb", "id", k=2, sample_size=2, metric=metric)  # type: ignore[arg-type]
    assert f" {want} LIMIT" in _truth_sql(drv.sqls, metric)


@pytest.mark.parametrize("metric,want", [("inner_product", "DESC"), ("l2", "ASC")])
async def test_recommend_hnsw_ef_search_truth_direction(
    monkeypatch: pytest.MonkeyPatch, metric: str, want: str
) -> None:
    import mcpg.vector_tuner_advanced as adv

    async def _noop(_driver: Any) -> None:
        return None

    async def _pk(*_a: Any, **_k: Any) -> str:
        return "id"

    async def _idx(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [{"index_method": "hnsw", "index_name": "ix"}]

    monkeypatch.setattr(adv, "_ensure_installed", _noop)
    monkeypatch.setattr(adv, "_detect_primary_key", _pk)
    monkeypatch.setattr(adv, "_indexes_on_column", _idx)
    drv = _CaptureDriver()
    await adv.recommend_hnsw_ef_search(drv, "public", "t", "emb", k=1, sample_queries=2, metric=metric)  # type: ignore[arg-type]
    assert f" {want} LIMIT" in _truth_sql(drv.sqls, metric)


@pytest.mark.parametrize("metric,want", [("inner_product", "DESC"), ("l2", "ASC")])
async def test_recommend_ivfflat_probes_truth_direction(
    monkeypatch: pytest.MonkeyPatch, metric: str, want: str
) -> None:
    import mcpg.vector_tuner_advanced as adv

    async def _noop(_driver: Any) -> None:
        return None

    async def _pk(*_a: Any, **_k: Any) -> str:
        return "id"

    async def _idx(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [{"index_method": "ivfflat", "index_name": "ix"}]

    monkeypatch.setattr(adv, "_ensure_installed", _noop)
    monkeypatch.setattr(adv, "_detect_primary_key", _pk)
    monkeypatch.setattr(adv, "_indexes_on_column", _idx)
    drv = _CaptureDriver()
    await adv.recommend_ivfflat_probes(drv, "public", "t", "emb", k=1, sample_queries=2, metric=metric)  # type: ignore[arg-type]
    assert f" {want} LIMIT" in _truth_sql(drv.sqls, metric)
