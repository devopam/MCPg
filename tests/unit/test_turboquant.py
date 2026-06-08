"""Tests for the pg_turboquant read-only advisor surface."""

from typing import Any

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
    create_turboquant_index,
    get_turboquant_heap_stats,
    get_turboquant_index_metadata,
    get_turboquant_last_scan_stats,
    list_turboquant_indexes,
    maintain_turboquant_index,
    recommend_turboquant_maintenance,
    recommend_turboquant_query_knobs,
    reindex_turboquant_index,
    turboquant_approx_candidates,
    turboquant_rerank_candidates,
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


async def test_recommend_turboquant_maintenance_fires_delta_tier_large_rule() -> None:
    # delta_health.merge_recommended=True → upstream's own advisory
    # → fire the WARNING with a tq_maintain_index suggested-action.
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [
                _index_row(
                    {
                        "algorithm_version": "v2",
                        "delta_live_count": 50_000,
                        "delta_batch_page_count": 800,
                        "delta_health": {
                            "merge_recommended": True,
                            "page_depth": 12,
                            "live_fraction": 0.4,
                        },
                    }
                )
            ],
        }
    )

    [finding] = await recommend_turboquant_maintenance(driver)  # type: ignore[arg-type]
    assert finding.code == "delta_tier_large"
    assert finding.severity == "WARNING"
    assert "delta_live_count=50000" in finding.evidence
    assert "delta_batch_page_count=800" in finding.evidence
    assert "tq_maintain_index" in finding.suggested_action


async def test_recommend_turboquant_maintenance_silent_when_delta_health_unreported() -> None:
    # Absent delta_health → no fire (don't fire on absence of info,
    # same convention as fast_path_ineligible).
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [_index_row({"algorithm_version": "v2"})],
        }
    )

    assert await recommend_turboquant_maintenance(driver) == []  # type: ignore[arg-type]


async def test_recommend_turboquant_maintenance_silent_when_merge_not_recommended() -> None:
    # merge_recommended=False → don't fire (the index is fine).
    driver = FakeParamRoutingDriver(
        {
            _TQ_PRESENT: [{"present": 1}],
            _VECTOR_PRESENT: [{"present": 1}],
            _LIST_INDEXES: [
                _index_row(
                    {
                        "algorithm_version": "v2",
                        "delta_live_count": 100,
                        "delta_health": {"merge_recommended": False},
                    }
                )
            ],
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


# --- maintain_turboquant_index ---------------------------------------------


_PREFLIGHT_SUBSTR = "WHERE am.amname = 'turboquant' AND n.nspname"


async def test_maintain_turboquant_index_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})

    with pytest.raises(TurboQuantError, match="not installed"):
        await maintain_turboquant_index(driver, "public", "idx")  # type: ignore[arg-type]


async def test_maintain_turboquant_index_raises_when_index_is_not_turboquant() -> None:
    # Extension present, preflight returns no rows (the index is real
    # but not a turboquant index, or doesn't exist at all). Refuse
    # rather than let upstream's error message leak catalog info.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            _PREFLIGHT_SUBSTR: [],
        }
    )

    with pytest.raises(TurboQuantError, match="not a turboquant index"):
        await maintain_turboquant_index(driver, "public", "some_other_idx")  # type: ignore[arg-type]


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
async def test_maintain_turboquant_index_rejects_unsafe_identifiers(schema: str, index: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})

    with pytest.raises(TurboQuantError, match="invalid"):
        await maintain_turboquant_index(driver, schema, index)  # type: ignore[arg-type]


async def test_maintain_turboquant_index_happy_path_returns_timings() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            _PREFLIGHT_SUBSTR: [{"present": 1}],
            "tq_maintain_index": [{"result": {}}],
        }
    )

    result = await maintain_turboquant_index(driver, "public", "embeddings_tq_idx")  # type: ignore[arg-type]
    assert result.schema == "public"
    assert result.index == "embeddings_tq_idx"
    assert result.started_at.endswith("Z")
    assert result.completed_at.endswith("Z")
    assert result.duration_seconds >= 0.0
    # Empty / missing JSON payload → all parsed fields None, raw == {}
    assert result.delta_merge_performed is None
    assert result.merged_delta_count is None
    assert result.recycled_delta_page_count is None
    assert result.raw == {}


