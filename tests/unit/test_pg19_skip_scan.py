"""Tests for the PG 19 skip-scan advisor."""

from __future__ import annotations

from _fakes import FakeDriver, FakeRoutingDriver

from mcpg.pg19_skip_scan import (
    SkipScanCandidate,
    SkipScanStatus,
    _absolute_ndv,
    get_skip_scan_status,
    recommend_skip_scan_indexes,
)


def _version_route(num: int, ver: str) -> dict[str, list[dict[str, object]]]:
    return {"current_setting('server_version_num')": [{"ver_num": num, "ver": ver}]}


# --- get_skip_scan_status -------------------------------------------------


async def test_status_available_on_pg19() -> None:
    driver = FakeRoutingDriver(_version_route(190001, "19beta1"))
    status = await get_skip_scan_status(driver)  # type: ignore[arg-type]
    assert isinstance(status, SkipScanStatus)
    assert status.available is True
    assert "skip-scan" in status.detail.lower()


async def test_status_unavailable_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    status = await get_skip_scan_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "single-column" in status.detail


async def test_status_never_raises_on_driver_failure() -> None:
    driver = FakeDriver(fail=True)
    status = await get_skip_scan_status(driver)  # type: ignore[arg-type]
    assert status.available is False
    assert "version probe failed" in status.detail


# --- _absolute_ndv normalisation -----------------------------------------


def test_absolute_ndv_positive_passes_through() -> None:
    assert _absolute_ndv(42, 1_000_000) == 42


def test_absolute_ndv_negative_converts_to_fraction() -> None:
    # -0.001 of 1M rows ≈ 1000 distinct values
    assert _absolute_ndv(-0.001, 1_000_000) == 1000


def test_absolute_ndv_zero_means_unknown() -> None:
    assert _absolute_ndv(0, 1_000_000) == 0


def test_absolute_ndv_none_means_unknown() -> None:
    assert _absolute_ndv(None, 1_000_000) == 0


def test_absolute_ndv_negative_without_reltuples_is_unknown() -> None:
    """We refuse to guess when the fraction is given but the table size isn't."""
    assert _absolute_ndv(-0.5, None) == 0


def test_absolute_ndv_negative_with_zero_reltuples_is_unknown() -> None:
    assert _absolute_ndv(-0.5, 0) == 0


def test_absolute_ndv_negative_floors_to_one() -> None:
    """A unique column (-1) of a 1-row table still has at least 1 distinct value."""
    assert _absolute_ndv(-1, 1) == 1


# --- recommend_skip_scan_indexes ------------------------------------------


async def test_recommend_returns_empty_on_pg18() -> None:
    driver = FakeRoutingDriver(_version_route(180003, "18.3"))
    result = await recommend_skip_scan_indexes(driver)  # type: ignore[arg-type]
    assert result == []


async def test_recommend_returns_empty_on_driver_failure() -> None:
    driver = FakeDriver(fail=True)
    result = await recommend_skip_scan_indexes(driver)  # type: ignore[arg-type]
    assert result == []


async def test_recommend_returns_empty_when_catalog_walk_fails() -> None:
    """Version probe succeeds (PG 19) but the catalog walk raises — return empty."""

    class _CatalogFailingDriver:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute_query(self, query, params=None, force_readonly=False):  # type: ignore[no-untyped-def]
            from mcpg._vendor.sql import SqlDriver

            self.calls.append(query)
            if "current_setting" in query:
                return [SqlDriver.RowResult(cells={"ver_num": 190001, "ver": "19beta1"})]
            raise RuntimeError("catalog walk failed")

    driver = _CatalogFailingDriver()
    result = await recommend_skip_scan_indexes(driver)  # type: ignore[arg-type]
    assert result == []


