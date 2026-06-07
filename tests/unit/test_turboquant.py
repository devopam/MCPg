"""Tests for the pg_turboquant read-only advisor surface."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.turboquant import (
    TurboQuantError,
    TurboQuantHeapStats,
    TurboQuantIndexInfo,
    TurboQuantLastScanStats,
    get_turboquant_heap_stats,
    get_turboquant_index_metadata,
    get_turboquant_last_scan_stats,
    list_turboquant_indexes,
)

_READ_ONLY = load_settings({"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db"})


_FULL_METADATA = {
    "algorithm_version": "v2",
    "quantizer_family": "turboquant",
    "residual_sketch_kind": "hadamard",
    "fast_path_eligible": True,
    "capability_flags": ["simd_avx512", "fast_path"],
    "delta_state": "clean",
    "maintenance_recommended": False,
}


# --- list_turboquant_indexes ------------------------------------------------


async def test_list_turboquant_indexes_returns_empty_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    assert await list_turboquant_indexes(driver) == []  # type: ignore[arg-type]


async def test_list_turboquant_indexes_maps_rows_when_extension_present() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'turboquant'": [
                {
                    "schema": "public",
                    "index": "embeddings_tq_idx",
                    "table": "embeddings",
                    "column": "embedding",
                    "metadata": _FULL_METADATA,
                }
            ],
        }
    )

    infos = await list_turboquant_indexes(driver)  # type: ignore[arg-type]

    assert infos == [
        TurboQuantIndexInfo(
            schema="public",
            index="embeddings_tq_idx",
            table="embeddings",
            column="embedding",
            algorithm_version="v2",
            quantizer_family="turboquant",
            residual_sketch_kind="hadamard",
            fast_path_eligible=True,
            capability_flags=["simd_avx512", "fast_path"],
            delta_state="clean",
            maintenance_recommended=False,
            raw_metadata=_FULL_METADATA,
        )
    ]


async def test_list_turboquant_indexes_tolerates_partial_metadata() -> None:
    # Upstream may add or remove keys; missing documented keys fall
    # through to None / [] rather than raising.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'turboquant'": [
                {
                    "schema": "public",
                    "index": "minimal_idx",
                    "table": "embeddings",
                    "column": "embedding",
                    "metadata": {"algorithm_version": "v1"},
                }
            ],
        }
    )

    infos = await list_turboquant_indexes(driver)  # type: ignore[arg-type]

    [info] = infos
    assert info.algorithm_version == "v1"
    assert info.quantizer_family is None
    assert info.capability_flags == []
    assert info.maintenance_recommended is None
    assert info.raw_metadata == {"algorithm_version": "v1"}


async def test_list_turboquant_indexes_decodes_json_text_payload() -> None:
    # Some drivers return JSONB columns as text — the helper must still
    # produce a usable parsed dict.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'turboquant'": [
                {
                    "schema": "public",
                    "index": "txt_idx",
                    "table": "embeddings",
                    "column": "embedding",
                    "metadata": '{"algorithm_version": "v2", "fast_path_eligible": true}',
                }
            ],
        }
    )

    [info] = await list_turboquant_indexes(driver)  # type: ignore[arg-type]
    assert info.algorithm_version == "v2"
    assert info.fast_path_eligible is True


# --- get_turboquant_index_metadata -----------------------------------------


async def test_get_turboquant_index_metadata_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(TurboQuantError, match="not installed"):
        await get_turboquant_index_metadata(driver, "public", "idx")  # type: ignore[arg-type]


async def test_get_turboquant_index_metadata_raises_when_no_match() -> None:
    # extension present but no row matches => upstream returned empty
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(TurboQuantError, match="no turboquant index"):
        await get_turboquant_index_metadata(driver, "public", "missing_idx")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("schema", "index"),
    [
        ("public; DROP", "idx"),
        ("public", "idx; DROP"),
        ("", "idx"),
        ("public", ""),
        ("public.embeddings", "idx"),
        ("public", "idx-name"),
    ],
)
async def test_get_turboquant_index_metadata_rejects_unsafe_identifiers(schema: str, index: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(TurboQuantError, match="invalid"):
        await get_turboquant_index_metadata(driver, schema, index)  # type: ignore[arg-type]


async def test_get_turboquant_index_metadata_returns_mapped_row() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'turboquant' AND n.nspname": [
                {
                    "schema": "public",
                    "index": "embeddings_tq_idx",
                    "table": "embeddings",
                    "column": "embedding",
                    "metadata": _FULL_METADATA,
                }
            ],
        }
    )

    info = await get_turboquant_index_metadata(driver, "public", "embeddings_tq_idx")  # type: ignore[arg-type]
    assert info.algorithm_version == "v2"
    assert info.maintenance_recommended is False


# --- get_turboquant_heap_stats ---------------------------------------------


async def test_get_turboquant_heap_stats_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(TurboQuantError, match="not installed"):
        await get_turboquant_heap_stats(driver, "public", "idx")  # type: ignore[arg-type]


async def test_get_turboquant_heap_stats_returns_row_count() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_index_heap_stats": [{"stats": {"row_count": 12345}}],
        }
    )

    stats = await get_turboquant_heap_stats(driver, "public", "embeddings_tq_idx")  # type: ignore[arg-type]
    assert stats == TurboQuantHeapStats(
        schema="public",
        index="embeddings_tq_idx",
        row_count=12345,
        raw={"row_count": 12345},
    )


async def test_get_turboquant_heap_stats_falls_back_to_alternate_key() -> None:
    # Older / forked builds may report 'rows' rather than 'row_count'.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_index_heap_stats": [{"stats": {"rows": 999}}],
        }
    )

    stats = await get_turboquant_heap_stats(driver, "public", "idx")  # type: ignore[arg-type]
    assert stats.row_count == 999


@pytest.mark.parametrize(
    ("schema", "index"),
    [("public", "idx; DROP"), ("../etc/passwd", "idx")],
)
async def test_get_turboquant_heap_stats_rejects_unsafe_identifiers(schema: str, index: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(TurboQuantError, match="invalid"):
        await get_turboquant_heap_stats(driver, schema, index)  # type: ignore[arg-type]


# --- get_turboquant_last_scan_stats ----------------------------------------


async def test_get_turboquant_last_scan_stats_returns_none_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})
    assert await get_turboquant_last_scan_stats(driver) is None  # type: ignore[arg-type]


async def test_get_turboquant_last_scan_stats_returns_none_when_no_scan_yet() -> None:
    # Upstream returns SQL NULL → our routing fake returns {} via _as_dict.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_last_scan_stats": [{"stats": None}],
        }
    )
    assert await get_turboquant_last_scan_stats(driver) is None  # type: ignore[arg-type]


async def test_get_turboquant_last_scan_stats_parses_full_payload() -> None:
    payload = {
        "score_mode": "fast_path",
        "simd_kernel": "avx512_int8",
        "pages_scanned": 1024,
        "pages_pruned": 768,
        "extra_field_upstream_might_add": "preserved-in-raw",
    }
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_last_scan_stats": [{"stats": payload}],
        }
    )

    stats = await get_turboquant_last_scan_stats(driver)  # type: ignore[arg-type]
    assert stats == TurboQuantLastScanStats(
        raw=payload,
        score_mode="fast_path",
        simd_kernel="avx512_int8",
        pages_scanned=1024,
        pages_pruned=768,
    )


# --- MCP layer wiring ------------------------------------------------------


async def test_turboquant_tools_register_in_read_only_mode() -> None:
    server = create_server(_READ_ONLY, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {
        "list_turboquant_indexes",
        "get_turboquant_index_metadata",
        "get_turboquant_heap_stats",
        "get_turboquant_last_scan_stats",
    } <= listed