async def test_maintain_turboquant_index_surfaces_upstream_return_json() -> None:
    # tq_maintain_index returns a documented JSON shape — verified
    # against src/tq_maintenance.h in the upstream investigation.
    # delta_merge_performed=true means upstream actually did work;
    # the other two counters quantify it.
    payload = {
        "delta_merge_performed": True,
        "merged_delta_count": 1234,
        "recycled_delta_page_count": 56,
        "future_field_upstream_might_add": "preserved-in-raw",
    }
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            _PREFLIGHT_SUBSTR: [{"present": 1}],
            "tq_maintain_index": [{"result": payload}],
        }
    )

    result = await maintain_turboquant_index(driver, "public", "embeddings_tq_idx")  # type: ignore[arg-type]
    assert result.delta_merge_performed is True
    assert result.merged_delta_count == 1234
    assert result.recycled_delta_page_count == 56
    assert result.raw == payload


async def test_maintain_turboquant_index_runs_maintenance_under_write_capability() -> None:
    # The third query call should be the actual maintain call and must
    # NOT have force_readonly set — a write capability check from the
    # SqlDriver layer relies on that flag.
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            _PREFLIGHT_SUBSTR: [{"present": 1}],
            "tq_maintain_index": [{"result": {}}],
        }
    )

    await maintain_turboquant_index(driver, "public", "embeddings_tq_idx")  # type: ignore[arg-type]

    maintain_calls = [c for c in driver.calls if "tq_maintain_index" in c[0]]
    assert len(maintain_calls) == 1
    _query, params, force_readonly = maintain_calls[0]
    assert params == ["public", "embeddings_tq_idx"]
    assert force_readonly is False


# --- create_turboquant_index (DDL) -----------------------------------------


def _ddl_db_with_extension_installed() -> FakeDatabase:
    """A FakeDatabase whose driver reports pg_turboquant as installed."""
    return FakeDatabase(FakeRoutingDriver({"pg_extension": [{"present": 1}]}))  # type: ignore[arg-type]


async def test_create_turboquant_index_raises_when_extension_absent() -> None:
    db = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))  # type: ignore[arg-type]
    with pytest.raises(TurboQuantError, match="not installed"):
        await create_turboquant_index(db, "public", "embeddings", "embedding", "idx", "cosine")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwarg", "value", "match"),
    [
        ("metric", "manhattan", "unsupported metric"),
        ("bits", 0, "bits must be an int"),
        ("bits", 65, "bits must be an int"),
        ("bits", True, "bits must be an int"),  # bool is rejected even though it's a subclass of int
        ("lists", -1, "lists must be an int"),
        ("lists", 2_000_000, "lists must be an int"),
        ("transform", "none", "unsupported transform"),
        ("transform", "fft", "unsupported transform"),
        ("normalized", "yes", "normalized must be a bool"),
        ("concurrently", "yes", "concurrently must be a bool"),
        ("concurrently", 1, "concurrently must be a bool"),
    ],
)
async def test_create_turboquant_index_validates_options(kwarg: str, value: Any, match: str) -> None:
    db = _ddl_db_with_extension_installed()
    kwargs: dict[str, Any] = {
        "schema": "public",
        "table": "embeddings",
        "column": "embedding",
        "index_name": "idx",
        "metric": "cosine",
    }
    if kwarg == "metric":
        kwargs["metric"] = value
    else:
        kwargs[kwarg] = value
    with pytest.raises(TurboQuantError, match=match):
        await create_turboquant_index(db, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ["schema", "table", "column", "index_name"],
)
async def test_create_turboquant_index_rejects_unsafe_identifiers(field: str) -> None:
    db = _ddl_db_with_extension_installed()
    kwargs = {
        "schema": "public",
        "table": "embeddings",
        "column": "embedding",
        "index_name": "idx",
        "metric": "cosine",
    }
    kwargs[field] = "bad; DROP TABLE x"
    with pytest.raises(TurboQuantError, match="invalid"):
        await create_turboquant_index(db, **kwargs)  # type: ignore[arg-type]


