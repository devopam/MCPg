"""Tests for the pg_turboquant read-only advisor surface."""

import pytest
from _fakes import FakeDatabase, FakeDriver, FakeParamRoutingDriver, FakeRoutingDriver
from mcp.shared.memory import create_connected_server_and_client_session

from mcpg.config import load_settings
from mcpg.server import create_server
from mcpg.turboquant import (
    TurboQuantError,
    TurboQuantHeapStats,
    TurboQuantIndexInfo,
    TurboQuantLastScanStats,
    audit_turboquant_indexes,
    get_turboquant_heap_stats,
    get_turboquant_index_metadata,
    get_turboquant_last_scan_stats,
    list_turboquant_indexes,
    recommend_turboquant_maintenance,
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
                    "reloptions": ["bits=4", "lists=100", "transform=hadamard", "normalized=true"],
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
            index_options={
                "bits": 4,
                "lists": 100,
                "transform": "hadamard",
                "normalized": True,
            },
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


async def test_list_turboquant_indexes_handles_empty_reloptions() -> None:
    # An index created without explicit options has reloptions = NULL —
    # the fake driver represents that as None in the cell. The parser
    # should produce {} rather than raise.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'turboquant'": [
                {
                    "schema": "public",
                    "index": "default_idx",
                    "table": "embeddings",
                    "column": "embedding",
                    "reloptions": None,
                    "metadata": _FULL_METADATA,
                }
            ],
        }
    )

    [info] = await list_turboquant_indexes(driver)  # type: ignore[arg-type]
    assert info.index_options == {}


async def test_list_turboquant_indexes_skips_malformed_reloptions_entries() -> None:
    # Future upstream additions or accidentally-malformed entries
    # should be silently skipped so catalog reads never break on a
    # surprise option name.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "WHERE am.amname = 'turboquant'": [
                {
                    "schema": "public",
                    "index": "weird_idx",
                    "table": "embeddings",
                    "column": "embedding",
                    "reloptions": [
                        "bits=8",
                        "no_equals_sign_at_all",  # malformed → skipped
                        "=missing_key",  # empty key → skipped
                        "future_option=banana",  # unknown key → preserved as str
                        "lists=-1",  # negative ints parse
                    ],
                    "metadata": _FULL_METADATA,
                }
            ],
        }
    )

    [info] = await list_turboquant_indexes(driver)  # type: ignore[arg-type]
    assert info.index_options == {
        "bits": 8,
        "future_option": "banana",
        "lists": -1,
    }


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
                    "reloptions": ["bits=4", "lists=100", "transform=hadamard", "normalized=true"],
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


# --- recommend_turboquant_maintenance --------------------------------------


_TQ_PRESENT: tuple[str, tuple[str, ...]] = ("pg_extension", ("pg_turboquant",))
_VECTOR_PRESENT: tuple[str, tuple[str, ...]] = ("pg_extension", ("vector",))
_LIST_INDEXES = ("WHERE am.amname = 'turboquant'", None)


def _index_row(metadata: dict, *, index: str = "embeddings_tq_idx") -> dict:
    return {
        "schema": "public",
        "index": index,
        "table": "embeddings",
        "column": "embedding",
        "reloptions": ["bits=4", "lists=100"],
        "metadata": metadata,
    }


async def test_recommend_turboquant_maintenance_returns_empty_when_extension_absent() -> None:
    driver = FakeParamRoutingDriver({_TQ_PRESENT: []})

    assert await recommend_turboquant_maintenance(driver) == []  # type: ignore[arg-type]


async def test_recommend_turboquant_maintenance_fires_prerequisites_unmet_when_vector_missing() -> None:
    # pg_turboquant installed, pgvector not — cluster-level critical
    # finding, no per-index walking.
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [],
        }
    )

    findings = await recommend_turboquant_maintenance(driver)  # type: ignore[arg-type]
    assert len(findings) == 1
    [finding] = findings
    assert finding.code == "prerequisites_unmet"
    assert finding.severity == "CRITICAL"
    assert finding.schema == ""
    assert finding.index == ""
    assert "vector" in finding.suggested_action.lower()


async def test_recommend_turboquant_maintenance_fires_format_v1_rule() -> None:
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [_index_row({"algorithm_version": "v1.3"})],
        }
    )

    [finding] = await recommend_turboquant_maintenance(driver)  # type: ignore[arg-type]
    assert finding.code == "format_v1_reindex_needed"
    assert finding.severity == "CRITICAL"
    assert finding.index == "embeddings_tq_idx"
    assert finding.suggested_action.startswith("REINDEX INDEX CONCURRENTLY")


async def test_recommend_turboquant_maintenance_fires_maintenance_due_rule() -> None:
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [
                _index_row(
                    {
                        "algorithm_version": "v2",
                        "maintenance_recommended": True,
                        "delta_state": "compaction_pending",
                    }
                )
            ],
        }
    )

    [finding] = await recommend_turboquant_maintenance(driver)  # type: ignore[arg-type]
    assert finding.code == "maintenance_due"
    assert finding.severity == "WARNING"
    assert "tq_maintain_index" in finding.suggested_action


async def test_recommend_turboquant_maintenance_fires_fast_path_ineligible_rule() -> None:
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [_index_row({"algorithm_version": "v2", "fast_path_eligible": False})],
        }
    )

    [finding] = await recommend_turboquant_maintenance(driver)  # type: ignore[arg-type]
    assert finding.code == "fast_path_ineligible"
    assert finding.severity == "WARNING"