async def test_recommend_flags_low_ndv_leading_column() -> None:
    routes = _version_route(190001, "19beta1")
    routes["WITH idx AS"] = [
        {
            "schema": "public",
            "table_name": "orders",
            "index_name": "ix_orders_status_created",
            "cols": ["status", "created_at"],
            "n_distinct": 4,
            "reltuples": 1_000_000,
        }
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_skip_scan_indexes(driver)  # type: ignore[arg-type]
    assert len(result) == 1
    cand = result[0]
    assert isinstance(cand, SkipScanCandidate)
    assert cand.leading_column == "status"
    assert cand.trailing_columns == ("created_at",)
    assert cand.estimated_leading_ndv == 4
    assert "skip-scan" in cand.rationale


async def test_recommend_skips_high_ndv_leading_column() -> None:
    """Composite (id, created_at) where id is unique — skip-scan unhelpful."""
    routes = _version_route(190001, "19beta1")
    routes["WITH idx AS"] = [
        {
            "schema": "public",
            "table_name": "orders",
            "index_name": "ix_orders_id_created",
            "cols": ["id", "created_at"],
            # -1 → unique → 1_000_000 distinct values, way above the 1000 cap
            "n_distinct": -1,
            "reltuples": 1_000_000,
        }
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_skip_scan_indexes(driver)  # type: ignore[arg-type]
    assert result == []


async def test_recommend_skips_unknown_ndv() -> None:
    """No ANALYZE stat → don't guess that the leading column is low-cardinality."""
    routes = _version_route(190001, "19beta1")
    routes["WITH idx AS"] = [
        {
            "schema": "public",
            "table_name": "orders",
            "index_name": "ix_orders_x_y",
            "cols": ["x", "y"],
            "n_distinct": None,
            "reltuples": 1_000_000,
        }
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_skip_scan_indexes(driver)  # type: ignore[arg-type]
    assert result == []


async def test_recommend_respects_max_leading_ndv_kwarg() -> None:
    routes = _version_route(190001, "19beta1")
    routes["WITH idx AS"] = [
        {
            "schema": "public",
            "table_name": "orders",
            "index_name": "ix",
            "cols": ["a", "b"],
            "n_distinct": 500,
            "reltuples": 1_000_000,
        }
    ]
    driver = FakeRoutingDriver(routes)
    # Default cap 1000 → flagged.
    assert len(await recommend_skip_scan_indexes(driver)) == 1  # type: ignore[arg-type]
    # Tighten cap to 100 → no longer flagged.
    assert await recommend_skip_scan_indexes(driver, max_leading_ndv=100) == []  # type: ignore[arg-type]


async def test_recommend_skips_single_column_indexes_from_catalog() -> None:
    """The catalog walk's WHERE clause filters out natts=1, but defensively
    a malformed `cols` array with one element must also be ignored."""
    routes = _version_route(190001, "19beta1")
    routes["WITH idx AS"] = [
        {
            "schema": "public",
            "table_name": "orders",
            "index_name": "ix",
            "cols": ["only_one"],  # shouldn't happen, but be defensive
            "n_distinct": 4,
            "reltuples": 1_000_000,
        }
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_skip_scan_indexes(driver)  # type: ignore[arg-type]
    assert result == []


async def test_recommend_emits_multiple_candidates_in_order() -> None:
    routes = _version_route(190001, "19beta1")
    routes["WITH idx AS"] = [
        {
            "schema": "public",
            "table_name": "orders",
            "index_name": "ix_a",
            "cols": ["status", "created_at"],
            "n_distinct": 4,
            "reltuples": 1_000_000,
        },
        {
            "schema": "public",
            "table_name": "orders",
            "index_name": "ix_b",
            "cols": ["region", "user_id"],
            "n_distinct": 12,
            "reltuples": 5_000_000,
        },
    ]
    driver = FakeRoutingDriver(routes)
    result = await recommend_skip_scan_indexes(driver)  # type: ignore[arg-type]
    assert [c.index_name for c in result] == ["ix_a", "ix_b"]
    assert [c.estimated_leading_ndv for c in result] == [4, 12]


# --- Dataclass shapes -----------------------------------------------------


def test_dataclass_shapes() -> None:
    status = SkipScanStatus(available=True, server_version_num=190001, server_version="19beta1", detail="ok")
    assert status.available is True
    cand = SkipScanCandidate(
        schema="public",
        table="t",
        index_name="ix",
        leading_column="status",
        trailing_columns=("created_at",),
        estimated_leading_ndv=4,
        rationale="ok",
    )
    assert cand.trailing_columns == ("created_at",)