async def test_create_turboquant_index_renders_minimal_sql_when_no_options() -> None:
    db = _ddl_db_with_extension_installed()
    result = await create_turboquant_index(
        db,  # type: ignore[arg-type]
        "public",
        "embeddings",
        "embedding",
        "embeddings_tq_idx",
        "cosine",
    )
    expected = (
        'CREATE INDEX CONCURRENTLY "embeddings_tq_idx" '
        'ON "public"."embeddings" '
        'USING turboquant ("embedding" tq_cosine_ops)'
    )
    assert result.create_sql == expected
    assert db.unmanaged == [expected]
    assert result.options == {}
    assert result.concurrently is True
    assert result.duration_seconds >= 0.0


async def test_create_turboquant_index_renders_with_options() -> None:
    db = _ddl_db_with_extension_installed()
    result = await create_turboquant_index(
        db,  # type: ignore[arg-type]
        "public",
        "embeddings",
        "embedding",
        "embeddings_tq_idx",
        "inner_product",
        bits=4,
        lists=100,
        transform="hadamard",
        normalized=True,
        concurrently=False,
    )
    expected = (
        'CREATE INDEX "embeddings_tq_idx" '
        'ON "public"."embeddings" '
        'USING turboquant ("embedding" tq_inner_product_ops) '
        "WITH (bits = 4, lists = 100, transform = 'hadamard', normalized = true)"
    )
    assert result.create_sql == expected
    assert db.unmanaged == [expected]
    assert result.options == {"bits": 4, "lists": 100, "transform": "hadamard", "normalized": True}


async def test_create_turboquant_index_quotes_mixed_case_identifiers() -> None:
    # The catalog can legally hold mixed-case / quote-containing
    # names; the rendered SQL must survive PG parsing.
    db = _ddl_db_with_extension_installed()
    result = await create_turboquant_index(
        db,  # type: ignore[arg-type]
        "MySchema",
        "My_Table",
        "My_Column",
        "My_Idx",
        "l2",
    )
    expected = 'CREATE INDEX CONCURRENTLY "My_Idx" ON "MySchema"."My_Table" USING turboquant ("My_Column" tq_l2_ops)'
    assert result.create_sql == expected


# --- reindex_turboquant_index ----------------------------------------------


async def test_reindex_turboquant_index_raises_when_extension_absent() -> None:
    db = FakeDatabase(FakeRoutingDriver({"pg_extension": []}))  # type: ignore[arg-type]
    with pytest.raises(TurboQuantError, match="not installed"):
        await reindex_turboquant_index(db, "public", "idx")  # type: ignore[arg-type]


async def test_reindex_turboquant_index_raises_when_index_is_not_turboquant() -> None:
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_extension": [{"present": 1}],
                _PREFLIGHT_SUBSTR: [],
            }
        )
    )
    with pytest.raises(TurboQuantError, match="not a turboquant index"):
        await reindex_turboquant_index(db, "public", "other_idx")  # type: ignore[arg-type]


async def test_reindex_turboquant_index_renders_sql_and_runs_unmanaged() -> None:
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_extension": [{"present": 1}],
                _PREFLIGHT_SUBSTR: [{"present": 1}],
            }
        )
    )
    result = await reindex_turboquant_index(db, "public", "embeddings_tq_idx")  # type: ignore[arg-type]
    expected = 'REINDEX INDEX CONCURRENTLY "public"."embeddings_tq_idx"'
    assert result.reindex_sql == expected
    assert db.unmanaged == [expected]
    assert result.concurrently is True