async def test_recommend_turboquant_maintenance_silent_when_fast_path_unreported() -> None:
    # ``None`` (key missing or null) is distinct from explicit ``False`` —
    # don't fire on the absence of information.
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [_index_row({"algorithm_version": "v2"})],
        }
    )

    assert await recommend_turboquant_maintenance(driver) == []  # type: ignore[arg-type]


async def test_recommend_turboquant_maintenance_quotes_identifiers_in_suggested_sql() -> None:
    # PG allows mixed-case names, embedded quotes, and embedded
    # apostrophes via delimited identifiers — catalog rows can carry
    # any of these. The suggested SQL must survive PG parsing in all
    # three cases.
    nasty_row = {
        "schema": 'My"Schema',  # embedded double quote → double it inside ident
        "index": "Mixed-Case Index",  # mixed case + space → must stay quoted
        "table": "embeddings",
        "column": "embedding",
        "reloptions": None,
        "metadata": {
            "algorithm_version": "v1.0",
            "maintenance_recommended": True,
        },
    }
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [nasty_row],
        }
    )

    findings = await recommend_turboquant_maintenance(driver)  # type: ignore[arg-type]
    by_code = {f.code: f for f in findings}

    # REINDEX is identifier-quoted: schema's " is doubled, both names
    # are kept quoted (so case + space survive).
    assert (
        by_code["format_v1_reindex_needed"].suggested_action
        == 'REINDEX INDEX CONCURRENTLY "My""Schema"."Mixed-Case Index";'
    )
    # tq_maintain_index takes a regclass string — needs both layers
    # (identifier-quote first, literal-quote second).
    assert by_code["maintenance_due"].suggested_action == (
        'SELECT tq_maintain_index(\'"My""Schema"."Mixed-Case Index"\'::regclass);'
    )


async def test_recommend_turboquant_maintenance_quotes_apostrophe_in_regclass_literal() -> None:
    # If an identifier contains a single quote, the regclass literal's
    # outer ' would close prematurely without escaping.
    row = {
        "schema": "O'Reilly",
        "index": "idx",
        "table": "embeddings",
        "column": "embedding",
        "reloptions": None,
        "metadata": {"algorithm_version": "v2", "maintenance_recommended": True},
    }
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [row],
        }
    )

    [finding] = await recommend_turboquant_maintenance(driver)  # type: ignore[arg-type]
    assert finding.suggested_action == "SELECT tq_maintain_index('\"O''Reilly\".\"idx\"'::regclass);"


async def test_recommend_turboquant_maintenance_combines_findings_across_indexes() -> None:
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [
                _index_row({"algorithm_version": "v1.4"}, index="old_idx"),
                _index_row(
                    {"algorithm_version": "v2", "maintenance_recommended": True},
                    index="needs_compaction_idx",
                ),
                _index_row({"algorithm_version": "v2", "fast_path_eligible": False}, index="slow_idx"),
            ],
        }
    )

    findings = await recommend_turboquant_maintenance(driver)  # type: ignore[arg-type]
    codes = sorted((f.code, f.index) for f in findings)
    assert codes == [
        ("fast_path_ineligible", "slow_idx"),
        ("format_v1_reindex_needed", "old_idx"),
        ("maintenance_due", "needs_compaction_idx"),
    ]


# --- audit_turboquant_indexes (scorecard adapter) --------------------------


async def test_audit_turboquant_indexes_returns_none_when_extension_absent() -> None:
    driver = FakeParamRoutingDriver({_TQ_PRESENT: []})

    assert await audit_turboquant_indexes(driver) is None  # type: ignore[arg-type]


async def test_audit_turboquant_indexes_emits_good_when_no_findings() -> None:
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [],  # no turboquant indexes — clean baseline
        }
    )

    category = await audit_turboquant_indexes(driver)  # type: ignore[arg-type]
    assert category is not None
    assert category.category == "pg_turboquant Indexes"
    assert category.status == "GOOD"
    assert category.score == 100
    assert [m.status for m in category.metrics] == ["GOOD"]


async def test_audit_turboquant_indexes_scores_drop_with_findings() -> None:
    # One CRITICAL (-30) + one WARNING (-15) = score 55 → CRITICAL band.
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [
                _index_row({"algorithm_version": "v1.0"}, index="old_idx"),
                _index_row(
                    {"algorithm_version": "v2", "maintenance_recommended": True},
                    index="warn_idx",
                ),
            ],
        }
    )

    category = await audit_turboquant_indexes(driver)  # type: ignore[arg-type]
    assert category is not None
    assert category.score == 55
    assert category.status == "CRITICAL"
    severities = sorted(m.status for m in category.metrics)
    assert severities == ["CRITICAL", "WARNING"]


async def test_audit_turboquant_indexes_score_clamped_at_zero() -> None:
    # Four CRITICALs would deduct 120; score must not go negative.
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [_index_row({"algorithm_version": "v1.0"}, index=f"old_{i}") for i in range(4)],
        }
    )

    category = await audit_turboquant_indexes(driver)  # type: ignore[arg-type]
    assert category is not None
    assert category.score == 0
    assert category.status == "CRITICAL"


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
        "recommend_turboquant_maintenance",
    } <= listed