async def test_reindex_turboquant_index_honours_concurrently_false() -> None:
    db = FakeDatabase(  # type: ignore[arg-type]
        FakeRoutingDriver(
            {
                "pg_extension": [{"present": 1}],
                _PREFLIGHT_SUBSTR: [{"present": 1}],
            }
        )
    )
    result = await reindex_turboquant_index(db, "public", "idx", concurrently=False)  # type: ignore[arg-type]
    assert result.reindex_sql == 'REINDEX INDEX "public"."idx"'
    assert db.unmanaged == ['REINDEX INDEX "public"."idx"']


@pytest.mark.parametrize("bad", ["yes", 1, "true"])
async def test_reindex_turboquant_index_rejects_non_bool_concurrently(bad: Any) -> None:
    db = _ddl_db_with_extension_installed()
    with pytest.raises(TurboQuantError, match="concurrently must be a bool"):
        await reindex_turboquant_index(db, "public", "idx", concurrently=bad)  # type: ignore[arg-type]


# --- TQ-5: query execution + per-query knobs --------------------------------


async def test_turboquant_approx_candidates_raises_when_extension_absent() -> None:
    driver = FakeRoutingDriver({"pg_extension": []})
    with pytest.raises(TurboQuantError, match="not installed"):
        await turboquant_approx_candidates(
            driver,  # type: ignore[arg-type]
            "public",
            "embeddings",
            "id",
            "embedding",
            [0.1, 0.2, 0.3],
            "cosine",
            10,
        )


@pytest.mark.parametrize(
    "metric",
    ["manhattan", "tq_cosine_ops", "Cosine", "ip"],
)
async def test_turboquant_approx_candidates_rejects_unsupported_metric(metric: str) -> None:
    # Note: ``ip`` is the upstream lexical token, but MCPg's public-
    # facing name is ``inner_product`` (a 3-name mapping centralised
    # in _TQ_METRIC_TEXT_FOR_METRIC). Callers should always use the
    # public name; the wrapper translates internally.
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(TurboQuantError, match="unsupported metric"):
        await turboquant_approx_candidates(
            driver,  # type: ignore[arg-type]
            "public",
            "embeddings",
            "id",
            "embedding",
            [0.1, 0.2, 0.3],
            metric,
            10,
        )


@pytest.mark.parametrize("field", ["schema", "table", "id_column", "embedding_column"])
async def test_turboquant_approx_candidates_rejects_unsafe_identifiers(field: str) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    kwargs: dict[str, Any] = {
        "schema": "public",
        "table": "embeddings",
        "id_column": "id",
        "embedding_column": "embedding",
        "query_vector": [0.1, 0.2, 0.3],
        "metric": "cosine",
        "candidate_limit": 10,
    }
    kwargs[field] = "bad; DROP"
    with pytest.raises(TurboQuantError, match="invalid"):
        await turboquant_approx_candidates(driver, **kwargs)  # type: ignore[arg-type]


async def test_turboquant_approx_candidates_rejects_non_positive_candidate_limit() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(TurboQuantError, match="candidate_limit"):
        await turboquant_approx_candidates(
            driver,  # type: ignore[arg-type]
            "public",
            "embeddings",
            "id",
            "embedding",
            [0.1, 0.2],
            "cosine",
            0,
        )


async def test_turboquant_approx_candidates_translates_metric_and_returns_rows() -> None:
    rows_returned = [
        {"candidate_id": "abc", "approximate_rank": 1, "approximate_distance": 0.1},
        {"candidate_id": "def", "approximate_rank": 2, "approximate_distance": 0.2},
    ]
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_approx_candidates": rows_returned,
        }
    )
    candidates = await turboquant_approx_candidates(
        driver,  # type: ignore[arg-type]
        "public",
        "embeddings",
        "id",
        "embedding",
        [0.1, 0.2, 0.3],
        "inner_product",  # public name
        10,
    )
    assert len(candidates) == 2
    assert candidates[0].candidate_id == "abc"
    assert candidates[1].approximate_rank == 2
    # Confirm the wrapper actually translated "inner_product" → "ip"
    # in the bound params (rather than passing the public name through
    # to upstream, which would error).
    call = next(c for c in driver.calls if "tq_approx_candidates" in c[0])
    _query, params, _ro = call
    assert "ip" in params  # the runtime metric token


async def test_turboquant_approx_candidates_serializes_list_query_vector() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_approx_candidates": [],
        }
    )
    await turboquant_approx_candidates(
        driver,  # type: ignore[arg-type]
        "public",
        "embeddings",
        "id",
        "embedding",
        [1.5, 2.5, 3.5],
        "l2",
        10,
    )
    call = next(c for c in driver.calls if "tq_approx_candidates" in c[0])
    _query, params, _ro = call
    assert "[1.5,2.5,3.5]" in params


async def test_turboquant_approx_candidates_passes_through_text_vector() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_approx_candidates": [],
        }
    )
    await turboquant_approx_candidates(
        driver,  # type: ignore[arg-type]
        "public",
        "embeddings",
        "id",
        "embedding",
        "[7,8,9]",  # pre-formatted
        "l2",
        10,
    )
    call = next(c for c in driver.calls if "tq_approx_candidates" in c[0])
    _query, params, _ro = call
    assert "[7,8,9]" in params


async def test_turboquant_approx_candidates_half_precision_selects_halfvec() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_approx_candidates": [],
        }
    )
    await turboquant_approx_candidates(
        driver,  # type: ignore[arg-type]
        "public",
        "embeddings",
        "id",
        "embedding",
        [0.1, 0.2],
        "cosine",
        10,
        half_precision=True,
    )
    call = next(c for c in driver.calls if "tq_approx_candidates" in c[0])
    query, _params, _ro = call
    assert "::halfvec" in query
    assert "::vector," not in query  # only the halfvec cast


async def test_turboquant_rerank_candidates_returns_both_rank_pairs() -> None:
    rows_returned = [
        {
            "candidate_id": "abc",
            "approximate_rank": 1,
            "approximate_distance": 0.10,
            "exact_rank": 1,
            "exact_distance": 0.05,
        },
        {
            "candidate_id": "def",
            "approximate_rank": 2,
            "approximate_distance": 0.20,
            "exact_rank": 3,
            "exact_distance": 0.18,
        },
    ]
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_rerank_candidates": rows_returned,
        }
    )
    candidates = await turboquant_rerank_candidates(
        driver,  # type: ignore[arg-type]
        "public",
        "embeddings",
        "id",
        "embedding",
        [0.1, 0.2, 0.3],
        "cosine",
        100,
        10,
    )
    assert len(candidates) == 2
    assert candidates[1].exact_rank == 3
    assert candidates[1].exact_distance == 0.18


async def test_turboquant_rerank_candidates_rejects_non_positive_final_limit() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(TurboQuantError, match="final_limit"):
        await turboquant_rerank_candidates(
            driver,  # type: ignore[arg-type]
            "public",
            "embeddings",
            "id",
            "embedding",
            [0.1, 0.2],
            "cosine",
            100,
            0,
        )


# --- recommend_turboquant_query_knobs --------------------------------------


async def test_recommend_turboquant_query_knobs_plain_overload() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_recommended_query_knobs": [
                {
                    "probes": 32,
                    "oversample_factor": 4,
                    "max_visited_codes": 50_000,
                    "max_visited_pages": 1_000,
                }
            ],
        }
    )
    knobs = await recommend_turboquant_query_knobs(driver, 100)  # type: ignore[arg-type]
    assert knobs.probes == 32
    assert knobs.oversample_factor == 4
    assert knobs.max_visited_codes == 50_000
    assert knobs.max_visited_pages == 1_000
    # Confirm we used the no-regclass overload (only 2 params bound)
    call = next(c for c in driver.calls if "tq_recommended_query_knobs" in c[0])
    _query, params, _ro = call
    assert params == [100, None]


async def test_recommend_turboquant_query_knobs_index_aware_overload() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_recommended_query_knobs": [
                {
                    "probes": 64,
                    "oversample_factor": 2,
                    "max_visited_codes": None,
                    "max_visited_pages": None,
                }
            ],
        }
    )
    knobs = await recommend_turboquant_query_knobs(
        driver,  # type: ignore[arg-type]
        100,
        final_limit=10,
        index_schema="public",
        index_name="embeddings_tq_idx",
        filter_selectivity=0.3,
    )
    assert knobs.probes == 64
    assert knobs.max_visited_codes is None  # None passthrough
    call = next(c for c in driver.calls if "tq_recommended_query_knobs" in c[0])
    query, params, _ro = call
    assert "regclass" in query  # used the regclass overload
    assert "public" in params and "embeddings_tq_idx" in params


async def test_recommend_turboquant_query_knobs_rejects_partial_index_arg() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(TurboQuantError, match="both index_schema and index_name"):
        await recommend_turboquant_query_knobs(
            driver,  # type: ignore[arg-type]
            100,
            index_schema="public",
        )
    with pytest.raises(TurboQuantError, match="both index_schema and index_name"):
        await recommend_turboquant_query_knobs(
            driver,  # type: ignore[arg-type]
            100,
            index_name="idx",
        )


async def test_recommend_turboquant_query_knobs_rejects_filter_selectivity_without_index() -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(TurboQuantError, match="filter_selectivity only applies"):
        await recommend_turboquant_query_knobs(
            driver,  # type: ignore[arg-type]
            100,
            filter_selectivity=0.5,
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_recommend_turboquant_query_knobs_rejects_non_finite_selectivity(bad: float) -> None:
    driver = FakeRoutingDriver({"pg_extension": [{"present": 1}]})
    with pytest.raises(TurboQuantError, match="filter_selectivity must be finite"):
        await recommend_turboquant_query_knobs(
            driver,  # type: ignore[arg-type]
            100,
            index_schema="public",
            index_name="idx",
            filter_selectivity=bad,
        )


async def test_recommend_turboquant_query_knobs_returns_all_none_when_empty() -> None:
    driver = FakeRoutingDriver(
        {
            "pg_extension": [{"present": 1}],
            "tq_recommended_query_knobs": [],  # upstream may return no row
        }
    )
    knobs = await recommend_turboquant_query_knobs(driver, 100)  # type: ignore[arg-type]
    assert knobs.probes is None
    assert knobs.oversample_factor is None


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
        "turboquant_approx_candidates",
        "turboquant_rerank_candidates",
        "recommend_turboquant_query_knobs",
    } <= listed
    # maintain_turboquant_index is WRITE-gated; must not be listed in
    # read-only mode.
    assert "maintain_turboquant_index" not in listed


_UNRESTRICTED = load_settings(
    {"MCPG_DATABASE_URL": "postgresql://u:p@localhost/db", "MCPG_ACCESS_MODE": "unrestricted"}
)


async def test_maintain_turboquant_index_registers_in_unrestricted_mode() -> None:
    server = create_server(_UNRESTRICTED, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "maintain_turboquant_index" in listed
    # DDL tools are gated by MCPG_ALLOW_DDL as well — absent without it.
    assert "create_turboquant_index" not in listed
    assert "reindex_turboquant_index" not in listed


_DDL = load_settings(
    {
        "MCPG_DATABASE_URL": "postgresql://u:p@localhost/db",
        "MCPG_ACCESS_MODE": "unrestricted",
        "MCPG_ALLOW_DDL": "true",
    }
)


async def test_turboquant_ddl_tools_register_with_ddl_opt_in() -> None:
    server = create_server(_DDL, database=FakeDatabase(FakeDriver()))  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert {"create_turboquant_index", "reindex_turboquant_index"} <= listed
